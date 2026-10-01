"""曦望同步搬运引擎。

一张单启动两个远端 worker：新加坡 ECS 把杭州 OSS 对象写入新加坡中转桶，
曦望 ECS 看到对象完整出现后立即拉到 NAS。中转 worker 写入 ``.panel-done``
标记后，曦望 worker 才结束；对象存储是原子可见的，因此不会读取半个对象。
"""
from __future__ import annotations

import base64
import os
import re
import shlex
import stat
from dataclasses import dataclass
from typing import Optional

from .errors import DeliveryError


class XiwangError(DeliveryError):
    pass


@dataclass(frozen=True)
class Config:
    sg_host: str = ""
    sg_port: int = 22
    sg_user: str = "root"
    sg_key_file: str = ""
    sg_host_key: str = ""
    xw_host: str = ""
    xw_port: int = 40002
    xw_user: str = "wuji"
    xw_key_file: str = ""
    xw_host_key: str = ""
    xw_dest_root: str = "/mnt/data04/296834/Wuji-Algorithm@wuji.tech/data"
    relay_endpoint: str = "oss-cn-singapore.aliyuncs.com"
    source_endpoint: str = "oss-cn-hangzhou-internal.aliyuncs.com"
    relay_region: str = "ap-southeast-1"
    source_region: str = "cn-hangzhou"
    relay_prefix: str = "aliyun-hz/"
    lock_file: str = "/tmp/wuji-panel-xiwang-transfer.lock"

    @classmethod
    def from_env(cls, environ: Optional[dict] = None):
        env = os.environ if environ is None else environ
        return cls(
            sg_host=str(env.get("XIWANG_SG_HOST", "") or "").strip(),
            sg_port=int(env.get("XIWANG_SG_PORT", "22") or 22),
            sg_user=str(env.get("XIWANG_SG_USER", "root") or "root"),
            sg_key_file=str(env.get("XIWANG_SG_KEY_FILE", "") or ""),
            sg_host_key=str(env.get("XIWANG_SG_HOST_KEY", "") or ""),
            xw_host=str(env.get("XIWANG_HOST", "") or "").strip(),
            xw_port=int(env.get("XIWANG_PORT", "40002") or 40002),
            xw_user=str(env.get("XIWANG_USER", "wuji") or "wuji"),
            xw_key_file=str(env.get("XIWANG_KEY_FILE", "") or ""),
            xw_host_key=str(env.get("XIWANG_HOST_KEY", "") or ""),
            xw_dest_root=(str(env.get("XIWANG_DEST_ROOT", cls.xw_dest_root) or cls.xw_dest_root).rstrip("/")),
            relay_endpoint=str(env.get("XIWANG_RELAY_ENDPOINT", cls.relay_endpoint) or cls.relay_endpoint),
            source_endpoint=str(env.get("XIWANG_SOURCE_ENDPOINT", cls.source_endpoint) or cls.source_endpoint),
            relay_region=str(env.get("XIWANG_RELAY_REGION", cls.relay_region) or cls.relay_region),
            source_region=str(env.get("XIWANG_SOURCE_REGION", cls.source_region) or cls.source_region),
            relay_prefix=str(env.get("XIWANG_RELAY_PREFIX", cls.relay_prefix) or cls.relay_prefix).strip("/") + "/",
            lock_file=str(env.get("XIWANG_LOCK_FILE", cls.lock_file) or cls.lock_file),
        )


def _safe(value: str, label: str) -> str:
    value = str(value or "").strip().strip("/")
    if not value or ".." in value or "//" in value or not re.fullmatch(r"[A-Za-z0-9._/-]+", value):
        raise XiwangError(f"{label} 含非法路径")
    return value


def _remote_dest(config: Config, relay_prefix: str) -> str:
    prefix = _safe(relay_prefix, "中转目录")
    if not prefix.startswith(config.relay_prefix.strip("/") + "/"):
        raise XiwangError("曦望中转目录必须落在 aliyun-hz/ 下")
    tail = prefix[len(config.relay_prefix.strip("/")) + 1 :].strip("/")
    if not tail:
        raise XiwangError("曦望目标目录不能为空")
    return f"{config.xw_dest_root}/{tail}"


def _config_file(job_id: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_-]", "", str(job_id or ""))
    if not clean:
        raise XiwangError("曦望任务号为空")
    return f"/tmp/wuji-panel-xiwang-{clean}.ini"


def _oss_ini(credentials: dict, *, endpoint: str) -> str:
    ak = str(credentials.get("access_key_id") or "")
    sk = str(credentials.get("access_key_secret") or "")
    if not ak or not sk:
        raise XiwangError("曦望任务缺少一次性 OSS 凭证")
    return "\n".join(
        ["[Credentials]", "language = CH", f"endpoint = {endpoint}", f"accessKeyID = {ak}", f"accessKeySecret = {sk}"]
    ) + "\n"


def commands(plan: dict, config: Config, job_id: str) -> dict:
    """生成两端 worker 命令；命令中不包含 AK/SK。"""
    src, dst = plan["src"], plan["dest"]
    bucket = _safe(src.get("bucket"), "源桶")
    source = _safe(src.get("prefix", ""), "源目录")
    relay_bucket = _safe(dst.get("bucket"), "中转桶")
    relay = _safe(dst.get("prefix", ""), "中转目录")
    target = _remote_dest(config, relay)
    marker = f"{relay.rstrip('/')}/.panel-done"
    source_uri = f"oss://{bucket}/{source}/"
    relay_uri = f"oss://{relay_bucket}/{relay}/"
    sg = (
        f"mkdir -p \"$WD\" && trap 'rm -rf -- \"$WD/ckpt\"' EXIT && "
        f"ossutil cp -r {shlex.quote(source_uri)} {shlex.quote(relay_uri)} "
        f"--endpoint {shlex.quote(config.source_endpoint)} --region {shlex.quote(config.source_region)} "
        f"--job 16 --parallel 8 --checkpoint-dir \"$WD/ckpt\" && "
        f"printf done > \"$WD/done\" && "
        f"ossutil cp /dev/null {shlex.quote('oss://' + relay_bucket + '/' + marker)} "
        f"--endpoint {shlex.quote(config.relay_endpoint)} --region {shlex.quote(config.relay_region)}; "
        f"rc=$?; printf '%s\\n' \"$rc\" > \"$WD/relay.rc\"; exit \"$rc\""
    )
    xw = (
        f"mkdir -p {shlex.quote(target)} \"$WD\"; trap 'rm -rf -- \"$WD/ckpt\"' EXIT; "
        f"while :; do "
        f"ossutil cp -r {shlex.quote(relay_uri)} {shlex.quote(target + '/')} "
        f"--endpoint {shlex.quote(config.relay_endpoint)} --region {shlex.quote(config.relay_region)} "
        f"--job 16 --parallel 8 --checkpoint-dir \"$WD/ckpt\" -u; "
        f"ossutil ls {shlex.quote('oss://' + relay_bucket + '/' + marker)} "
        f"--endpoint {shlex.quote(config.relay_endpoint)} --region {shlex.quote(config.relay_region)} >/dev/null 2>&1 && break; "
        f"sleep 10; done"
    )
    return {"sg": sg, "xw": xw, "target": target, "marker": marker, "config_path": _config_file(job_id)}


def validate_config(config: Config) -> None:
    for path, label in ((config.sg_key_file, "新加坡 SSH 私钥"), (config.xw_key_file, "曦望 SSH 私钥")):
        if not path:
            raise XiwangError(f"未配置{label}")
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except OSError as exc:
            raise XiwangError(f"{label}不可读：{exc}") from exc
        if mode & 0o077:
            raise XiwangError(f"{label}权限必须是 0600 或 0400")

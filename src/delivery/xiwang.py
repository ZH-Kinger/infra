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
from .transfer_telemetry import parse_sample, sample_command
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
    sg_mount_root: str = "/mnt"
    relay_endpoint: str = "oss-ap-southeast-1.aliyuncs.com"
    source_endpoint: str = "oss-cn-hangzhou-internal.aliyuncs.com"
    relay_region: str = "ap-southeast-1"
    source_region: str = "cn-hangzhou"
    relay_prefix: str = "aliyun-hz/"
    lock_file: str = "/tmp/wuji-panel-xiwang-transfer.lock"
    work_root: str = "/tmp"

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
            sg_mount_root=(str(env.get("XIWANG_SG_MOUNT_ROOT", cls.sg_mount_root) or cls.sg_mount_root).rstrip("/")),
            relay_endpoint=str(env.get("XIWANG_RELAY_ENDPOINT", cls.relay_endpoint) or cls.relay_endpoint),
            source_endpoint=str(env.get("XIWANG_SOURCE_ENDPOINT", cls.source_endpoint) or cls.source_endpoint),
            relay_region=str(env.get("XIWANG_RELAY_REGION", cls.relay_region) or cls.relay_region),
            source_region=str(env.get("XIWANG_SOURCE_REGION", cls.source_region) or cls.source_region),
            relay_prefix=str(env.get("XIWANG_RELAY_PREFIX", cls.relay_prefix) or cls.relay_prefix).strip("/") + "/",
            lock_file=str(env.get("XIWANG_LOCK_FILE", cls.lock_file) or cls.lock_file),
            work_root=str(env.get("XIWANG_WORK_ROOT", cls.work_root) or cls.work_root).rstrip("/"),
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


def commands(plan: dict, config: Config, job_id: str, include_prefixes=None) -> dict:
    """生成两端 worker 命令；命令中不包含 AK/SK。"""
    src, dst = plan["src"], plan["dest"]
    bucket = _safe(src.get("bucket"), "源桶")
    source = _safe(src.get("prefix", ""), "源目录")
    relay_bucket = _safe(dst.get("bucket"), "中转桶")
    relay = _safe(dst.get("prefix", ""), "中转目录")
    target = _remote_dest(config, relay)
    meta_prefix = f"{relay.rstrip('/')}/.panel-meta/{_safe(job_id, '任务号')}"
    marker = f"{meta_prefix}/done"
    config_path = _config_file(job_id)
    cfg = f"--config-file {shlex.quote(config_path)}"
    source_uri = f"oss://{bucket}/{source}/"
    relay_uri = f"oss://{relay_bucket}/{relay}/"
    relay_mount = f"{config.sg_mount_root}/{relay}"
    items = [str(x or "").strip().strip("/") for x in (include_prefixes or [])]
    if items:
        if any(not re.fullmatch(r"[A-Za-z0-9._-]+", x) for x in items) or len(set(items)) != len(items):
            raise XiwangError("曦望同步子目录白名单不合法")
        sg_parts = [
            f"( mkdir -p {shlex.quote(relay_mount + '/' + x)} && ossutil cp -r {shlex.quote(source_uri + x + '/') } {shlex.quote(relay_mount + '/' + x + '/') } {cfg} "
            f"--endpoint {shlex.quote(config.source_endpoint)} --region {shlex.quote(config.source_region)} "
            f"--job 30 --parallel 16 --checkpoint-dir \"$WD/ckpt-{x}\" -u && "
            f"mkdir -p {shlex.quote(config.sg_mount_root + '/' + meta_prefix)} && "
            f"touch {shlex.quote(config.sg_mount_root + '/' + meta_prefix + '/' + x + '.done')} ) &"
            for x in items
        ]
        sg_steps = "; ".join(" ".join(sg_parts[i:i + 4]) + " wait" for i in range(0, len(sg_parts), 4))
        xw_parts = [
            f"( i=0; while [ $i -lt 120960 ]; do ossutil cp {shlex.quote('oss://' + relay_bucket + '/' + meta_prefix + '/' + x + '.done')} \"$WD/ready-{x}\" {cfg} -f "
            f"--endpoint {shlex.quote(config.relay_endpoint)} --region {shlex.quote(config.relay_region)} >/dev/null 2>&1 && break; i=$((i + 1)); sleep 10; done; "
            f"test $i -lt 120960 || exit 75; ossutil cp -r {shlex.quote(relay_uri + x + '/')} {shlex.quote(target + '/' + x + '/')} {cfg} "
            f"--endpoint {shlex.quote(config.relay_endpoint)} --region {shlex.quote(config.relay_region)} --job 30 --parallel 16 --checkpoint-dir \"$WD/ckpt-{x}\" -u ) &"
            for x in items
        ]
        xw_steps = "; ".join(" ".join(xw_parts[i:i + 4]) + " wait" for i in range(0, len(xw_parts), 4)) + "; b=$(du -sb " + shlex.quote(target) + " 2>/dev/null | awk '{print $1}'); o=$(find " + shlex.quote(target) + " -type f 2>/dev/null | wc -l); printf '%s\\n%s\\n' \"$b\" \"$o\" > \"$WD/progress\""
        copies = sg_steps
    else:
        copies = (
            f"mkdir -p {shlex.quote(relay_mount)} && ossutil cp -r {shlex.quote(source_uri)} {shlex.quote(relay_mount + '/')} {cfg} "
            f"--endpoint {shlex.quote(config.source_endpoint)} --region {shlex.quote(config.source_region)} "
            f"--job 30 --parallel 16 --checkpoint-dir \"$WD/ckpt\" -u"
        )
        xw_steps = (
            f"ossutil cp -r {shlex.quote(relay_uri)} {shlex.quote(target + '/')} {cfg} "
            f"--endpoint {shlex.quote(config.relay_endpoint)} --region {shlex.quote(config.relay_region)} "
            f"--job 30 --parallel 16 --checkpoint-dir \"$WD/ckpt\" -u; "
            f"{{ du -sb {shlex.quote(target)} 2>/dev/null | awk '{{print $1}}'; find {shlex.quote(target)} -type f 2>/dev/null | wc -l; }} > \"$WD/progress\""
        )
    sg = (
        f"mkdir -p \"$WD\" && trap 'rm -rf -- \"$WD/ckpt\"' EXIT && "
        f"total_b=0; total_o=0; "
        f"{copies} && "
        f"printf done > \"$WD/done\" && "
        f"mkdir -p {shlex.quote(config.sg_mount_root + '/' + meta_prefix)} && touch {shlex.quote(config.sg_mount_root + '/' + marker)}; "
        f"rc=$?; printf '%s\\n' \"$rc\" > \"$WD/relay.rc\"; exit \"$rc\""
    )
    xw = (
        f"mkdir -p {shlex.quote(target)} \"$WD\"; total_b=0; total_o=0; {xw_steps}"
    )
    return {"sg": sg, "xw": xw, "target": target, "marker": marker, "config_path": config_path}


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


def _job_dir(config: Config, job_id: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_-]", "", str(job_id or ""))
    if not clean:
        raise XiwangError("曦望任务号为空")
    return f"{config.work_root}/wuji-panel-xiwang-{clean}"


def _connect(host: str, port: int, user: str, key_file: str, host_key: str):
    try:
        import paramiko
    except ImportError as exc:  # pragma: no cover
        raise XiwangError("曦望链路需要 paramiko") from exc
    if not host or not key_file or not host_key:
        raise XiwangError("曦望链路缺少 SSH 主机、私钥或 host key")
    mode = stat.S_IMODE(os.stat(key_file).st_mode)
    if mode & 0o077:
        raise XiwangError("曦望 SSH 私钥权限必须是 0600 或 0400")
    fields = host_key.split()
    if len(fields) < 2:
        raise XiwangError("曦望 host key 格式不对")
    kind, encoded = fields[-2], fields[-1]
    ctor = {"ssh-ed25519": paramiko.Ed25519Key, "ssh-rsa": paramiko.RSAKey}.get(kind)
    if ctor is None:
        raise XiwangError(f"不支持的曦望 host key：{kind}")
    client = paramiko.SSHClient()
    client.get_host_keys().add(f"[{host}]:{port}" if port != 22 else host, kind, ctor(data=__import__("base64").b64decode(encoded)))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(host, port=port, username=user, key_filename=key_file, timeout=20, banner_timeout=20, auth_timeout=20, allow_agent=False, look_for_keys=False)
    except Exception as exc:
        client.close()
        raise XiwangError(f"连接曦望链路主机失败：{exc}") from exc
    return client


def _exec(client, script: str, timeout: int = 30) -> tuple[int, str, str]:
    _, out, err = client.exec_command(script, timeout=timeout)
    return out.channel.recv_exit_status(), out.read().decode("utf-8", "replace"), err.read().decode("utf-8", "replace")


def _write_config(client, path: str, text: str) -> None:
    sftp = client.open_sftp()
    try:
        with sftp.open(path, "w") as fh:
            fh.write(text)
        sftp.chmod(path, 0o600)
    finally:
        sftp.close()


def submit(plan: dict, job_id: str, *, config: Optional[Config] = None, credentials: Optional[dict] = None, include_prefixes=None) -> str:
    config = config or Config.from_env()
    validate_config(config)
    got = commands(plan, config, job_id, include_prefixes=include_prefixes)
    work = _job_dir(config, job_id)
    path = got["config_path"]
    clients = []
    try:
        for host, port, user, key, script in (
            (config.sg_host, config.sg_port, config.sg_user, config.sg_key_file, got["sg"]),
            (config.xw_host, config.xw_port, config.xw_user, config.xw_key_file, got["xw"]),
        ):
            client = _connect(host, port, user, key, config.sg_host_key if host == config.sg_host else config.xw_host_key)
            clients.append(client)
            endpoint = config.source_endpoint if host == config.sg_host else config.relay_endpoint
            _write_config(client, path, _oss_ini(credentials or {}, endpoint=endpoint))
            stage = 'relay' if host == config.sg_host else 'pull'
            worker = f"set -eu\nWD={shlex.quote(work)}\nexport WD\ntrap 'rc=$?; printf \"%s\\n\" \"$rc\" > \"$WD/{stage}.rc\"; rm -f -- {shlex.quote(path)}' EXIT\n{script}\n"
            encoded = base64.b64encode(worker.encode()).decode()
            rc, out, err = _exec(client, f"mkdir -p {shlex.quote(work)}; nohup bash -c \"$(echo {encoded} | base64 -d)\" > {shlex.quote(work)}/{'relay' if host == config.sg_host else 'pull'}.log 2>&1 & echo $!", timeout=30)
            if rc != 0 or not out.strip():
                raise XiwangError(f"曦望 worker 下发失败：{(err or out)[:240]}")
        return str(job_id)
    except Exception:
        for client in clients:
            try:
                _exec(client, f"rm -f -- {shlex.quote(path)}")
            finally:
                client.close()
        raise
    finally:
        for client in clients:
            client.close()


def poll(job_id: str, *, config: Optional[Config] = None) -> dict:
    config = config or Config.from_env()
    work = _job_dir(config, job_id)
    results = []
    connection_errors = []
    for host, port, user, key, host_key, marker in (
        (config.sg_host, config.sg_port, config.sg_user, config.sg_key_file, config.sg_host_key, "relay"),
        (config.xw_host, config.xw_port, config.xw_user, config.xw_key_file, config.xw_host_key, "pull"),
    ):
        try:
            client = _connect(host, port, user, key, host_key)
        except XiwangError as exc:
            connection_errors.append(f"{marker} 主机 {host}:{port}：{exc}")
            continue
        try:
            rc, out, err = _exec(client, f"cat {shlex.quote(work)}/{marker}.rc 2>/dev/null || true; echo PROGRESS; cat {shlex.quote(work)}/progress 2>/dev/null || true; echo LOG; tail -c 12000 {shlex.quote(work)}/{marker}.log 2>/dev/null || true", timeout=12)
            raw, _, rest = out.partition("PROGRESS\n")
            progress, _, log = rest.partition("LOG\n")
            results.append((marker, raw.strip(), progress.strip(), log.strip()))
            try:
                _, telemetry, _ = _exec(client, sample_command(job_id, f"/mnt/aliyun-hz/worldengine/.panel-meta/{job_id}"), timeout=8)
                results[-1] = results[-1] + (parse_sample(telemetry.strip()),)
            except Exception:
                results[-1] = results[-1] + ({},)
        finally:
            client.close()
    info = {row[0]: row[1:] for row in results}
    relay, pull = info.get("relay", ("", "", ""))[0], info.get("pull", ("", "", ""))[0]
    progress = info.get("pull", ("", "", ""))[1].splitlines()
    relay_log = info.get("relay", ("", "", ""))[2]
    pull_log = info.get("pull", ("", "", ""))[2]
    relay_telemetry = info.get("relay", ("", "", "", {}))[3]
    pull_telemetry = info.get("pull", ("", "", "", {}))[3]
    bytes_done = int(progress[0]) if progress and progress[0].isdigit() else 0
    objects_done = int(progress[1]) if len(progress) > 1 and progress[1].isdigit() else 0
    relay_bytes_done = 0
    relay_objects_done = 0
    discovered = {}
    matches = re.findall(r"(?:Estimated|Total)\s+(\d+) objects,\s*([0-9.]+)\s*(KiB|MiB|GiB|TiB)", relay_log)
    if matches:
        match = matches[-1]
        factor = {"KiB": 2**10, "MiB": 2**20, "GiB": 2**30, "TiB": 2**40}[match[2]]
        discovered = {"source_objects": int(match[0]), "source_bytes": int(float(match[1]) * factor)}
    percent = re.findall(r"(\d+(?:\.\d+)?)%", relay_log)
    if percent:
        discovered["source_percent"] = float(percent[-1])
    done = re.findall(r"done:\((\d+)\s+(?:files|objects),\s*([0-9.]+)\s*(KiB|MiB|GiB|TiB)\)", relay_log)
    if done:
        n, amount, unit = done[-1]
        relay_bytes_done = int(float(amount) * {"KiB": 2**10, "MiB": 2**20, "GiB": 2**30, "TiB": 2**40}[unit])
        relay_objects_done = int(n)
    relay_skipped = re.findall(r"skipped:\((\d+)\s+(?:files|objects),\s*([0-9.]+)\s*(KiB|MiB|GiB|TiB)\)", relay_log)
    if relay_skipped:
        n, amount, unit = relay_skipped[-1]
        relay_bytes_done += int(float(amount) * {"KiB": 2**10, "MiB": 2**20, "GiB": 2**30, "TiB": 2**40}[unit])
        relay_objects_done += int(n)
    pull_done = re.findall(r"done:\((\d+)\s+(?:files|objects),\s*([0-9.]+)\s*(KiB|MiB|GiB|TiB)\)", pull_log)
    if pull_done:
        n, amount, unit = pull_done[-1]
        bytes_done = int(float(amount) * {"KiB": 2**10, "MiB": 2**20, "GiB": 2**30, "TiB": 2**40}[unit])
        objects_done = int(n)
    skipped = re.findall(r"skipped:\((\d+)\s+(?:files|objects),\s*([0-9.]+)\s*(KiB|MiB|GiB|TiB)\)", pull_log)
    if skipped:
        n, amount, unit = skipped[-1]
        bytes_done += int(float(amount) * {"KiB": 2**10, "MiB": 2**20, "GiB": 2**30, "TiB": 2**40}[unit])
        objects_done += int(n)
    speeds = re.findall(r"avg\s+([0-9.]+)\s*(KiB|MiB|GiB|TiB)/s", pull_log)
    if speeds:
        amount, unit = speeds[-1]
        speed_bps = int(float(amount) * {"KiB": 2**10, "MiB": 2**20, "GiB": 2**30, "TiB": 2**40}[unit])
    else:
        speed_bps = 0
    relay_speeds = re.findall(r"avg\s+([0-9.]+)\s*(KiB|MiB|GiB|TiB)/s", relay_log)
    relay_speed_bps = 0
    if relay_speeds:
        amount, unit = relay_speeds[-1]
        relay_speed_bps = int(float(amount) * {"KiB": 2**10, "MiB": 2**20, "GiB": 2**30, "TiB": 2**40}[unit])
    # /proc/io 统计会把 ossutil 的多个进程和缓存写入重复计数，可能出现
    # 数 GB/s 的虚高值；原始 ossutil avg 才是链路速率。只有日志没有速率时才用采样兜底。
    if relay_speed_bps <= 0:
        relay_speed_bps = int(relay_telemetry.get("speed_bps") or 0)
    if speed_bps <= 0:
        speed_bps = int(pull_telemetry.get("speed_bps") or 0)
    active_batches = sorted(set(relay_telemetry.get("active", [])) | set(pull_telemetry.get("active", [])))
    completed_batches = sorted(set(relay_telemetry.get("completed", [])) | set(pull_telemetry.get("completed", [])))
    if pull == "0" and relay == "0" and not connection_errors:
        return {"status": "DONE", "done": True, "failed": False, "error": "", "bytes": bytes_done, "objects": objects_done, "relay_bytes": relay_bytes_done, "relay_objects": relay_objects_done, "speed_bps": speed_bps, "relay_speed_bps": relay_speed_bps, "active_batches": active_batches, "completed_batches": completed_batches, **discovered}
    if relay.isdigit() and relay != "0":
        return {"status": "FAILED", "done": False, "failed": True, "error": f"新加坡 worker 退出码 {relay}", "bytes": bytes_done, "objects": objects_done, "relay_bytes": relay_bytes_done, "relay_objects": relay_objects_done, "speed_bps": speed_bps, "relay_speed_bps": relay_speed_bps, "active_batches": active_batches, "completed_batches": completed_batches, **discovered}
    if pull.isdigit() and pull != "0":
        return {"status": "FAILED", "done": False, "failed": True, "error": f"曦望 worker 退出码 {pull}", "bytes": bytes_done, "objects": objects_done, "relay_bytes": relay_bytes_done, "relay_objects": relay_objects_done, "speed_bps": speed_bps, "relay_speed_bps": relay_speed_bps, "active_batches": active_batches, "completed_batches": completed_batches, **discovered}
    return {"status": "RUNNING", "done": False, "failed": False, "error": "; ".join(connection_errors), "bytes": bytes_done, "objects": objects_done, "relay_bytes": relay_bytes_done, "relay_objects": relay_objects_done, "speed_bps": speed_bps, "relay_speed_bps": relay_speed_bps, "active_batches": active_batches, "completed_batches": completed_batches, **discovered}

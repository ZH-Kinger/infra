"""九章（AlayaNeW）与对象存储之间的面板迁移引擎。

九章没有可供面板调用的对象存储迁移 API，链路由面板主动 SSH 到九章，在九章
机器上执行固定版本的 ``ossutil``：

* ``oss://… -> jz://jz-b200/…``：九章直接从杭州 OSS 拉到 GPFS；
* ``jz://jz-b200/… -> oss://wuji-data-tran/alayanew/<数据类型>/…``：九章结果按
  回传桶词表归档，主数据桶的最终沉降由后续校验/确认流程处理。

这里不接受私钥内容放进申请单或普通配置。生产环境用权限为 0600/0400 的文件，
并且固定 host key；测试只替换 ``run``，所以不会发起真实连接。
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


class JiuzhangError(DeliveryError):
    """九章连接或远端任务失败。"""


@dataclass(frozen=True)
class Config:
    host: str = ""
    port: int = 30019
    user: str = "root"
    key_file: str = ""
    host_key: str = ""
    dest_root: str = "/root/nas"
    work_dir: str = "$HOME/.panel_jiuzhang_jobs"
    oss_endpoint: str = "oss-cn-hangzhou.aliyuncs.com"
    oss_region: str = "cn-hangzhou"
    jobs: int = 32
    parallel: int = 8
    lock_file: str = "/tmp/wuji-panel-jiuzhang-transfer.lock"

    @classmethod
    def from_env(cls, environ: Optional[dict] = None) -> "Config":
        env = os.environ if environ is None else environ

        def positive(name: str, default: int) -> int:
            try:
                return max(1, int(env.get(name, "") or default))
            except (TypeError, ValueError):
                return default

        return cls(
            host=str(env.get("JIUZHANG_HOST", "") or "").strip(),
            port=positive("JIUZHANG_PORT", 30019),
            user=str(env.get("JIUZHANG_USER", "root") or "root").strip(),
            key_file=str(env.get("JIUZHANG_SSH_KEY_FILE", "") or "").strip(),
            host_key=str(env.get("JIUZHANG_HOST_KEY", "") or "").strip(),
            dest_root=(str(env.get("JIUZHANG_DEST_ROOT", "/root/nas") or "/root/nas").rstrip("/") or "/"),
            work_dir=(str(env.get("JIUZHANG_WORK_DIR", "$HOME/.panel_jiuzhang_jobs") or "$HOME/.panel_jiuzhang_jobs").rstrip("/") or "$HOME"),
            oss_endpoint=str(env.get("JIUZHANG_OSS_ENDPOINT", "oss-cn-hangzhou.aliyuncs.com") or "").strip(),
            oss_region=str(env.get("JIUZHANG_OSS_REGION", "cn-hangzhou") or "").strip(),
            jobs=positive("JIUZHANG_OSSUTIL_JOBS", 32),
            parallel=positive("JIUZHANG_OSSUTIL_PARALLEL", 8),
            lock_file=str(
                env.get("JIUZHANG_LOCK_FILE", "/tmp/wuji-panel-jiuzhang-transfer.lock")
                or "/tmp/wuji-panel-jiuzhang-transfer.lock"
            ).strip(),
        )


def _segment_path(prefix: str) -> str:
    """把已由 URI 白名单检查过的前缀变成安全的相对路径。"""
    value = str(prefix or "").strip().strip("/")
    if not value:
        return ""
    if "//" in value or any(part in ("", ".", "..") for part in value.split("/")):
        raise JiuzhangError("九章目录包含非法路径段")
    if any(ch in value for ch in ("\x00", "\n", "\r", " ", "\\", "*", "?")):
        raise JiuzhangError("九章目录包含非法字符")
    return value


def remote_dir(config: Config, node: dict) -> str:
    """将 ``jz://集群/目录/`` 映射到九章 GPFS 目录。"""
    if node.get("scheme") != "jz":
        raise JiuzhangError("九章目录必须使用 jz:// 方案")
    rel = _segment_path(node.get("prefix", ""))
    root = config.dest_root.rstrip("/") or "/"
    return f"{root}/{rel}" if rel else root


def _flags(config: Config) -> str:
    parts = []
    if config.oss_endpoint:
        parts.append(f"-e {shlex.quote(config.oss_endpoint)}")
    if config.oss_region:
        parts.append(f"--region {shlex.quote(config.oss_region)}")
    parts.extend([f"--job {config.jobs}", f"--parallel {config.parallel}", "-u", "-f"])
    return " ".join(parts)


def _private_key(config: Config):
    try:
        import paramiko
    except ImportError as exc:  # pragma: no cover - production dependency
        raise JiuzhangError("九章链路需要 paramiko") from exc
    if not config.key_file:
        raise JiuzhangError("未配置 JIUZHANG_SSH_KEY_FILE")
    try:
        mode = stat.S_IMODE(os.stat(config.key_file).st_mode)
    except OSError as exc:
        raise JiuzhangError(f"九章私钥不可读：{exc}") from exc
    if mode & 0o077:
        raise JiuzhangError("九章私钥权限必须是 0600 或 0400")
    for cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            return cls.from_private_key_file(config.key_file)
        except Exception:
            continue
    raise JiuzhangError("九章私钥格式无法解析")


def _client(config: Config):
    try:
        import paramiko
    except ImportError as exc:  # pragma: no cover - production dependency
        raise JiuzhangError("九章链路需要 paramiko") from exc
    if not config.host:
        raise JiuzhangError("未配置 JIUZHANG_HOST")
    fields = config.host_key.split()
    if len(fields) < 2:
        raise JiuzhangError("未配置合法的 JIUZHANG_HOST_KEY")
    kind, encoded = fields[-2], fields[-1]
    ctor = {
        "ssh-ed25519": paramiko.Ed25519Key,
        "ssh-rsa": paramiko.RSAKey,
        "ecdsa-sha2-nistp256": paramiko.ECDSAKey,
    }.get(kind)
    if ctor is None:
        raise JiuzhangError(f"不支持的九章 host key 类型：{kind}")
    try:
        key = ctor(data=base64.b64decode(encoded))
    except Exception as exc:
        raise JiuzhangError("JIUZHANG_HOST_KEY 不是合法公钥") from exc
    client = paramiko.SSHClient()
    name = f"[{config.host}]:{config.port}" if config.port != 22 else config.host
    client.get_host_keys().add(name, kind, key)
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(
            hostname=config.host,
            port=config.port,
            username=config.user,
            pkey=_private_key(config),
            timeout=20,
            banner_timeout=20,
            auth_timeout=20,
            allow_agent=False,
            look_for_keys=False,
        )
    except Exception as exc:
        client.close()
        raise JiuzhangError(f"连接九章失败：{exc}") from exc
    return client


def run(script: str, *, config: Optional[Config] = None, timeout: int = 90) -> tuple[int, str, str]:
    client = _client(config or Config.from_env())
    try:
        _, stdout, stderr = client.exec_command(script, timeout=timeout)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        return stdout.channel.recv_exit_status(), out, err
    finally:
        client.close()


def _work_dir(config: Config, job_id: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_-]", "", str(job_id or ""))
    if not clean:
        raise JiuzhangError("九章任务号为空")
    return f"{config.work_dir}/{clean}"


def _assign_path(path: str, variable: str = "JD") -> str:
    """为远端 shell 生成安全的变量赋值，保留受控的 ``$HOME`` 展开。"""
    if path == "$HOME":
        return f'{variable}="$HOME"'
    if path.startswith("$HOME/"):
        tail = path[len("$HOME/") :]
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", tail):
            raise JiuzhangError("JIUZHANG_WORK_DIR 含非法字符")
        return f'{variable}="$HOME/{tail}"'
    return f"{variable}={shlex.quote(path)}"


def _config_path(job_id: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_-]", "", str(job_id or ""))
    if not clean:
        raise JiuzhangError("九章任务号为空")
    return f"/tmp/wuji-panel-jiuzhang-{clean}.ini"


def _config_text(config: Config, credentials: dict) -> str:
    """ossutil 专用临时配置；内容只通过 SFTP 传输，不进入远端命令行。"""
    ak = str(credentials.get("access_key_id") or "")
    sk = str(credentials.get("access_key_secret") or "")
    token = str(credentials.get("security_token") or "")
    if not ak or not sk:
        raise JiuzhangError("九章任务缺少一次性 OSS 凭证")
    lines = [
        "[Credentials]",
        "language = CH",
        f"endpoint = {config.oss_endpoint}",
        f"accessKeyID = {ak}",
        f"accessKeySecret = {sk}",
    ]
    if token:
        lines.append(f"securityToken = {token}")
    return "\n".join(lines) + "\n"


def _install_config(path: str, text: str, *, config: Config) -> None:
    """通过 SSH 加密通道写入 0600 配置，不把 AK/SK 放进 shell 命令。"""
    client = _client(config)
    try:
        sftp = client.open_sftp()
        try:
            with sftp.open(path, "w") as fh:
                fh.write(text)
            sftp.chmod(path, 0o600)
        finally:
            sftp.close()
    except Exception as exc:
        raise JiuzhangError(f"写入九章一次性凭证失败：{exc}") from exc
    finally:
        client.close()


def _cleanup_config(path: str, *, config: Config) -> None:
    rc, out, err = run(f"rm -f -- {shlex.quote(path)}", config=config, timeout=30)
    if rc != 0:
        raise JiuzhangError(f"清理九章一次性凭证失败：{(err or out)[:160]}")


def _command(
    plan: dict, config: Config, work_dir: str, config_path: Optional[str] = None
) -> str:
    src, dst = plan["src"], plan["dest"]
    config_path = config_path or "/tmp/wuji-panel-jiuzhang-command.ini"
    flags = f"--config-file {shlex.quote(config_path)} {_flags(config)}"
    if plan["direction"] == "oss->jz":
        source = f"oss://{src['bucket']}/{src.get('prefix', '')}"
        target = remote_dir(config, dst)
        return (
            f"mkdir -p {shlex.quote(target)} && "
            f"ossutil cp -r {shlex.quote(source)} {shlex.quote(target)} {flags} "
            f'--checkpoint-dir "$JD/ckpt"'
        )
    if plan["direction"] == "jz->oss":
        source = remote_dir(config, src)
        target = f"oss://{dst['bucket']}/{dst.get('prefix', '')}"
        return (
            f"test -d {shlex.quote(source)} && "
            f"ossutil cp -r {shlex.quote(source)}/ {shlex.quote(target)} {flags} "
            f'--checkpoint-dir "$JD/ckpt"'
        )
    raise JiuzhangError(f"九章不支持方向 {plan.get('direction', '')}")


def submit(
    plan: dict,
    job_id: str,
    *,
    config: Optional[Config] = None,
    credentials: Optional[dict] = None,
) -> str:
    config = config or Config.from_env()
    work = _work_dir(config, job_id)
    config_path = _config_path(job_id)
    _install_config(config_path, _config_text(config, credentials or {}), config=config)
    inner = _command(plan, config, work, config_path)
    # 退出码写入 marker 后再由 EXIT trap 删除一次性配置。即使 ossutil
    # 失败或远端 SSH 在启动后断开，AK/SK 也不会一直留在九章。
    worker = (
        f"trap 'rm -f -- {shlex.quote(config_path)}' EXIT\n"
        f"{inner}\n"
        "rc=$?\n"
        "printf '%s\\n' \"$rc\" > \"$JD/transfer.rc\"\n"
        "exit \"$rc\"\n"
    )
    encoded = base64.b64encode(worker.encode("utf-8")).decode("ascii")
    script = f'''set -u
LOCK={shlex.quote(config.lock_file)}
exec 9>"$LOCK"
if ! flock -n 9; then echo BUSY; exit 75; fi
{_assign_path(work)}
mkdir -p "$JD/ckpt" || exit 1
if [ -f "$JD/transfer.pid" ] && kill -0 "$(cat "$JD/transfer.pid" 2>/dev/null)" 2>/dev/null; then
  echo ALREADY_RUNNING; exit 0
fi
rm -f "$JD/transfer.rc"
export JD
WORK=$(echo {encoded} | base64 -d)
{{ nohup bash -c "$WORK" > "$JD/transfer.log" 2>&1 &
  echo $! > "$JD/transfer.pid"; }}
sleep 2
if [ -f "$JD/transfer.rc" ]; then
  echo "ALREADY_DONE rc=$(cat "$JD/transfer.rc")"
elif kill -0 "$(cat "$JD/transfer.pid" 2>/dev/null)" 2>/dev/null; then
  echo LAUNCHED
else
  echo LAUNCH_DEAD
fi
'''
    rc, out, err = run(script, config=config)
    if rc != 0:
        try:
            _cleanup_config(config_path, config=config)
        finally:
            raise JiuzhangError(f"九章任务下发失败：{(err or out)[:300]}")
    if "BUSY" in out:
        _cleanup_config(config_path, config=config)
        raise JiuzhangError("九章已有传输任务在运行，本任务不会并行下发")
    if "LAUNCH_DEAD" in out:
        _cleanup_config(config_path, config=config)
        raise JiuzhangError(f"九章任务启动后退出：{out[:300]}")
    if not any(mark in out for mark in ("LAUNCHED", "ALREADY_RUNNING", "ALREADY_DONE")):
        _cleanup_config(config_path, config=config)
        raise JiuzhangError(f"九章任务下发结果无法确认：{out[:300]}")
    if "ALREADY_DONE" in out:
        # 已结束的旧任务没有新的 worker 替我们执行 EXIT trap。
        _cleanup_config(config_path, config=config)
    return str(job_id)


def poll(job_id: str, *, config: Optional[Config] = None) -> dict:
    config = config or Config.from_env()
    work = _work_dir(config, job_id)
    script = f'''set -u
{_assign_path(work)}
pid=$(cat "$JD/transfer.pid" 2>/dev/null || echo 0)
kill -0 "$pid" 2>/dev/null && echo ALIVE=1 || echo ALIVE=0
echo "RC=$(cat "$JD/transfer.rc" 2>/dev/null || echo NONE)"
'''
    rc, out, err = run(script, config=config)
    if rc != 0:
        # SSH 暂时不通不等于远端任务失败，保持在途让下一轮重试。
        raise JiuzhangError(f"查询九章任务失败：{(err or out)[:240]}")
    alive = "ALIVE=1" in out
    match = re.search(r"RC=(\S+)", out)
    raw = match.group(1) if match else "NONE"
    if raw != "NONE" and raw.lstrip("-").isdigit():
        code = int(raw)
        # 正常 worker 已经通过 EXIT trap 清理；这里再做一次幂等兜底，覆盖
        # 旧版本 worker、trap 被中断以及管理员手工结束进程等情况。
        try:
            _cleanup_config(_config_path(job_id), config=config)
        except JiuzhangError:
            # 查询结果不能因为清理 SSH 短暂失败而丢失，下一轮继续重试清理。
            pass
        return {
            "status": "DONE" if code in (0, 24) else "FAILED",
            "done": code in (0, 24),
            "failed": code not in (0, 24),
            "error": "" if code in (0, 24) else f"九章 ossutil 退出码 {code}",
        }
    if alive:
        return {"status": "RUNNING", "done": False, "failed": False, "error": ""}
    return {
        "status": "FAILED",
        "done": False,
        "failed": True,
        "error": "九章任务进程已退出但没有退出码",
    }

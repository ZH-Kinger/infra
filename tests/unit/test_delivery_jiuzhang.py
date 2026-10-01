import base64
import re

import pytest

from delivery import jiuzhang
from delivery import catalog, flows
from delivery.errors import DeliveryError


def _plan(direction="oss->jz"):
    if direction == "oss->jz":
        return {
            "engine": "jiuzhang",
            "direction": direction,
            "src": {"scheme": "oss", "bucket": "wuji-bucket-hangzhou", "prefix": "raw/run/"},
            "dest": {
                "scheme": "jz",
                "bucket": "jz-b200",
                "prefix": "wuji-data-tran/aliyun/raw/run/",
            },
        }
    return {
        "engine": "jiuzhang",
        "direction": direction,
        "src": {"scheme": "jz", "bucket": "jz-b200", "prefix": "processed/run/"},
        "dest": {
            "scheme": "oss",
            "bucket": "wuji-data-tran",
            "prefix": "alayanew/teleop/arm/20260930-run/",
        },
    }


def test_work_dir_keeps_home_expandable():
    cfg = jiuzhang.Config(work_dir="$HOME/.panel_jiuzhang_jobs")
    assert jiuzhang._assign_path(jiuzhang._work_dir(cfg, "panel-REQ-1")) == (
        'JD="$HOME/.panel_jiuzhang_jobs/panel-REQ-1"'
    )


def test_submit_uses_base64_and_does_not_embed_raw_nested_shell(monkeypatch):
    seen = {}
    def fake_run(script, **kwargs):
        seen["script"] = script
        return 0, "LAUNCHED", ""

    monkeypatch.setattr(jiuzhang, "run", fake_run)
    monkeypatch.setattr(jiuzhang, "_install_config", lambda *args, **kwargs: None)
    jiuzhang.submit(
        _plan(),
        "panel-REQ-1",
        config=jiuzhang.Config(host="jz"),
        credentials={"access_key_id": "ak-test", "access_key_secret": "sk-test"},
    )
    script = seen["script"]
    token = re.search(r"echo (\S+) \| base64 -d", script).group(1)
    inner = base64.b64decode(token).decode()
    assert "ossutil cp -r oss://wuji-bucket-hangzhou/raw/run/" in inner
    assert '--checkpoint-dir "$JD/ckpt"' in inner
    assert "trap 'rm -f -- /tmp/wuji-panel-jiuzhang-panel-REQ-1.ini' EXIT" in inner
    assert "JD=\"$HOME" in script


def test_submit_cleans_config_for_already_done_job(monkeypatch):
    cleaned = []
    monkeypatch.setattr(jiuzhang, "_install_config", lambda *args, **kwargs: None)
    monkeypatch.setattr(jiuzhang, "run", lambda *args, **kwargs: (0, "ALREADY_DONE rc=0", ""))
    monkeypatch.setattr(
        jiuzhang, "_cleanup_config", lambda path, **kwargs: cleaned.append(path)
    )
    jiuzhang.submit(
        _plan(),
        "panel-REQ-2",
        config=jiuzhang.Config(host="jz"),
        credentials={"access_key_id": "ak-test", "access_key_secret": "sk-test"},
    )
    assert cleaned == ["/tmp/wuji-panel-jiuzhang-panel-REQ-2.ini"]


@pytest.mark.parametrize("rc,done", [(0, True), (24, True), (1, False)])
def test_poll_rc_semantics(monkeypatch, rc, done):
    monkeypatch.setattr(jiuzhang, "run", lambda *args, **kwargs: (0, f"ALIVE=0\nRC={rc}\n", ""))
    monkeypatch.setattr(jiuzhang, "_cleanup_config", lambda *args, **kwargs: None)
    got = jiuzhang.poll("panel-REQ-1", config=jiuzhang.Config(host="jz"))
    assert got["done"] is done
    assert got["failed"] is (not done)


def test_return_command_targets_alayanew_bucket():
    cmd = jiuzhang._command(_plan("jz->oss"), jiuzhang.Config(), "/tmp/job")
    assert "oss://wuji-data-tran/alayanew/teleop/arm/20260930-run/" in cmd
    assert "/root/nas/processed/run" in cmd


def test_remote_path_rejects_escape():
    with pytest.raises(jiuzhang.JiuzhangError):
        jiuzhang.remote_dir(jiuzhang.Config(), {"scheme": "jz", "prefix": "../etc/"})


def test_flow_validation_accepts_only_registered_jz_cluster():
    cat = catalog.load("identity/request-templates.json")
    template = next(row for row in cat.templates if row.id == "oss-move")
    validator = flows.Flows.__new__(flows.Flows)
    clean, _ = validator._validate_transfer(
        template,
        {
            "source": "oss://wuji-bucket-hangzhou/raw/run/",
            "dest": "jz://jz-b200/wuji-data-tran/aliyun/raw/run/",
            "task_name": "jz-test-transfer",
            "overwrite": "skip",
        },
        "test",
    )
    assert clean["dest"].startswith("jz://jz-b200/")

    with pytest.raises(DeliveryError, match="文件系统"):
        validator._validate_transfer(
            template,
            {
                "source": "oss://wuji-bucket-hangzhou/raw/run/",
                "dest": "jz://unknown-cluster/raw/run/",
                "task_name": "jz-test-transfer",
                "overwrite": "skip",
            },
            "test",
        )

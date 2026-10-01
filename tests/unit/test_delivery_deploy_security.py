import os
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_oauth2_proxy_session_cookie_defaults_are_secure():
    config = (ROOT / "deploy/panel/oauth2-proxy.example.yaml").read_text(encoding="utf-8")
    assert "--cookie-secure=true" in config
    assert "--cookie-httponly=true" in config
    assert "--cookie-samesite=lax" in config
    assert "--cookie-expire=8h" in config


def test_identity_backup_is_private_and_networkless(tmp_path):
    script = (ROOT / "deploy/panel/backup-identity.sh").read_text(encoding="utf-8")
    service = (ROOT / "deploy/panel/delivery-backup.service").read_text(encoding="utf-8")
    assert "umask 077" in script
    assert 'chmod 600 "$out"' in script
    for setting in (
        "User=root",
        "Group=root",
        "PrivateNetwork=yes",
        "PrivateDevices=yes",
        "ProtectKernelTunables=yes",
        "ProtectControlGroups=yes",
        "ProtectSystem=strict",
        "ReadWritePaths=/var/backups/delivery",
    ):
        assert setting in service

    source = tmp_path / "identity"
    source.mkdir()
    (source / "people.json").write_text('{"people":[]}', encoding="utf-8")
    (source / "tickets.json").write_text('{"tickets":[]}', encoding="utf-8")
    (source / "people.lock").write_text("locked", encoding="utf-8")
    backup = tmp_path / "backups"
    result = subprocess.run(  # noqa: S603 — fixed repository script, disposable fixtures
        ["/bin/sh", str(ROOT / "deploy/panel/backup-identity.sh")],
        env={
            **os.environ,
            "DELIVERY_IDENTITY_DIR": str(source),
            "DELIVERY_BACKUP_DIR": str(backup),
        },
        capture_output=True,
        text=True,
        check=True,
    )
    assert "已备份" in result.stdout
    archives = list(backup.glob("identity-*.tar.gz"))
    assert len(archives) == 1
    assert backup.stat().st_mode & 0o777 == 0o700
    assert archives[0].stat().st_mode & 0o777 == 0o600
    with tarfile.open(archives[0]) as archive:
        assert "identity/people.lock" not in archive.getnames()
        assert archive.extractfile("identity/people.json").read() == b'{"people":[]}'
        assert archive.extractfile("identity/tickets.json").read() == b'{"tickets":[]}'


def test_service_token_rotation_is_fail_closed_and_documented():
    server = (ROOT / "src/delivery/server.py").read_text(encoding="utf-8")
    docs = (ROOT / "docs/collab/planning/service-access-mlflow.md").read_text(encoding="utf-8")
    assert "st.st_mode & 0o077" in server
    assert "tokens" in docs and "新旧并存" in docs
    assert "删旧的" in docs

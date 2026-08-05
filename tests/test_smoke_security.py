"""Minimal smoke tests for local auth, secret store, and shell denylist."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Keep scratch under the repo to avoid Windows Temp permission issues with pytest tmp_path
_SCRATCH = ROOT / "tests" / ".scratch"


def setup_module() -> None:
    _SCRATCH.mkdir(parents=True, exist_ok=True)


def test_loopback_bind():
    from metateam.services.local_auth import is_loopback_bind, is_loopback_host

    assert is_loopback_bind("127.0.0.1")
    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("localhost")
    assert not is_loopback_bind("0.0.0.0")
    assert not is_loopback_host("8.8.8.8")


def test_token_roundtrip():
    from metateam.services import local_auth

    path = _SCRATCH / ".local_token_test"
    if path.exists():
        path.unlink()
    local_auth.TOKEN_PATH = path
    local_auth._cached = None
    t1 = local_auth.load_or_create_token()
    local_auth._cached = None
    t2 = local_auth.load_or_create_token()
    assert t1 == t2
    assert local_auth.token_matches(t1)
    assert not local_auth.token_matches("nope")
    path.unlink(missing_ok=True)
    local_auth._cached = None


def test_secret_encrypt_roundtrip():
    from metateam.services import secret_store

    path = _SCRATCH / ".secret_key_test"
    if path.exists():
        path.unlink()
    secret_store.SECRET_PATH = path
    secret_store._fernet = None
    enc = secret_store.encrypt_secret("sk-test-key")
    assert enc.startswith("enc:")
    assert secret_store.decrypt_secret(enc) == "sk-test-key"
    assert secret_store.decrypt_secret("plain") == "plain"
    path.unlink(missing_ok=True)
    secret_store._fernet = None


def test_dangerous_shell_blocked():
    from metateam.runtime.tools import _DANGEROUS_SHELL_RE

    assert _DANGEROUS_SHELL_RE.search("rm -rf /")
    assert _DANGEROUS_SHELL_RE.search("Remove-Item -Recurse C:\\Windows")
    assert not _DANGEROUS_SHELL_RE.search("npm run build")


def test_shell_argv_shape():
    from metateam.runtime.tools import _shell_argv
    import os

    argv = _shell_argv("echo hi")
    if os.name == "nt":
        assert argv[0] == "powershell.exe"
        assert "-Command" in argv
        assert "echo hi" in argv[-1]
        assert "UTF8Encoding" in argv[-1]
    else:
        assert argv[:2] == ["/bin/bash", "-lc"]

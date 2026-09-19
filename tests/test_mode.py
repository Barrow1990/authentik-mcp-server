"""Tests for the read/write mode split — AUTHENTIK_MCP_MODE.

These run server.py's import in a fresh subprocess rather than importing it
in-process, because the mode split takes effect at *import time* (whether
set_user_active gets defined at all) and this test process's own `server`
module has already been imported once by conftest.py with mode=write —
reusing it here couldn't observe what happens on a fresh import with a
different mode.
"""

import subprocess
import sys
from pathlib import Path

SERVER_DIR = Path(__file__).parent.parent


def _run(mode: str | None, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = {
        "PATH": "/usr/bin:/bin",
        "AUTHENTIK_URL": "http://test-authentik:9000",
        "AUTHENTIK_API_TOKEN": "test-api-token",
    }
    if mode is not None:
        env["AUTHENTIK_MCP_MODE"] = mode
    env.update(extra_env or {})

    code = (
        "import server; "
        "print('HAS_WRITE_TOOL=' + str(hasattr(server, 'set_user_active'))); "
        "print('MODE=' + server.AUTHENTIK_MCP_MODE); "
        "print('PORT=' + str(server.MCP_PORT)); "
        "print('SERVER_NAME=' + server.mcp.name)"
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=SERVER_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_default_mode_is_read():
    result = _run(mode=None)
    assert result.returncode == 0, result.stderr
    assert "MODE=read" in result.stdout


def test_read_mode_has_no_write_tool():
    result = _run(mode="read")
    assert result.returncode == 0, result.stderr
    assert "HAS_WRITE_TOOL=False" in result.stdout
    assert "PORT=8937" in result.stdout
    assert "SERVER_NAME=authentik-read" in result.stdout


def test_write_mode_has_write_tool():
    result = _run(mode="write")
    assert result.returncode == 0, result.stderr
    assert "HAS_WRITE_TOOL=True" in result.stdout
    assert "PORT=8942" in result.stdout
    assert "SERVER_NAME=authentik-write" in result.stdout


def test_explicit_mcp_port_overrides_mode_default():
    result = _run(mode="read", extra_env={"MCP_PORT": "9999"})
    assert result.returncode == 0, result.stderr
    assert "PORT=9999" in result.stdout


def test_invalid_mode_exits_nonzero_with_clear_error():
    result = _run(mode="delete-everything")
    assert result.returncode != 0
    assert "AUTHENTIK_MCP_MODE must be 'read' or 'write'" in result.stderr


def test_mode_scoped_token_takes_priority_over_plain_fallback():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import server; print('TOKEN=' + server.AUTHENTIK_API_TOKEN)",
        ],
        cwd=SERVER_DIR,
        env={
            "PATH": "/usr/bin:/bin",
            "AUTHENTIK_URL": "http://test-authentik:9000",
            "AUTHENTIK_MCP_MODE": "read",
            "AUTHENTIK_API_TOKEN": "plain-fallback-token",
            "AUTHENTIK_API_TOKEN_READ": "read-scoped-token",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "TOKEN=read-scoped-token" in result.stdout


def test_missing_token_of_either_kind_exits_nonzero():
    result = subprocess.run(
        [sys.executable, "-c", "import server"],
        cwd=SERVER_DIR,
        env={
            "PATH": "/usr/bin:/bin",
            "AUTHENTIK_URL": "http://test-authentik:9000",
            "AUTHENTIK_MCP_MODE": "read",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "AUTHENTIK_API_TOKEN_READ" in result.stderr

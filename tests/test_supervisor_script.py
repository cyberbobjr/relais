from __future__ import annotations

import json
import os
import signal
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SUPERVISOR_SCRIPT = REPO_ROOT / "supervisor.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _create_stale_socket(socket_path: Path) -> None:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(socket_path))
    finally:
        sock.close()


def _fake_supervisorctl_script() -> str:
    return """#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path


def _load_state() -> dict[str, object]:
    return json.loads(Path(os.environ[\"SUPERVISOR_TEST_STATE\"]).read_text(encoding=\"utf-8\"))


def _save_state(state: dict[str, object]) -> None:
    Path(os.environ[\"SUPERVISOR_TEST_STATE\"]).write_text(json.dumps(state), encoding=\"utf-8\")


def main() -> int:
    args = sys.argv[1:]
    if len(args) >= 2 and args[0] == \"-c\":
        args = args[2:]

    state = _load_state()
    command = args[0]

    if command == \"status\":
        if state[\"running\"]:
            print(\"core:fake RUNNING\")
            return 0
        return 1

    if command == \"pid\":
        if state[\"running\"]:
            print(state[\"pid\"])
            return 0
        return 1

    if command == \"shutdown\":
        if state[\"running\"]:
            os.kill(int(state[\"pid\"]), signal.SIGTERM)
            state[\"running\"] = False
            _save_state(state)
            print(\"Shut down\")
            return 0
        return 1

    if command == \"start\":
        if state[\"running\"]:
            print(\"all: started\")
            return 0
        return 1

    return 2


raise SystemExit(main())
"""


def _fake_supervisord_script() -> str:
    return """#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path


def main() -> int:
    state_path = Path(os.environ[\"SUPERVISOR_TEST_STATE\"])
    state = json.loads(state_path.read_text(encoding=\"utf-8\"))
    repo_root = Path(os.environ[\"SUPERVISOR_TEST_ROOT\"])
    socket_path = repo_root / ".relais" / "supervisor.sock"
    pid_path = repo_root / ".relais" / "supervisord.pid"

    with open(os.devnull, \"rb\") as devnull_in, open(os.devnull, \"ab\") as devnull_out:
        proc = subprocess.Popen(
            [\"sleep\", \"5\"],
            stdin=devnull_in,
            stdout=devnull_out,
            stderr=devnull_out,
            start_new_session=True,
        )
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(socket_path))
    finally:
        sock.close()

    pid_path.write_text(f\"{proc.pid}\\n\", encoding=\"utf-8\")
    state.update({\"running\": True, \"pid\": proc.pid})
    state_path.write_text(json.dumps(state), encoding=\"utf-8\")
    return 0


raise SystemExit(main())
"""


def _prepare_fake_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    script_path = tmp_path / "supervisor.sh"
    script_path.write_text(SUPERVISOR_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    script_path.chmod(SUPERVISOR_SCRIPT.stat().st_mode)

    (tmp_path / ".relais" / "logs").mkdir(parents=True)
    (tmp_path / "supervisord.conf").write_text("[supervisord]\n", encoding="utf-8")

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "supervisorctl", _fake_supervisorctl_script())
    _write_executable(fake_bin / "supervisord", _fake_supervisord_script())

    state_path = tmp_path / "state.json"
    return script_path, fake_bin, state_path


def _make_short_tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="relsup-", dir="/tmp"))


def _spawn_orphan_sleep() -> int:
    result = subprocess.run(
        ["sh", "-c", "sleep 5 >/dev/null 2>&1 & echo $!"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(result.stdout.strip())


def _spawn_orphan_supervisord_like_process(config_path: Path) -> int:
    command = (
        f"{sys.executable} -c 'import time; time.sleep(5)' "
        f"supervisord -c '{config_path}' >/dev/null 2>&1 & echo $!"
    )
    result = subprocess.run(
        ["sh", "-c", command],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(result.stdout.strip())


def _run_script(script_path: Path, fake_bin: Path, state_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["SUPERVISOR_TEST_STATE"] = str(state_path)
    env["SUPERVISOR_TEST_ROOT"] = str(script_path.parent)
    return subprocess.run(
        [str(script_path), *args],
        cwd=script_path.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_stop_all_cleans_stale_socket_after_shutdown(tmp_path: Path) -> None:
    repo_root = _make_short_tmpdir()
    script_path, fake_bin, state_path = _prepare_fake_repo(repo_root)
    sleeper_pid = _spawn_orphan_sleep()
    socket_path = repo_root / ".relais" / "supervisor.sock"
    pid_path = repo_root / ".relais" / "supervisord.pid"

    try:
        state_path.write_text(
            json.dumps({"running": True, "pid": sleeper_pid}),
            encoding="utf-8",
        )
        _create_stale_socket(socket_path)
        pid_path.write_text(f"{sleeper_pid}\n", encoding="utf-8")

        result = _run_script(script_path, fake_bin, state_path, "stop", "all")

        assert result.returncode == 0, result.stderr
        assert "supervisord stopped." in result.stdout
        assert "Timeout:" not in result.stderr
        assert not socket_path.exists()
        assert not pid_path.exists()
    finally:
        try:
            os.kill(sleeper_pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        shutil.rmtree(repo_root, ignore_errors=True)


def test_restart_all_succeeds_when_shutdown_leaves_stale_socket(tmp_path: Path) -> None:
    repo_root = _make_short_tmpdir()
    script_path, fake_bin, state_path = _prepare_fake_repo(repo_root)
    original_pid = _spawn_orphan_sleep()
    socket_path = repo_root / ".relais" / "supervisor.sock"
    pid_path = repo_root / ".relais" / "supervisord.pid"

    try:
        state_path.write_text(
            json.dumps({"running": True, "pid": original_pid}),
            encoding="utf-8",
        )
        _create_stale_socket(socket_path)
        pid_path.write_text(f"{original_pid}\n", encoding="utf-8")

        result = _run_script(script_path, fake_bin, state_path, "restart", "all")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        restarted_pid = int(state["pid"])

        assert result.returncode == 0, result.stderr
        assert state["running"] is True
        assert restarted_pid != original_pid
        assert socket_path.exists()
        assert pid_path.read_text(encoding="utf-8").strip() == str(restarted_pid)
    finally:
        try:
            os.kill(original_pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        current_state = json.loads(state_path.read_text(encoding="utf-8"))
        current_pid = int(current_state["pid"])
        if current_state["running"]:
            try:
                os.kill(current_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        shutil.rmtree(repo_root, ignore_errors=True)


def test_stop_all_kills_orphaned_supervisord_without_socket(tmp_path: Path) -> None:
    repo_root = _make_short_tmpdir()
    script_path, fake_bin, state_path = _prepare_fake_repo(repo_root)
    orphan_pid = _spawn_orphan_supervisord_like_process(repo_root / "supervisord.conf")

    try:
        state_path.write_text(json.dumps({"running": False, "pid": orphan_pid}), encoding="utf-8")

        result = _run_script(script_path, fake_bin, state_path, "stop", "all")

        assert result.returncode == 0, result.stderr
        assert f"Stopping orphan supervisord (PID {orphan_pid})..." in result.stderr
        with pytest.raises(ProcessLookupError):
            os.kill(orphan_pid, 0)
    finally:
        try:
            os.kill(orphan_pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        shutil.rmtree(repo_root, ignore_errors=True)
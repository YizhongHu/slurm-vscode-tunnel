#!/usr/bin/env python3.11
import argparse
import json
import os
import pathlib
import pty
import re
import select
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Dict, Optional, Set, Tuple

from codeserver_lib import load_config, merged_env
from codeserver_relay import READY_PATTERNS


MAX_RESPAWN_RESTARTS = 3
RESPAWN_RESTART_DELAY_SECONDS = 5
TERMINATE_GRACE_SECONDS = 5
STALE_SERVER_CHECK_INTERVAL_SECONDS = 10
TUNNEL_STATUS_CHECK_INTERVAL_SECONDS = 5
TUNNEL_STATUS_TIMEOUT_SECONDS = 10
UPGRADE_GRACE_SECONDS = 180

UPGRADE_HINT_PATTERNS = [
    re.compile(r"\bupdat(?:e|ing)\b.*\bCLI\b", re.IGNORECASE),
    re.compile(r"\bCLI\b.*\bupdat(?:e|ing)\b", re.IGNORECASE),
    re.compile(r"\brespawn requested\b", re.IGNORECASE),
]


@dataclass(frozen=True)
class TunnelStatus:
    connected: bool
    summary: str


def extract_code_commit(version_text: str) -> Optional[str]:
    match = re.search(r"\(commit ([0-9a-f]+)\)", version_text)
    return match.group(1) if match else None


def current_code_commit(
    code_bin: str, env: Dict[str, str], quiet: bool = False
) -> Optional[str]:
    proc = subprocess.run(
        [code_bin, "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        if not quiet:
            msg = proc.stderr.strip() or proc.stdout.strip() or "unknown error"
            print(f"[codeserver_inner] failed to read VS Code CLI version: {msg}")
            sys.stdout.flush()
        return None
    return extract_code_commit(proc.stdout)


def parse_tunnel_status(status_text: str) -> Optional[TunnelStatus]:
    try:
        raw = json.loads(status_text)
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None

    tunnel = raw.get("tunnel")
    if not isinstance(tunnel, dict):
        return None

    state = str(tunnel.get("tunnel") or tunnel.get("state") or "").strip()
    name = str(tunnel.get("name") or "").strip()
    reason = str(tunnel.get("last_fail_reason") or "").strip()
    connected = state.lower() == "connected" or tunnel.get("connected") is True

    pieces = []
    if name:
        pieces.append(f"name={name}")
    if state:
        pieces.append(f"state={state}")
    if reason:
        pieces.append(f"last_fail_reason={reason}")
    summary = " ".join(pieces) or "status=unknown"
    return TunnelStatus(connected=connected, summary=summary)


def query_tunnel_status(code_bin: str, env: Dict[str, str]) -> Optional[TunnelStatus]:
    proc = subprocess.run(
        [code_bin, "tunnel", "status"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
        timeout=TUNNEL_STATUS_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        return None
    return parse_tunnel_status(proc.stdout)


def has_upgrade_hint(text: str) -> bool:
    return any(pattern.search(text) for pattern in UPGRADE_HINT_PATTERNS)


def process_cmdline(pid: int) -> str:
    try:
        data = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return data.replace(b"\0", b" ").decode("utf-8", errors="replace")


def terminate_pid_process_group(pid: int, grace_seconds: float = 5.0) -> None:
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return

    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)

    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def active_server_commit(servers_dir: Optional[pathlib.Path] = None) -> Optional[str]:
    """Commit of the server the CLI is currently using.

    The VS Code CLI keeps ``~/.vscode/cli/servers/lru.json`` ordered
    most-recently-used first, so its first entry tracks the live server even
    after the CLI self-updates mid-session.
    """
    if servers_dir is None:
        servers_dir = pathlib.Path.home() / ".vscode" / "cli" / "servers"
    try:
        entries = json.loads((servers_dir / "lru.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if isinstance(entry, str) and entry.startswith("Stable-"):
            return entry.removeprefix("Stable-")
    return None


def protected_server_commits(code_bin: str, env: Dict[str, str]) -> Set[str]:
    """Commits whose servers must never be reaped.

    Combines the commit the CLI reports (``code --version``) with the
    most-recently-used server from ``lru.json``. Requiring both signals to
    agree before a server is considered stale means a momentarily stale read of
    either one cannot cause us to kill the live server.
    """
    commits: Set[str] = set()
    cli_commit = current_code_commit(code_bin, env, quiet=True)
    if cli_commit:
        commits.add(cli_commit)
    active = active_server_commit()
    if active:
        commits.add(active)
    return commits


def is_stale_server_commit(commit: str, protected: Set[str]) -> bool:
    """A server is stale only when we positively know the live commit(s) and
    this is not one of them (staging builds are never reaped)."""
    if not protected:
        return False
    if commit in protected or commit.endswith(".staging"):
        return False
    return True


def cleanup_stale_server_processes(
    code_bin: Optional[str],
    env: Dict[str, str],
    *,
    upgrade_grace_until: float = 0.0,
    now: Optional[float] = None,
) -> None:
    if not code_bin:
        return
    if now is None:
        now = time.monotonic()
    if now < upgrade_grace_until:
        return
    protected = protected_server_commits(code_bin, env)
    if not protected:
        # Could not determine the live server; never reap blindly.
        return
    servers_dir = pathlib.Path.home() / ".vscode" / "cli" / "servers"
    if not servers_dir.exists():
        return

    for server_dir in servers_dir.glob("Stable-*"):
        commit = server_dir.name.removeprefix("Stable-")
        if not is_stale_server_commit(commit, protected):
            continue
        pid_path = server_dir / "pid.txt"
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        cmdline = process_cmdline(pid)
        if str(server_dir) not in cmdline:
            continue
        print(
            f"[codeserver_inner] terminating stale VS Code server "
            f"pid={pid} commit={commit}; protected={sorted(protected)}"
        )
        sys.stdout.flush()
        terminate_pid_process_group(pid, TERMINATE_GRACE_SECONDS)


def cancel_previous_job(job_id: str) -> None:
    proc = subprocess.run(
        ["scancel", str(job_id)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode == 0:
        print(f"[relay] canceled previous job {job_id}")
    else:
        msg = proc.stderr.strip() or proc.stdout.strip() or "unknown scancel error"
        print(f"[relay] failed to cancel previous job {job_id}: {msg}")
    sys.stdout.flush()


def terminate_process_group(proc: subprocess.Popen, grace_seconds: float = 5.0) -> None:
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return

    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.1)

    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def forward_pty_output(
    argv,
    env: Dict[str, str],
    tunnel_log: pathlib.Path,
    previous_job_id: Optional[str],
    ready_timeout: int,
    code_bin: Optional[str],
) -> Tuple[int, bool]:
    master_fd, slave_fd = pty.openpty()
    try:
        proc = subprocess.Popen(
            argv,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
            close_fds=True,
            start_new_session=True,
        )
    finally:
        os.close(slave_fd)

    ready = False
    previous_canceled = False
    respawn_requested = False
    deadline = time.monotonic() + max(0, ready_timeout)
    next_stale_check = time.monotonic()
    next_status_check = time.monotonic()
    upgrade_grace_until = 0.0
    protected_snapshot = protected_server_commits(code_bin, env) if code_bin else set()
    last_status_summary = ""
    buffer = ""

    with tunnel_log.open("ab") as logf:
        while True:
            now = time.monotonic()
            if code_bin and now >= next_status_check:
                try:
                    status = query_tunnel_status(code_bin, env)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    status = None
                    if last_status_summary != "status query failed":
                        print(f"[relay] tunnel status query failed: {exc}")
                        sys.stdout.flush()
                        last_status_summary = "status query failed"
                if status and status.summary != last_status_summary:
                    print(f"[relay] tunnel status: {status.summary}")
                    sys.stdout.flush()
                    last_status_summary = status.summary
                if status and status.connected and not ready:
                    ready = True
                    print("[relay] readiness detected from code tunnel status")
                    sys.stdout.flush()
                if previous_job_id and ready and not previous_canceled:
                    cancel_previous_job(previous_job_id)
                    previous_canceled = True

                current_protected = protected_server_commits(code_bin, env)
                if protected_snapshot and current_protected - protected_snapshot:
                    upgrade_grace_until = max(
                        upgrade_grace_until, now + UPGRADE_GRACE_SECONDS
                    )
                    print(
                        "[codeserver_inner] VS Code server commit changed; "
                        f"suppressing stale cleanup for {UPGRADE_GRACE_SECONDS}s"
                    )
                    sys.stdout.flush()
                if current_protected:
                    protected_snapshot = current_protected
                next_status_check = now + TUNNEL_STATUS_CHECK_INTERVAL_SECONDS

            if not ready and now >= next_stale_check:
                cleanup_stale_server_processes(
                    code_bin,
                    env,
                    upgrade_grace_until=upgrade_grace_until,
                    now=now,
                )
                next_stale_check = now + STALE_SERVER_CHECK_INTERVAL_SECONDS

            ready_fds, _, _ = select.select([master_fd], [], [], 1.0)
            if not ready_fds:
                if proc.poll() is not None:
                    break
                continue

            try:
                chunk = os.read(master_fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            logf.write(chunk)
            logf.flush()

            text = chunk.decode("utf-8", errors="replace")
            buffer = (buffer + text)[-8000:]
            if code_bin and has_upgrade_hint(text):
                upgrade_grace_until = max(
                    upgrade_grace_until, time.monotonic() + UPGRADE_GRACE_SECONDS
                )
                print(
                    "[codeserver_inner] VS Code CLI upgrade activity detected; "
                    f"suppressing stale cleanup for {UPGRADE_GRACE_SECONDS}s"
                )
                sys.stdout.flush()
            if (
                not code_bin
                and not ready
                and any(pattern.search(buffer) for pattern in READY_PATTERNS)
            ):
                ready = True
                print("[relay] readiness detected")
                sys.stdout.flush()
            if re.search(r"\brespawn requested\b", buffer, re.IGNORECASE):
                respawn_requested = True
                msg = (
                    "[codeserver_inner] VS Code CLI requested respawn; "
                    "terminating tunnel process group for a clean relaunch"
                )
                print(msg)
                sys.stdout.flush()
                logf.write((msg + "\n").encode("utf-8"))
                logf.flush()
                terminate_process_group(proc, TERMINATE_GRACE_SECONDS)
                break
            if previous_job_id and ready and not previous_canceled:
                cancel_previous_job(previous_job_id)
                previous_canceled = True
            if previous_job_id and not ready and time.monotonic() > deadline:
                print(
                    f"[relay] readiness not detected after {ready_timeout}s; "
                    f"leaving previous job {previous_job_id} alive"
                )
                sys.stdout.flush()
                previous_canceled = True

    rc = proc.wait()
    os.close(master_fd)

    if rc < 0:
        rc = 128 + abs(rc)
    return rc, respawn_requested


def supervise_pty_output(
    argv,
    env: Dict[str, str],
    tunnel_log: pathlib.Path,
    previous_job_id: Optional[str],
    ready_timeout: int,
    code_bin: Optional[str] = None,
) -> int:
    for restart_idx in range(MAX_RESPAWN_RESTARTS + 1):
        cleanup_stale_server_processes(code_bin, env)
        if restart_idx:
            print(
                f"[codeserver_inner] relaunching tunnel after respawn "
                f"({restart_idx}/{MAX_RESPAWN_RESTARTS})"
            )
            sys.stdout.flush()
        rc, respawn_requested = forward_pty_output(
            argv,
            env,
            tunnel_log,
            previous_job_id,
            ready_timeout,
            code_bin,
        )
        if not respawn_requested:
            return rc
        if restart_idx >= MAX_RESPAWN_RESTARTS:
            print(
                "[codeserver_inner] respawn restart limit reached; "
                "exiting instead of looping forever"
            )
            sys.stdout.flush()
            return 1
        time.sleep(RESPAWN_RESTART_DELAY_SECONDS)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--session-dir", required=True)
    ap.add_argument("--run-log", required=True)
    ap.add_argument("--tunnel-log", required=True)
    ap.add_argument("--previous-job-id")
    ap.add_argument("--relay-ready-timeout", type=int, default=300)
    ap.add_argument("--test-command")
    args = ap.parse_args()

    cfg = load_config(pathlib.Path(args.config))
    session_dir = pathlib.Path(args.session_dir)
    tunnel_log = pathlib.Path(args.tunnel_log)

    session_dir.mkdir(parents=True, exist_ok=True)
    tunnel_log.parent.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.update(merged_env(cfg, args.profile))

    if args.test_command:
        argv = ["bash", "-lc", args.test_command]
        code_bin = None
    else:
        argv = [cfg["code_bin"]] + cfg["code_tunnel_args"]
        code_bin = cfg["code_bin"]

    print(f"[codeserver_inner] host={os.uname().nodename}")
    print(f"[codeserver_inner] profile={args.profile}")
    print(f"[codeserver_inner] session_dir={session_dir}")
    print(f"[codeserver_inner] tunnel_log={tunnel_log}")
    print(f"[codeserver_inner] command={shlex.join(argv)}")
    if code_bin:
        protected = protected_server_commits(code_bin, env)
        if protected:
            print(
                f"[codeserver_inner] protected_server_commits={sorted(protected)}"
            )
    if args.previous_job_id:
        print(f"[relay] previous_job_id={args.previous_job_id}")
        print(f"[relay] ready_timeout={args.relay_ready_timeout}s")
    sys.stdout.flush()

    return supervise_pty_output(
        argv,
        env,
        tunnel_log,
        args.previous_job_id,
        args.relay_ready_timeout,
        code_bin,
    )


if __name__ == "__main__":
    raise SystemExit(main())

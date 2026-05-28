#!/usr/bin/env python3
import argparse
import os
import pathlib
import pty
import shlex
import signal
import subprocess
import sys
import time
from typing import Dict, Optional, Tuple

from codeserver_lib import load_config, merged_env
from codeserver_relay import READY_PATTERNS


MAX_RESPAWN_RESTARTS = 3
RESPAWN_RESTART_DELAY_SECONDS = 5
TERMINATE_GRACE_SECONDS = 5


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
    buffer = ""

    with tunnel_log.open("ab") as logf:
        while True:
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
            if not ready and any(pattern.search(buffer) for pattern in READY_PATTERNS):
                ready = True
                print("[relay] readiness detected")
                sys.stdout.flush()
            if "respawn requested" in buffer.lower():
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
) -> int:
    for restart_idx in range(MAX_RESPAWN_RESTARTS + 1):
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
    else:
        argv = [cfg["code_bin"]] + cfg["code_tunnel_args"]

    print(f"[codeserver_inner] host={os.uname().nodename}")
    print(f"[codeserver_inner] profile={args.profile}")
    print(f"[codeserver_inner] session_dir={session_dir}")
    print(f"[codeserver_inner] tunnel_log={tunnel_log}")
    print(f"[codeserver_inner] command={shlex.join(argv)}")
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
    )


if __name__ == "__main__":
    raise SystemExit(main())

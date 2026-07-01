#!/usr/bin/env python3.11
import argparse
import datetime as dt
import pathlib
import tomllib
from typing import Optional

from codeserver_lib import (
    ConfigError,
    default_config_path,
    die,
    find_auth_block,
    is_chain_dir,
    load_config,
    load_json,
    parse_slurm_seconds,
    query_job_status,
    resolve_session_dir,
    run_capture,
    state_from_status,
    tail_lines,
)
from codeserver_relay import format_duration as format_chain_duration


TERMINAL_STATES = {
    "BOOT_FAIL",
    "CANCELLED",
    "COMPLETED",
    "DEADLINE",
    "FAILED",
    "NODE_FAIL",
    "OUT_OF_MEMORY",
    "PREEMPTED",
    "REVOKED",
    "SPECIAL_EXIT",
    "TIMEOUT",
}


def print_block(title: str, block: str) -> None:
    print()
    print(f"===== {title} =====")
    print(block)


def format_duration(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    hours, rem = divmod(seconds, 60 * 60)
    minutes, _ = divmod(rem, 60)

    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}min")
    return " ".join(parts)


def parse_slurm_start(value: str) -> Optional[dt.datetime]:
    raw = value.strip()
    if not raw or raw in {"N/A", "Unknown"}:
        return None
    try:
        return dt.datetime.fromisoformat(raw)
    except ValueError:
        return None


def format_pending_reason(value: str) -> str:
    raw = value.strip() or "unknown"
    raw = raw.strip("()").upper()
    return f"({raw})"


def capture_status_cmd(argv: list[str]) -> tuple[int, str, str]:
    try:
        return run_capture(argv)
    except OSError as exc:
        return 127, "", str(exc)


def query_status_line(job_id: Optional[str]) -> str:
    if not job_id:
        return "unknown"

    rc, out, _ = capture_status_cmd(["squeue", "-h", "-j", job_id, "-o", "%T|%R|%S|%M|%l"])
    if rc == 0 and out.strip():
        state, reason, start_text, elapsed_text, limit_text = (
            out.splitlines()[0].split("|", 4)
        )
        state = state.strip()
        if state == "PENDING":
            start = parse_slurm_start(start_text)
            if start is not None:
                remaining = int((start - dt.datetime.now(start.tzinfo)).total_seconds())
                if remaining > 0 and remaining % 60:
                    remaining += 60 - (remaining % 60)
                return (
                    f"{state}, reason: {format_pending_reason(reason)}, "
                    f"{format_duration(remaining)} until start"
                )
            return f"{state}, reason: {format_pending_reason(reason)}"

        elapsed = parse_slurm_seconds(elapsed_text)
        limit = parse_slurm_seconds(limit_text)
        if state == "RUNNING" and elapsed is not None and limit is not None:
            return f"{state} {format_duration(elapsed)}/{format_duration(limit)}"
        return state or "unknown"

    rc, out, _ = capture_status_cmd(
        [
            "sacct",
            "-n",
            "-P",
            "-j",
            job_id,
            "--format=JobIDRaw,State,Elapsed,Timelimit",
        ]
    )
    if rc == 0 and out.strip():
        for line in out.splitlines():
            parts = line.split("|")
            if len(parts) < 4 or parts[0] != job_id:
                continue
            state, elapsed_text, limit_text = parts[1], parts[2], parts[3]
            elapsed = parse_slurm_seconds(elapsed_text)
            limit = parse_slurm_seconds(limit_text)
            if state == "RUNNING" and elapsed is not None and limit is not None:
                return f"{state} {format_duration(elapsed)}/{format_duration(limit)}"
            return state or "unknown"

    return "unknown"


def query_job_progress(job_id: Optional[str]) -> tuple[str, Optional[int], Optional[int]]:
    if not job_id:
        return "unknown", None, None

    rc, out, _ = capture_status_cmd(["squeue", "-h", "-j", job_id, "-o", "%T|%M|%l"])
    if rc == 0 and out.strip():
        state, elapsed_text, limit_text = out.splitlines()[0].split("|", 2)
        return (
            state.strip() or "unknown",
            parse_slurm_seconds(elapsed_text),
            parse_slurm_seconds(limit_text),
        )

    rc, out, _ = capture_status_cmd(
        [
            "sacct",
            "-n",
            "-P",
            "-j",
            job_id,
            "--format=JobIDRaw,State,Elapsed,Timelimit",
        ]
    )
    if rc == 0 and out.strip():
        for line in out.splitlines():
            parts = line.split("|")
            if len(parts) < 4 or parts[0] != job_id:
                continue
            return (
                parts[1] or "unknown",
                parse_slurm_seconds(parts[2]),
                parse_slurm_seconds(parts[3]),
            )

    return "unknown", None, None


def relay_progress(chain: dict) -> tuple[int, int]:
    requested = int(chain.get("requested_time_seconds") or 0)
    used = 0
    for job in chain.get("jobs", []):
        job_id = str(job.get("job_id") or "")
        begin = int(job.get("begin_offset_seconds") or 0)
        duration = int(job.get("duration_seconds") or 0)

        state, elapsed, _limit = (
            ("unknown", None, None)
            if job_id.startswith("DRY-RUN")
            else query_job_progress(job_id)
        )
        state = state.upper()

        if state == "RUNNING":
            used = max(used, begin + min(elapsed or 0, duration))
        elif state in TERMINAL_STATES:
            terminal_elapsed = elapsed if elapsed is not None else duration
            used = max(used, begin + min(terminal_elapsed, duration))

    used = min(used, requested) if requested else used
    remaining = max(0, requested - used)
    return used, remaining


def print_session(session_dir: pathlib.Path) -> int:
    meta_path = session_dir / "meta.json"
    if not meta_path.exists():
        die(f"missing metadata: {meta_path}")

    meta = load_json(meta_path)
    run_log = pathlib.Path(meta["run_log"])
    tunnel_log = pathlib.Path(meta["tunnel_log"])
    job_id: Optional[str] = meta.get("job_id")

    print(f"session:     {meta['session_id']}")
    print(f"status:      {query_status_line(job_id)}")
    print(f"profile:     {meta['profile']}")
    print(f"run log:     {run_log}")
    print(f"tunnel log:  {tunnel_log}")
    print(f"config:      {meta['config_path']}")
    print(f"job id:      {job_id or '-'}")

    auth_found = False
    for title, path in (("auth prompt from run.log", run_log), ("auth prompt from tunnel.log", tunnel_log)):
        block = find_auth_block(path)
        if block:
            print_block(title, block)
            auth_found = True

    print()
    print(f"NEEDS_REAUTH={'yes' if auth_found else 'no'}")

    if not auth_found:
        print()
        print("===== recent run.log =====")
        for line in tail_lines(run_log, 40):
            print(line)
        print()
        print("===== recent tunnel.log =====")
        for line in tail_lines(tunnel_log, 40):
            print(line)
    return 0


def print_chain(chain_dir: pathlib.Path) -> int:
    chain_path = chain_dir / "chain.json"
    chain = load_json(chain_path)
    print(f"relay chain: {chain['chain_id']}")
    print(f"profile:     {chain['profile']}")
    print(f"chain dir:   {chain_dir}")
    print(f"config:      {chain['config_path']}")
    print(f"requested:   {format_chain_duration(int(chain['requested_time_seconds']))}")
    print(f"limit:       {format_chain_duration(int(chain['profile_max_seconds']))}")
    print(f"overlap:     {format_chain_duration(int(chain['relay_overlap_seconds']))}")
    print(f"segments:    {len(chain.get('jobs', []))}")
    used, remaining = relay_progress(chain)
    print(f"used:        {format_chain_duration(used)}")
    print(f"remaining:   {format_chain_duration(remaining)}")
    print()
    print("IDX  JOB_ID       STATE     BEGIN       DURATION  PREV_JOB    LOG")
    for job in chain.get("jobs", []):
        job_id = str(job.get("job_id") or "-")
        status = query_job_status(job_id) if job_id and not job_id.startswith("DRY-RUN") else None
        state = state_from_status(status)
        begin = format_chain_duration(int(job.get("begin_offset_seconds", 0)))
        duration = format_chain_duration(int(job.get("duration_seconds", 0)))
        prev = str(job.get("previous_job_id") or "-")
        print(
            f"{int(job['index']):<4} {job_id:<12} {state:<9} {begin:<11} "
            f"{duration:<9} {prev:<11} {job.get('run_log', '-') }"
        )

    auth_found = False
    for job in chain.get("jobs", []):
        for log_key in ("run_log", "tunnel_log"):
            path = pathlib.Path(job[log_key])
            block = find_auth_block(path)
            if block:
                print_block(f"auth prompt from job-{int(job['index']):03d}/{log_key}", block)
                auth_found = True
    print()
    print(f"NEEDS_REAUTH={'yes' if auth_found else 'no'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Show session status for latest, a profile, session id, chain id, or job id.")
    ap.add_argument("target", nargs="?", default="latest", help="latest, profile name, session id, chain id, or job id.")
    ap.add_argument("--config", default=str(default_config_path()), help="Path to the TOML config file.")
    args = ap.parse_args()

    try:
        cfg = load_config(pathlib.Path(args.config).resolve())
    except (ConfigError, FileNotFoundError, tomllib.TOMLDecodeError) as exc:
        die(f"{exc}. Use --help for usage.", code=2)
    try:
        session_dir = resolve_session_dir(cfg, args.target)
    except FileNotFoundError as exc:
        die(f"{exc}. Use --help for usage.")

    if not session_dir.exists():
        if args.target.isdigit():
            print(f"job id:      {args.target}")
            print(f"status:      {query_status_line(args.target)}")
            return 0
        die(f"no session found for '{args.target}'. Use --help for usage.")
    if is_chain_dir(session_dir):
        return print_chain(session_dir)
    return print_session(session_dir)


if __name__ == "__main__":
    raise SystemExit(main())

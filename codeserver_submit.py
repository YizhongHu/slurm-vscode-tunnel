#!/usr/bin/env python3.11
import argparse
import pathlib
import shlex
import shutil
import subprocess
import sys
import tomllib
from typing import Any, Dict, List, Optional

from codeserver_lib import (
    ConfigError,
    default_config_path,
    die,
    dump_json,
    ensure_root_dirs,
    get_profile,
    load_config,
    profile_names,
    session_id_for,
)
from codeserver_relay import (
    filter_sbatch_args,
    format_duration,
    parse_duration,
    plan_relay,
    profile_time,
    slurm_begin_offset,
)


HOLD_COMMAND = "echo READY; sleep infinity"


def build_sbatch_cmd(
    profile: Dict[str, Any],
    run_log: pathlib.Path,
    batch_script: pathlib.Path,
    duration_seconds: int,
    begin_offset_seconds: int = 0,
) -> List[str]:
    args = filter_sbatch_args(list(profile["sbatch_args"]), ["time", "begin", "output", "error"])
    cmd = ["sbatch", "--parsable"] + args + [f"--time={format_duration(duration_seconds)}"]
    if begin_offset_seconds > 0:
        cmd.append(f"--begin={slurm_begin_offset(begin_offset_seconds)}")
    return cmd + [f"--output={run_log}", f"--error={run_log}", str(batch_script)]


def write_batch_script(
    batch_script: pathlib.Path,
    python_bin: str,
    inner_py: pathlib.Path,
    config_path: pathlib.Path,
    profile_name: str,
    session_dir: pathlib.Path,
    run_log: pathlib.Path,
    tunnel_log: pathlib.Path,
    pre_commands: List[str],
    previous_job_id: Optional[str] = None,
    relay_ready_timeout: Optional[int] = None,
    test_command: Optional[str] = None,
) -> None:
    preamble = ""
    if pre_commands:
        preamble = "\n".join(pre_commands) + "\n"

    extra_args = ""
    if previous_job_id:
        extra_args += f" \\\n  --previous-job-id {shlex.quote(str(previous_job_id))}"
    if relay_ready_timeout is not None:
        extra_args += f" \\\n  --relay-ready-timeout {relay_ready_timeout}"
    if test_command:
        extra_args += f" \\\n  --test-command {shlex.quote(test_command)}"

    body = f"""#!/usr/bin/env bash
set -euo pipefail

echo "[batch] host=$(hostname)"
echo "[batch] start=$(date --iso-8601=seconds)"
echo "[batch] profile={shlex.quote(profile_name)}"
echo "[batch] session_dir={shlex.quote(str(session_dir))}"
echo "[batch] run_log={shlex.quote(str(run_log))}"
echo "[batch] tunnel_log={shlex.quote(str(tunnel_log))}"

{preamble}exec {shlex.quote(python_bin)} {shlex.quote(str(inner_py))} \\
  --config {shlex.quote(str(config_path))} \\
  --profile {shlex.quote(profile_name)} \\
  --session-dir {shlex.quote(str(session_dir))} \\
  --run-log {shlex.quote(str(run_log))} \\
  --tunnel-log {shlex.quote(str(tunnel_log))}{extra_args}
"""
    batch_script.write_text(body, encoding="utf-8")
    batch_script.chmod(0o755)


def submit_cmd(sbatch_cmd: List[str], dry_run: bool, dry_id: str) -> str:
    if dry_run:
        return dry_id
    proc = subprocess.run(
        sbatch_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        die(f"sbatch failed:\n{proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip().split(";", 1)[0]


def batch_python_bin() -> str:
    return sys.executable or shutil.which("python3.11") or shutil.which("python3") or "python3"


def update_current_links(state_dir: pathlib.Path, profile_name: str, target: pathlib.Path) -> None:
    relative_target = pathlib.Path("..") / "logs" / target.name
    for link in (state_dir / "current", state_dir / f"current-{profile_name}"):
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(relative_target)


def print_followups(session_id: str, profile_name: str, job_ids: List[str], dry_run: bool) -> None:
    print()
    print("status:")
    print("  cs status")
    print(f"  cs status {profile_name}")
    print(f"  cs status {session_id}")
    if not dry_run:
        for job_id in job_ids:
            print(f"  cs status {job_id}")
    print("stop:")
    print(f"  cs stop {session_id}")
    if not dry_run:
        for job_id in job_ids:
            print(f"  cs stop {job_id}")


def submit_single(
    cfg: Dict[str, Any],
    config_path: pathlib.Path,
    profile_name: str,
    profile: Dict[str, Any],
    duration: int,
    args: argparse.Namespace,
) -> int:
    root_dir = ensure_root_dirs(cfg)
    state_dir = root_dir / "state"
    session_id = session_id_for(profile_name)
    session_dir = root_dir / "logs" / session_id
    run_log = session_dir / "run.log"
    tunnel_log = session_dir / "tunnel.log"
    meta_json = session_dir / "meta.json"
    batch_script = session_dir / "batch.sh"
    session_dir.mkdir(parents=True, exist_ok=True)

    python_bin = batch_python_bin()
    inner_py = pathlib.Path(__file__).resolve().parent / "codeserver_inner.py"
    ready_timeout = parse_duration(str(profile.get("relay_ready_timeout") or "5m"))
    write_batch_script(
        batch_script,
        python_bin,
        inner_py,
        config_path,
        profile_name,
        session_dir,
        run_log,
        tunnel_log,
        profile["pre_commands"],
        relay_ready_timeout=ready_timeout,
        test_command=args.test_command,
    )
    sbatch_cmd = build_sbatch_cmd(profile, run_log, batch_script, duration)

    meta = {
        "type": "session",
        "session_id": session_id,
        "profile": profile_name,
        "config_path": str(config_path),
        "session_dir": str(session_dir),
        "run_log": str(run_log),
        "tunnel_log": str(tunnel_log),
        "batch_script": str(batch_script),
        "sbatch_cmd": sbatch_cmd,
        "duration_seconds": duration,
        "test_command": args.test_command,
    }
    dump_json(meta_json, meta)
    job_id = submit_cmd(sbatch_cmd, args.dry_run, "DRY-RUN")
    meta["job_id"] = job_id
    dump_json(meta_json, meta)

    if not args.dry_run:
        update_current_links(state_dir, profile_name, session_dir)

    print(f"{'would start' if args.dry_run else 'started'} session: {session_id}")
    print(f"profile:         {profile_name}")
    print(f"requested time:  {format_duration(duration)}")
    print(f"job id:          {job_id}")
    print(f"run log:         {run_log}")
    print(f"tunnel log:      {tunnel_log}")
    if args.dry_run:
        print(f"sbatch command:  {shlex.join(sbatch_cmd)}")
    print_followups(session_id, profile_name, [job_id], args.dry_run)
    return 0


def submit_chain(
    cfg: Dict[str, Any],
    config_path: pathlib.Path,
    profile_name: str,
    profile: Dict[str, Any],
    requested: int,
    max_time: int,
    overlap: int,
    segments: List[Dict[str, int]],
    args: argparse.Namespace,
) -> int:
    root_dir = ensure_root_dirs(cfg)
    state_dir = root_dir / "state"
    chain_id = f"{session_id_for(profile_name)}-relay"
    chain_dir = root_dir / "logs" / chain_id
    chain_dir.mkdir(parents=True, exist_ok=True)
    chain_json = chain_dir / "chain.json"

    python_bin = batch_python_bin()
    inner_py = pathlib.Path(__file__).resolve().parent / "codeserver_inner.py"
    ready_timeout = parse_duration(str(profile.get("relay_ready_timeout") or "5m"))

    print(f"{'would start' if args.dry_run else 'started'} relay chain: {chain_id}")
    print(f"profile:         {profile_name}")
    print(f"requested time:  {format_duration(requested)}")
    print(f"profile limit:   {format_duration(max_time)}")
    print(f"relay overlap:   {format_duration(overlap)}")
    print(f"plan:            {len(segments)} jobs")
    print()
    print("relay chain:")
    for seg in segments:
        start = "now" if seg["begin_offset"] == 0 else f"~+{format_duration(seg['begin_offset'])}"
        print(
            f"  {seg['index']}/{len(segments)}: starts {start}, "
            f"runs {format_duration(seg['duration'])}"
        )

    chain = {
        "type": "relay_chain",
        "chain_id": chain_id,
        "profile": profile_name,
        "config_path": str(config_path),
        "chain_dir": str(chain_dir),
        "requested_time_seconds": requested,
        "profile_max_seconds": max_time,
        "relay_overlap_seconds": overlap,
        "relay_ready_timeout_seconds": ready_timeout,
        "test_command": args.test_command,
        "jobs": [],
    }
    dump_json(chain_json, chain)

    previous_job_id: Optional[str] = None
    job_ids: List[str] = []
    for seg in segments:
        seg_dir = chain_dir / f"job-{seg['index']:03d}"
        run_log = seg_dir / "run.log"
        tunnel_log = seg_dir / "tunnel.log"
        batch_script = seg_dir / "batch.sh"
        meta_json = seg_dir / "meta.json"
        seg_dir.mkdir(parents=True, exist_ok=True)

        write_batch_script(
            batch_script,
            python_bin,
            inner_py,
            config_path,
            profile_name,
            seg_dir,
            run_log,
            tunnel_log,
            profile["pre_commands"],
            previous_job_id=previous_job_id,
            relay_ready_timeout=ready_timeout,
            test_command=args.test_command,
        )
        sbatch_cmd = build_sbatch_cmd(
            profile,
            run_log,
            batch_script,
            seg["duration"],
            seg["begin_offset"],
        )
        job_id = submit_cmd(sbatch_cmd, args.dry_run, f"DRY-RUN-{seg['index']:03d}")
        job_ids.append(job_id)

        meta = {
            "type": "relay_segment",
            "session_id": f"{chain_id}-job-{seg['index']:03d}",
            "chain_id": chain_id,
            "profile": profile_name,
            "config_path": str(config_path),
            "session_dir": str(seg_dir),
            "run_log": str(run_log),
            "tunnel_log": str(tunnel_log),
            "batch_script": str(batch_script),
            "sbatch_cmd": sbatch_cmd,
            "job_id": job_id,
            "previous_job_id": previous_job_id,
            "index": seg["index"],
            "begin_offset_seconds": seg["begin_offset"],
            "duration_seconds": seg["duration"],
        }
        dump_json(meta_json, meta)
        chain["jobs"].append(meta)
        dump_json(chain_json, chain)
        previous_job_id = job_id

    if not args.dry_run:
        update_current_links(state_dir, profile_name, chain_dir)

    print()
    print(f"chain dir:       {chain_dir}")
    if args.dry_run:
        print("sbatch commands:")
        for job in chain["jobs"]:
            print(f"  {job['index']}: {shlex.join(job['sbatch_cmd'])}")
    print_followups(chain_id, profile_name, job_ids, args.dry_run)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Submit a new code tunnel session through Slurm.")
    ap.add_argument("profile", nargs="?", default=None, help="Profile name from codeserver.toml.")
    ap.add_argument("--config", default=str(default_config_path()), help="Path to the TOML config file.")
    ap.add_argument("--dry-run", action="store_true", help="Write session files and show sbatch commands without submitting.")
    ap.add_argument("--time", help="Requested walltime, e.g. 72:00:00, 72h, 3d.")
    ap.add_argument("--relay-overlap", help="How long before expiry to start the next relay job.")
    ap.add_argument("--no-relay", action="store_true", help="Fail instead of splitting requests longer than the profile max.")
    ap.add_argument("--hold", action="store_true", help="Hold a Slurm allocation open for VS Code Remote-SSH proxying.")
    ap.add_argument("--test-command", help="Developer/test command to run instead of code tunnel, e.g. 'echo READY; sleep 600'.")
    args = ap.parse_args()

    if args.hold and args.test_command:
        die("--hold and --test-command cannot be used together", code=2)
    if args.hold:
        args.test_command = HOLD_COMMAND

    config_path = pathlib.Path(args.config).resolve()
    try:
        cfg = load_config(config_path)
    except (ConfigError, FileNotFoundError, tomllib.TOMLDecodeError) as exc:
        die(f"{exc}. Use --help for usage.", code=2)

    profile_name = args.profile or cfg["default_profile"]
    try:
        profile = get_profile(cfg, profile_name)
    except ConfigError as exc:
        names = ", ".join(profile_names(cfg))
        die(f"{exc}. available profiles: {names}. Use --help for usage.", code=2)

    try:
        max_time = profile_time(profile, "max_time") or profile_time(profile, "default_time")
        if max_time is None:
            die(f"profile '{profile_name}' needs max_time/default_time or --time in sbatch_args", code=2)
        default_time = profile_time(profile, "default_time") or max_time
        requested = parse_duration(args.time) if args.time else default_time
        overlap = parse_duration(args.relay_overlap or str(profile.get("relay_overlap") or "15m"))
        segments = plan_relay(requested, max_time, overlap)
    except ConfigError as exc:
        die(f"{exc}. Use --help for usage.", code=2)

    if len(segments) == 1:
        return submit_single(cfg, config_path, profile_name, profile, requested, args)

    if args.no_relay or not profile.get("relay_enabled", True):
        die(
            f"requested time {format_duration(requested)} exceeds profile limit "
            f"{format_duration(max_time)} and relay is disabled",
            code=2,
        )

    return submit_chain(cfg, config_path, profile_name, profile, requested, max_time, overlap, segments, args)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/python3.11
import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import tomllib
from typing import Any, Dict, List, Optional

import codeserver_status
import codeserver_stop
import codeserver_submit
from codeserver_lib import (
    ConfigError,
    default_config_path,
    die,
    dump_json,
    expand_path,
    get_profile,
    load_config,
    load_json,
    parse_slurm_seconds,
    profile_names,
    query_job_status,
    resolve_session_dir,
    run_capture,
    state_from_status,
)
from codeserver_relay import format_duration as relay_format_duration
from codeserver_relay import plan_relay
from codeserver_relay import profile_time


TOP_HELP = """usage: cs [command] [options] [target]

Manage VS Code tunnel sessions on Slurm.

commands:
  submit, s [profile]        Submit a new code tunnel session.
  status, stat, i [target]   Show status for latest, profile, session id, chain id, or job id.
  list, l                    List known sessions/chains and Slurm state.
  stop, x [target]           Cancel a session or relay chain.
  extend [target] DURATION   Add time to a running/pending Slurm job.
  continue, c [target]       Continue/connect to a running session node.
  proxy, p [target]          Proxy stdin/stdout to SSH on the session node.
  profiles                   Show configured profiles.
  config                     Show resolved config path and key settings.
  completion [bash|zsh]      Print shell completion setup.

short action flags:
  -s [profile]               Alias for: submit [profile]
  -i [target]                Alias for: status [target]
  -l                         Alias for: list
  -x [target]                Alias for: stop [target]
  -p [target]                Alias for: proxy [target]

examples:
  cs submit                  Submit the default profile.
  cs -s cpu --time 72h       Submit a long CPU relay chain if needed.
  cs status gpu              Show current GPU tunnel status.
  cs list --expand           List relay chain segments.
  cs stop cpu                Cancel current CPU tunnel session/chain.
  cs extend cpu 10h          Add 10 hours to the current CPU tunnel job.
  cs continue cpu            Continue/connect to the current CPU session node.
  cs proxy --auto-submit --wait cpu
                             Submit/wait as needed, then proxy to the CPU job node.
"""

SHORT_ACTIONS = {"-s": "submit", "-i": "status", "-l": "list", "-x": "stop", "-p": "proxy"}
ACTIVE_STATES = {"BOOT_FAIL", "CONFIGURING", "COMPLETING", "PENDING", "PREEMPTED", "RUNNING", "RESIZING", "REQUEUED", "REQUEUE_FED", "REQUEUE_HOLD", "REQUEUE_HOLD_FED", "SIGNALING", "SPECIAL_EXIT", "STAGE_OUT", "STOPPED", "SUSPENDED"}


def normalize_argv(argv: List[str]) -> List[str]:
    if not argv or argv[0] in ("-h", "--help"):
        return argv
    if argv[0] in SHORT_ACTIONS:
        return [SHORT_ACTIONS[argv[0]]] + argv[1:]
    return argv


def add_config_arg(parser: argparse.ArgumentParser, *, default: Any = str(default_config_path())) -> None:
    parser.add_argument("-c", "--config", default=default, help="Path to the TOML config file.")


def load_cfg(path: str) -> Dict[str, Any]:
    try:
        return load_config(pathlib.Path(path).resolve())
    except (ConfigError, FileNotFoundError, tomllib.TOMLDecodeError) as exc:
        die(f"{exc}. Use --help for usage.", code=2)


def call_legacy_main(module: Any, prog: str, args: List[str]) -> int:
    old_argv = sys.argv
    try:
        sys.argv = [prog] + args
        return int(module.main())
    finally:
        sys.argv = old_argv


def is_active_state(state: str) -> bool:
    return state.upper() in ACTIVE_STATES


def chain_state(chain: Dict[str, Any]) -> str:
    states: List[str] = []
    for job in chain.get("jobs", []):
        job_id = str(job.get("job_id") or "")
        status = query_job_status(job_id) if job_id and not job_id.startswith("DRY-RUN") else None
        states.append(state_from_status(status))
    if any(state == "RUNNING" for state in states):
        return "RUNNING"
    if any(state == "PENDING" for state in states):
        return "PENDING"
    return states[0] if states else "unknown"


def session_rows(cfg: Dict[str, Any], expand: bool = False) -> List[Dict[str, str]]:
    root = expand_path(cfg["root_dir"])
    rows: List[Dict[str, str]] = []
    logs_dir = root / "logs"
    if not logs_dir.exists():
        return rows

    for chain_path in sorted(logs_dir.glob("*/chain.json"), reverse=True):
        try:
            chain = load_json(chain_path)
        except (OSError, json.JSONDecodeError):
            continue
        state = chain_state(chain)
        rows.append({"session": str(chain.get("chain_id") or chain_path.parent.name), "profile": str(chain.get("profile") or "-"), "job": f"{len(chain.get('jobs', []))} jobs", "state": state, "dir": str(chain_path.parent)})
        if expand:
            for job in chain.get("jobs", []):
                job_id = str(job.get("job_id") or "")
                status = query_job_status(job_id) if job_id and not job_id.startswith("DRY-RUN") else None
                rows.append({"session": f"  job-{int(job['index']):03d}", "profile": str(chain.get("profile") or "-"), "job": job_id or "-", "state": state_from_status(status), "dir": str(job.get("session_dir") or "-")})

    chain_dirs = {p.parent.resolve() for p in logs_dir.glob("*/chain.json")}
    for meta_path in sorted(logs_dir.glob("*/meta.json"), reverse=True):
        if meta_path.parent.resolve() in chain_dirs:
            continue
        try:
            meta = load_json(meta_path)
        except (OSError, json.JSONDecodeError):
            continue
        job_id = str(meta.get("job_id") or "")
        status = query_job_status(job_id) if job_id and not job_id.startswith("DRY-RUN") else None
        rows.append({"session": str(meta.get("session_id") or meta_path.parent.name), "profile": str(meta.get("profile") or "-"), "job": job_id or "-", "state": state_from_status(status), "dir": str(meta_path.parent)})
    return rows


def print_table(rows: List[Dict[str, str]]) -> None:
    headers = ["SESSION", "PROFILE", "JOB", "STATE", "DIR"]
    data = [[row["session"], row["profile"], row["job"], row["state"], row["dir"]] for row in rows]
    widths = [len(header) for header in headers]
    for row in data:
        for idx, value in enumerate(row):
            widths[idx] = max(widths[idx], len(value))
    fmt = "  ".join(f"{{:<{width}}}" for width in widths)
    print(fmt.format(*headers))
    for row in data:
        print(fmt.format(*row))


def cmd_list(args: argparse.Namespace) -> int:
    cfg = load_cfg(args.config)
    rows = session_rows(cfg, expand=args.expand)
    if args.active:
        rows = [row for row in rows if is_active_state(row["state"])]
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0
    if not rows:
        print("no sessions found")
        return 0
    print_table(rows)
    return 0


def job_name_for_profile(cfg: Dict[str, Any], profile_name: str) -> Optional[str]:
    profile = cfg["profiles"].get(profile_name)
    if not profile:
        return None
    for arg in profile.get("sbatch_args", []):
        if arg.startswith("--job-name="):
            return arg.split("=", 1)[1]
    return None


def resolve_proxy_job_name(cfg: Dict[str, Any], target: Optional[str]) -> str:
    name = target or cfg["default_profile"]
    return job_name_for_profile(cfg, name) or name


def proxy_target_selector(cfg: Dict[str, Any], target: Optional[str]) -> tuple[str, bool, str]:
    requested = target or cfg["default_profile"]
    if requested.isdigit():
        return requested, True, f"job id: {requested}"
    if requested in cfg["profiles"]:
        job_name = resolve_proxy_job_name(cfg, requested)
        return job_name, False, f"job name: {job_name}"

    job_id = job_id_from_session_target(cfg, target)
    if job_id and job_id != "DRY-RUN":
        return job_id, True, f"job id: {job_id}"

    job_name = resolve_proxy_job_name(cfg, target)
    return job_name, False, f"job name: {job_name}"


def job_id_from_session_target(cfg: Dict[str, Any], target: Optional[str]) -> Optional[str]:
    name = target or "latest"
    try:
        session_dir = resolve_session_dir(cfg, name)
    except FileNotFoundError:
        return None
    meta_path = session_dir / "meta.json"
    if not meta_path.exists():
        return None
    try:
        meta = load_json(meta_path)
    except (OSError, json.JSONDecodeError):
        return None
    job_id = meta.get("job_id")
    return str(job_id) if job_id else None


def squeue_running_nodes(*, user: str, target: str, by_job_id: bool) -> List[str]:
    selector = ["-j", target] if by_job_id else ["-n", target]
    rc, out, err = run_capture(["squeue", "-h", "-u", user, *selector, "-t", "RUNNING", "-o", "%N"])
    if rc != 0:
        die(f"squeue failed:\n{err.strip() or out.strip()}", code=127)
    return [line.strip() for line in out.splitlines() if line.strip()]


def squeue_job_matches(*, user: str, target: str, by_job_id: bool) -> List[Dict[str, str]]:
    selector = ["-j", target] if by_job_id else ["-n", target]
    rc, out, err = run_capture(["squeue", "-h", "-u", user, *selector, "-t", "PENDING,RUNNING", "-o", "%i|%T"])
    if rc != 0:
        die(f"squeue failed:\n{err.strip() or out.strip()}", code=127)
    matches: List[Dict[str, str]] = []
    for line in out.splitlines():
        raw = line.strip()
        if not raw:
            continue
        job_id, _, state = raw.partition("|")
        matches.append({"job_id": job_id.strip(), "state": state.strip().upper()})
    return matches


def preferred_job_id(matches: List[Dict[str, str]]) -> str:
    for desired_state in ("RUNNING", "PENDING"):
        for match in matches:
            if match["state"] == desired_state:
                return match["job_id"]
    return matches[0]["job_id"]


def resolve_slurm_job_id(cfg: Dict[str, Any], target: Optional[str], *, command_name: str) -> str:
    if shutil.which("squeue") is None:
        die("squeue not found on this host", code=127)

    user = os.environ.get("USER", "")
    selector, by_job_id, label = proxy_target_selector(cfg, target)
    matches = squeue_job_matches(user=user, target=selector, by_job_id=by_job_id)

    if not matches:
        die(f"no pending or running allocation found for {label}", code=127)
    selected = preferred_job_id(matches)
    if len(matches) > 1:
        summary = ", ".join(f"{match['job_id']}:{match['state']}" for match in matches)
        print(
            f"WARNING: {len(matches)} pending/running allocations match {label}; "
            f"{command_name} will use {selected}. Matches: {summary}",
            file=sys.stderr,
        )
    return selected


def running_node_for_target(
    cfg: Dict[str, Any],
    target: Optional[str],
    *,
    command_name: str,
    required: bool,
) -> Optional[str]:
    for cmd in ("squeue", "scontrol"):
        if shutil.which(cmd) is None:
            die(f"{cmd} not found on this host", code=127)
    user = os.environ.get("USER", "")
    selector, by_job_id, label = proxy_target_selector(cfg, target)
    matches = squeue_running_nodes(user=user, target=selector, by_job_id=by_job_id)

    nodelist = matches[0] if matches else ""
    if not nodelist:
        if required:
            die(f"no running allocation found for {label}", code=127)
        return None
    if len(matches) > 1:
        print(
            f"WARNING: {len(matches)} running allocations match {label}; "
            f"{command_name} will use only the first one listed: {nodelist}",
            file=sys.stderr,
        )

    rc, out, err = run_capture(["scontrol", "show", "hostnames", nodelist])
    if rc != 0:
        die(f"scontrol failed:\n{err.strip() or out.strip()}", code=127)
    node = out.splitlines()[0].strip() if out.strip() else ""
    if not node:
        die(f"failed to resolve hostname from NodeList: {nodelist}", code=127)
    return node


def first_node_for_target(cfg: Dict[str, Any], target: Optional[str], *, command_name: str) -> str:
    node = running_node_for_target(cfg, target, command_name=command_name, required=True)
    assert node is not None
    return node


def submit_hold_allocation(cfg: Dict[str, Any], profile_name: str, requested_seconds: Optional[int]) -> None:
    try:
        profile = get_profile(cfg, profile_name)
        max_time = profile_time(profile, "max_time") or profile_time(profile, "default_time")
        default_time = profile_time(profile, "default_time") or max_time
    except ConfigError as exc:
        die(f"{exc}. Use --help for usage.", code=2)

    if default_time is None:
        die(f"profile '{profile_name}' needs max_time/default_time or --time", code=2)

    duration = requested_seconds or default_time
    if max_time is not None and duration > max_time:
        die(
            f"auto-submit time {relay_format_duration(duration)} exceeds profile limit "
            f"{relay_format_duration(max_time)}",
            code=2,
        )

    submit_args = argparse.Namespace(
        dry_run=False,
        test_command=codeserver_submit.HOLD_COMMAND,
    )
    print(
        f"cs proxy: submitting hold allocation profile={profile_name} "
        f"time={relay_format_duration(duration)}",
        file=sys.stderr,
    )
    with contextlib.redirect_stdout(sys.stderr):
        codeserver_submit.submit_single(
            cfg,
            pathlib.Path(cfg["config_path"]),
            profile_name,
            profile,
            duration,
            submit_args,
        )


def maybe_auto_submit_for_proxy(cfg: Dict[str, Any], args: argparse.Namespace) -> None:
    if not args.auto_submit:
        return
    profile_name = args.target or cfg["default_profile"]
    if profile_name not in cfg["profiles"]:
        die("--auto-submit target must be omitted or be a profile name", code=2)

    lock_dir = expand_path(cfg["root_dir"]) / "state"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"auto-submit-{profile_name}.lock"
    with lock_path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        job_name = resolve_proxy_job_name(cfg, profile_name)
        matches = squeue_job_matches(
            user=os.environ.get("USER", ""),
            target=job_name,
            by_job_id=False,
        )
        if matches:
            summary = ", ".join(f"{match['job_id']}:{match['state']}" for match in matches)
            print(f"cs proxy: using existing allocation(s): {summary}", file=sys.stderr)
            return

        requested = parse_duration_seconds(args.time) if args.time else None
        submit_hold_allocation(cfg, profile_name, requested)


def wait_for_first_node(cfg: Dict[str, Any], args: argparse.Namespace) -> str:
    timeout = parse_duration_seconds(args.wait_timeout) if args.wait_timeout else None
    poll_interval = max(1.0, float(args.poll_interval))
    deadline = time.monotonic() + timeout if timeout is not None else None
    _, _, label = proxy_target_selector(cfg, args.target)
    printed = False

    while True:
        node = running_node_for_target(
            cfg,
            args.target,
            command_name=args.command,
            required=False,
        )
        if node:
            return node
        now = time.monotonic()
        if deadline is not None and now >= deadline:
            die(f"timed out waiting for running allocation for {label}", code=127)
        if not printed:
            print(f"cs proxy: waiting for running allocation for {label}", file=sys.stderr)
            printed = True
        time.sleep(poll_interval)


def exec_ssh_proxy(node: str) -> None:
    if shutil.which("nc"):
        os.execvp("nc", ["nc", node, "22"])
    if shutil.which("socat"):
        os.execvp("socat", ["socat", "-", f"TCP:{node}:22"])
    os.execvp("ssh", ["ssh", "-o", "BatchMode=yes", "-W", f"{node}:22", "localhost"])


def cmd_connect(args: argparse.Namespace) -> int:
    # Shared handler for `proxy` and `continue`: both resolve the session node
    # and hand stdin/stdout to SSH on it.
    cfg = load_cfg(args.config)
    maybe_auto_submit_for_proxy(cfg, args)
    if args.wait or args.auto_submit:
        node = wait_for_first_node(cfg, args)
    else:
        node = first_node_for_target(cfg, args.target, command_name=args.command)
    exec_ssh_proxy(node)
    return 127


def parse_duration_seconds(value: str) -> int:
    raw = value.strip().lower()
    if not raw:
        die("duration must not be empty", code=2)
    if raw.startswith("+"):
        raw = raw[1:]

    unit_match = re.fullmatch(r"(\d+)([dhm])", raw)
    if unit_match:
        amount = int(unit_match.group(1))
        unit = unit_match.group(2)
        if unit == "d":
            return amount * 24 * 60 * 60
        if unit == "h":
            return amount * 60 * 60
        return amount * 60

    day_match = re.fullmatch(r"(\d+)-(\d{1,2})(?::(\d{1,2}))?(?::(\d{1,2}))?", raw)
    if day_match:
        days = int(day_match.group(1))
        hours = int(day_match.group(2))
        minutes = int(day_match.group(3) or 0)
        seconds = int(day_match.group(4) or 0)
        if minutes >= 60 or seconds >= 60:
            die(f"invalid duration: {value}", code=2)
        return (((days * 24) + hours) * 60 + minutes) * 60 + seconds

    parts = raw.split(":")
    if 1 <= len(parts) <= 3 and all(part.isdigit() for part in parts):
        nums = [int(part) for part in parts]
        if len(nums) == 1:
            return nums[0] * 60
        if len(nums) == 2:
            hours, minutes = nums
            seconds = 0
        else:
            hours, minutes, seconds = nums
        if minutes >= 60 or seconds >= 60:
            die(f"invalid duration: {value}", code=2)
        return ((hours * 60) + minutes) * 60 + seconds

    die("duration must look like 10h, 90m, 1-02:00:00, HH:MM:SS, or minutes", code=2)


def format_slurm_time(seconds: int) -> str:
    days, rem = divmod(seconds, 24 * 60 * 60)
    hours, rem = divmod(rem, 60 * 60)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}-{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def job_field(job_text: str, field: str) -> Optional[str]:
    match = re.search(rf"(?:^|\s){re.escape(field)}=(\S+)", job_text)
    return match.group(1) if match else None


def job_seconds_until_end(job: Dict[str, Any]) -> int:
    job_id = str(job.get("job_id") or "")
    if not job_id or job_id.startswith("DRY-RUN"):
        die(f"cannot extend relay segment without a real Slurm job id: {job_id or '-'}")
    rc, out, err = run_capture(["squeue", "-h", "-j", job_id, "-o", "%T|%M|%l|%S"])
    if rc != 0:
        die(f"squeue failed:\n{err.strip() or out.strip()}", code=127)
    if not out.strip():
        die(f"job {job_id} is not pending or running; cannot extend relay")

    state, elapsed_text, limit_text, start_text = out.splitlines()[0].split("|", 3)
    state = state.strip().upper()
    limit = parse_slurm_seconds(limit_text) or int(job.get("duration_seconds") or 0)
    if limit <= 0:
        die(f"could not determine time limit for job {job_id}")

    if state == "RUNNING":
        elapsed = parse_slurm_seconds(elapsed_text)
        if elapsed is None:
            die(f"could not determine elapsed time for running job {job_id}")
        return max(0, limit - elapsed)

    if state == "PENDING":
        start = codeserver_status.parse_slurm_start(start_text)
        if start is None:
            die(f"job {job_id} is pending without an estimated start time; try extending after it starts")
        now = dt.datetime.now(start.tzinfo)
        return max(0, int((start - now).total_seconds())) + limit

    die(f"job {job_id} is {state}, not pending/running; cannot extend relay")


def chain_from_session(cfg: Dict[str, Any], session_dir: pathlib.Path) -> tuple[pathlib.Path, Dict[str, Any]]:
    meta = load_json(session_dir / "meta.json")
    root = expand_path(cfg["root_dir"])
    state_dir = root / "state"
    chain_id = f"{meta['session_id']}-relay"
    chain_dir = root / "logs" / chain_id
    chain_dir.mkdir(parents=True, exist_ok=True)

    first_job = dict(meta)
    first_job.update(
        {
            "type": "relay_segment",
            "chain_id": chain_id,
            "index": 1,
            "begin_offset_seconds": 0,
            "duration_seconds": int(meta.get("duration_seconds") or 0),
            "previous_job_id": None,
        }
    )
    chain = {
        "type": "relay_chain",
        "chain_id": chain_id,
        "profile": meta["profile"],
        "config_path": meta["config_path"],
        "chain_dir": str(chain_dir),
        "requested_time_seconds": int(meta.get("duration_seconds") or 0),
        "profile_max_seconds": 0,
        "relay_overlap_seconds": 0,
        "relay_ready_timeout_seconds": 0,
        "test_command": meta.get("test_command"),
        "jobs": [first_job],
    }
    dump_json(chain_dir / "chain.json", chain)
    codeserver_submit.update_current_links(state_dir, str(meta["profile"]), chain_dir)
    return chain_dir, chain


def load_or_create_chain(cfg: Dict[str, Any], session_dir: pathlib.Path) -> tuple[pathlib.Path, Dict[str, Any]]:
    if (session_dir / "chain.json").exists():
        return session_dir, load_json(session_dir / "chain.json")
    return chain_from_session(cfg, session_dir)


def append_relay_extension(
    cfg: Dict[str, Any],
    chain_dir: pathlib.Path,
    chain: Dict[str, Any],
    extra_seconds: int,
) -> list[str]:
    if extra_seconds <= 0:
        die("extend duration must be positive", code=2)

    profile_name = str(chain["profile"])
    profile = get_profile(cfg, profile_name)
    if not profile.get("relay_enabled", True):
        die(f"profile '{profile_name}' has relay disabled", code=2)

    max_time = int(chain.get("profile_max_seconds") or 0)
    if max_time <= 0:
        max_time = profile_time(profile, "max_time") or profile_time(profile, "default_time") or 0
    if max_time <= 0:
        die(f"profile '{profile_name}' needs max_time/default_time to extend relay", code=2)

    overlap = int(chain.get("relay_overlap_seconds") or 0)
    if overlap <= 0:
        overlap = profile_time(profile, "relay_overlap") or 0
    ready_timeout = int(chain.get("relay_ready_timeout_seconds") or 0)
    if ready_timeout <= 0:
        ready_timeout = profile_time(profile, "relay_ready_timeout") or 300

    jobs = list(chain.get("jobs", []))
    if not jobs:
        die(f"relay chain has no jobs: {chain_dir / 'chain.json'}")
    last_job = jobs[-1]
    seconds_until_end = job_seconds_until_end(last_job)
    submit_base_begin = max(0, seconds_until_end - overlap)

    last_begin = int(last_job.get("begin_offset_seconds") or 0)
    last_duration = int(last_job.get("duration_seconds") or 0)
    metadata_base_begin = max(0, last_begin + last_duration - overlap)
    requested_with_overlap = extra_seconds + overlap
    segments = plan_relay(requested_with_overlap, max_time, overlap)

    print(f"current requested: {relay_format_duration(int(chain.get('requested_time_seconds') or 0))}")
    print(f"last job id:       {last_job.get('job_id') or '-'}")
    print(f"last job ends in:  {relay_format_duration(seconds_until_end)}")
    print(f"relay overlap:     {relay_format_duration(overlap)}")
    print(f"extension:         {relay_format_duration(extra_seconds)}")
    print(f"new relay jobs:    {len(segments)}")
    print()

    config_path = pathlib.Path(str(chain.get("config_path") or cfg["config_path"]))
    python_bin = codeserver_submit.batch_python_bin()
    inner_py = pathlib.Path(__file__).resolve().parent / "codeserver_inner.py"
    previous_job_id = str(last_job.get("job_id") or "")
    next_index = max(int(job.get("index", 0)) for job in jobs) + 1
    submitted: list[str] = []

    for offset, seg in enumerate(segments):
        index = next_index + offset
        seg_dir = chain_dir / f"job-{index:03d}"
        run_log = seg_dir / "run.log"
        tunnel_log = seg_dir / "tunnel.log"
        batch_script = seg_dir / "batch.sh"
        meta_json = seg_dir / "meta.json"
        seg_dir.mkdir(parents=True, exist_ok=True)

        codeserver_submit.write_batch_script(
            batch_script,
            python_bin,
            inner_py,
            config_path,
            profile_name,
            seg_dir,
            run_log,
            tunnel_log,
            profile["pre_commands"],
            previous_job_id=previous_job_id or None,
            relay_ready_timeout=ready_timeout,
            test_command=chain.get("test_command"),
        )
        submit_begin = submit_base_begin + int(seg["begin_offset"])
        sbatch_cmd = codeserver_submit.build_sbatch_cmd(
            profile,
            run_log,
            batch_script,
            int(seg["duration"]),
            submit_begin,
        )
        print(
            f"submitting job-{index:03d}: starts ~+{relay_format_duration(submit_begin)}, "
            f"runs {relay_format_duration(int(seg['duration']))}, "
            f"previous={previous_job_id or '-'}",
            flush=True,
        )
        job_id = codeserver_submit.submit_cmd(sbatch_cmd, False, "DRY-RUN")
        print(f"submitted job-{index:03d}:  {job_id}", flush=True)
        submitted.append(job_id)

        meta = {
            "type": "relay_segment",
            "session_id": f"{chain['chain_id']}-job-{index:03d}",
            "chain_id": chain["chain_id"],
            "profile": profile_name,
            "config_path": str(config_path),
            "session_dir": str(seg_dir),
            "run_log": str(run_log),
            "tunnel_log": str(tunnel_log),
            "batch_script": str(batch_script),
            "sbatch_cmd": sbatch_cmd,
            "job_id": job_id,
            "previous_job_id": previous_job_id or None,
            "index": index,
            "begin_offset_seconds": metadata_base_begin + int(seg["begin_offset"]),
            "duration_seconds": int(seg["duration"]),
        }
        dump_json(meta_json, meta)
        jobs.append(meta)
        previous_job_id = job_id

    chain["jobs"] = jobs
    chain["requested_time_seconds"] = int(chain.get("requested_time_seconds") or 0) + extra_seconds
    chain["profile_max_seconds"] = max_time
    chain["relay_overlap_seconds"] = overlap
    chain["relay_ready_timeout_seconds"] = ready_timeout
    dump_json(chain_dir / "chain.json", chain)
    return submitted


def cmd_extend(args: argparse.Namespace) -> int:
    cfg = load_cfg(args.config)
    extra_seconds = parse_duration_seconds(args.duration)
    try:
        session_dir = resolve_session_dir(cfg, args.target or cfg["default_profile"])
    except FileNotFoundError as exc:
        die(f"{exc}. Use --help for usage.")
    if not session_dir.exists():
        die(f"no session found for '{args.target or cfg['default_profile']}'. Use --help for usage.")

    was_chain = (session_dir / "chain.json").exists()
    chain_dir, chain = load_or_create_chain(cfg, session_dir)

    if not was_chain:
        print(f"converted session to relay chain: {chain['chain_id']}")
    print(f"extending relay:  {chain['chain_id']}")
    print(f"profile:         {chain['profile']}")
    print(f"chain dir:       {chain_dir}")
    print()

    submitted = append_relay_extension(cfg, chain_dir, chain, extra_seconds)

    print()
    print(f"extended relay:  {chain['chain_id']}")
    print(f"added:           {relay_format_duration(extra_seconds)}")
    print(f"submitted jobs:  {', '.join(submitted)}")
    print()
    print(f"status:          cs status {chain['chain_id']}")
    print(f"stop:            cs stop {chain['chain_id']}")
    return 0


def cmd_profiles(args: argparse.Namespace) -> int:
    cfg = load_cfg(args.config)
    print(f"default: {cfg['default_profile']}")
    for name in profile_names(cfg):
        profile = cfg["profiles"][name]
        enabled = "enabled" if profile["enabled"] else "disabled"
        job_name = job_name_for_profile(cfg, name) or "-"
        max_time = profile.get("max_time") or "sbatch/default"
        print(f"{name}: {enabled}, job={job_name}, max_time={max_time}")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    cfg = load_cfg(args.config)
    root = expand_path(cfg["root_dir"])
    print(f"config:          {cfg['config_path']}")
    print(f"root dir:        {root}")
    print(f"default profile: {cfg['default_profile']}")
    print(f"code command:    {' '.join([cfg['code_bin']] + cfg['code_tunnel_args'])}")
    print(f"profiles:        {', '.join(profile_names(cfg))}")
    return 0


def completion_targets(cfg: Dict[str, Any]) -> List[str]:
    targets = {"latest", *profile_names(cfg)}
    root = expand_path(cfg["root_dir"])
    logs_dir = root / "logs"
    if logs_dir.exists():
        for meta_path in logs_dir.glob("*/meta.json"):
            try:
                meta = load_json(meta_path)
            except (OSError, json.JSONDecodeError):
                continue
            if meta.get("session_id"):
                targets.add(str(meta["session_id"]))
            if meta.get("job_id"):
                targets.add(str(meta["job_id"]))
            if meta.get("profile"):
                targets.add(str(meta["profile"]))
    for name in profile_names(cfg):
        job_name = job_name_for_profile(cfg, name)
        if job_name:
            targets.add(job_name)
    if shutil.which("squeue"):
        user = os.environ.get("USER", "")
        try:
            proc = subprocess.run(
                ["squeue", "-h", "-u", user, "-o", "%i|%j"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=0.75,
            )
        except (OSError, subprocess.TimeoutExpired):
            proc = None
        if proc and proc.returncode == 0:
            for line in proc.stdout.splitlines():
                parts = line.split("|", 1)
                if parts and parts[0].strip():
                    targets.add(parts[0].strip())
                if len(parts) == 2 and parts[1].strip():
                    targets.add(parts[1].strip())
    return sorted(targets)


def cmd_complete(args: argparse.Namespace) -> int:
    cfg = load_cfg(args.config)
    commands = [
        "submit",
        "s",
        "status",
        "stat",
        "i",
        "list",
        "l",
        "stop",
        "x",
        "extend",
        "continue",
        "c",
        "proxy",
        "p",
        "profiles",
        "config",
        "completion",
    ]
    if args.kind == "commands":
        print("\n".join(commands))
    elif args.kind == "profiles":
        print("\n".join(profile_names(cfg)))
    elif args.kind == "targets":
        print("\n".join(completion_targets(cfg)))
    return 0


BASH_COMPLETION = r'''_cs_completion()
{
  local cur cmd
  COMPREPLY=()
  cur="${COMP_WORDS[COMP_CWORD]}"
  cmd="${COMP_WORDS[1]}"

  if [[ "$COMP_CWORD" -eq 1 ]]; then
    COMPREPLY=( $(compgen -W "$(cs complete commands 2>/dev/null)" -- "$cur") )
    return 0
  fi

  case "$cmd" in
    submit|s|-s)
      COMPREPLY=( $(compgen -W "$(cs complete profiles 2>/dev/null)" -- "$cur") )
      ;;
    list|l|-l)
      COMPREPLY=( $(compgen -W "-a --active --all --json" -- "$cur") )
      ;;
    extend)
      if [[ "$COMP_CWORD" -eq 2 ]]; then
        COMPREPLY=( $(compgen -W "$(cs complete targets 2>/dev/null)" -- "$cur") )
      else
        COMPREPLY=( $(compgen -W "30m 1h 2h 4h 8h 10h 12h 1-00:00:00" -- "$cur") )
      fi
      ;;
    status|stat|i|stop|x|extend|continue|c|proxy|p|-i|-x|-p)
      COMPREPLY=( $(compgen -W "$(cs complete targets 2>/dev/null)" -- "$cur") )
      ;;
    completion)
      COMPREPLY=( $(compgen -W "bash zsh" -- "$cur") )
      ;;
  esac
}
complete -F _cs_completion cs
'''


ZSH_COMPLETION = r'''#compdef cs
_cs() {
  local -a commands profiles targets
  commands=("${(@f)$(cs complete commands 2>/dev/null)}")

  if (( CURRENT == 2 )); then
    _describe 'command' commands
    return
  fi

  case "$words[2]" in
    submit|s|-s)
      profiles=("${(@f)$(cs complete profiles 2>/dev/null)}")
      _describe 'profile' profiles
      ;;
    list|l|-l)
      _values 'option' '-a' '--active' '--all' '--json'
      ;;
    extend)
      if (( CURRENT == 3 )); then
        targets=("${(@f)$(cs complete targets 2>/dev/null)}")
        _describe 'target' targets
      else
        _values 'duration' 30m 1h 2h 4h 8h 10h 12h 1-00:00:00
      fi
      ;;
    status|stat|i|stop|x|extend|continue|c|proxy|p|-i|-x|-p)
      targets=("${(@f)$(cs complete targets 2>/dev/null)}")
      _describe 'target' targets
      ;;
    completion)
      _values 'shell' bash zsh
      ;;
  esac
}
(( $+functions[compdef] )) && compdef _cs cs
'''


def cmd_completion(args: argparse.Namespace) -> int:
    if args.shell == "bash":
        print(BASH_COMPLETION)
    elif args.shell == "zsh":
        print(ZSH_COMPLETION)
    else:
        die("completion shell must be one of: bash, zsh", code=2)
    return 0


def add_connect_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--wait",
        action="store_true",
        help="Wait until the target allocation is running before proxying.",
    )
    parser.add_argument(
        "--auto-submit",
        action="store_true",
        help="Submit a hold allocation for a profile if no pending/running allocation exists.",
    )
    parser.add_argument(
        "--time",
        help="Walltime for an auto-submitted hold allocation, e.g. 8h or 12:00:00.",
    )
    parser.add_argument(
        "--wait-timeout",
        help="Maximum time to wait for a node, e.g. 30m. Default: wait indefinitely.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=10.0,
        help="Seconds between Slurm polling attempts while waiting.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cs", add_help=False, formatter_class=argparse.RawDescriptionHelpFormatter, description=TOP_HELP)
    parser.add_argument("-h", "--help", action="store_true", help=argparse.SUPPRESS)
    add_config_arg(parser)
    subparsers = parser.add_subparsers(dest="command")

    submit = subparsers.add_parser("submit", aliases=["s"], help="Submit a new tunnel session.")
    add_config_arg(submit, default=argparse.SUPPRESS)
    submit.add_argument("profile", nargs="?", help="Profile from codeserver.toml.")
    submit.add_argument("--dry-run", action="store_true", help="Write session files and show sbatch commands without submitting.")
    submit.add_argument("--time", help="Requested walltime, e.g. 72:00:00, 72h, 3d.")
    submit.add_argument("--relay-overlap", help="How long before expiry to start the next relay job.")
    submit.add_argument("--no-relay", action="store_true", help="Fail instead of splitting long requests.")
    submit.add_argument("--hold", action="store_true", help="Hold a Slurm allocation open for VS Code Remote-SSH proxying.")
    submit.add_argument("--test-command", help="Developer/test command to run instead of code tunnel.")

    status = subparsers.add_parser("status", aliases=["stat", "i"], help="Show session status.")
    add_config_arg(status, default=argparse.SUPPRESS)
    status.add_argument("target", nargs="?", help="latest, profile name, session id, chain id, or job id.")

    stop = subparsers.add_parser("stop", aliases=["x"], help="Cancel a session or relay chain.")
    add_config_arg(stop, default=argparse.SUPPRESS)
    stop.add_argument("target", nargs="?", help="latest, profile name, session id, chain id, or job id.")

    extend = subparsers.add_parser("extend", help="Add time to a pending/running Slurm job.")
    add_config_arg(extend, default=argparse.SUPPRESS)
    extend.add_argument("target", nargs="?", help="latest, profile name, session id, job id, or job name.")
    extend.add_argument(
        "duration",
        nargs="?",
        default=None,
        help="Time to add, such as 10h, 90m, 1-02:00:00, HH:MM:SS, or minutes.",
    )

    cont = subparsers.add_parser(
        "continue",
        aliases=["c"],
        help="Continue/connect to a running session node.",
    )
    add_config_arg(cont, default=argparse.SUPPRESS)
    cont.add_argument("target", nargs="?", help="latest, profile name, session id, job id, or job name.")
    add_connect_options(cont)

    list_parser = subparsers.add_parser("list", aliases=["l"], help="List known sessions.")
    add_config_arg(list_parser, default=argparse.SUPPRESS)
    list_parser.add_argument("-a", "--active", action="store_true", help="Show only active sessions.")
    list_parser.add_argument("--all", action="store_true", help="Include historical sessions.")
    list_parser.add_argument("--expand", action="store_true", help="Show relay chain segments.")
    list_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    proxy = subparsers.add_parser("proxy", aliases=["p"], help="Proxy SSH to the session node.")
    add_config_arg(proxy, default=argparse.SUPPRESS)
    proxy.add_argument("target", nargs="?", help="latest, profile name, session id, job id, or job name.")
    add_connect_options(proxy)

    profiles = subparsers.add_parser("profiles", help="Show configured profiles.")
    add_config_arg(profiles, default=argparse.SUPPRESS)
    config = subparsers.add_parser("config", help="Show resolved config settings.")
    add_config_arg(config, default=argparse.SUPPRESS)

    complete = subparsers.add_parser("complete", help=argparse.SUPPRESS)
    add_config_arg(complete, default=argparse.SUPPRESS)
    complete.add_argument("kind", choices=["commands", "profiles", "targets"])

    completion = subparsers.add_parser("completion", help="Print shell completion setup.")
    completion.add_argument("shell", choices=["bash", "zsh"])

    return parser


def legacy_args(args: argparse.Namespace, value: Optional[str]) -> List[str]:
    out: List[str] = []
    if value:
        out.append(value)
    for name in ("dry_run", "no_relay", "hold"):
        if getattr(args, name, False):
            out.append("--" + name.replace("_", "-"))
    for name in ("time", "relay_overlap", "test_command"):
        value = getattr(args, name, None)
        if value:
            out.extend(["--" + name.replace("_", "-"), value])
    out.extend(["--config", args.config])
    return out


def main() -> int:
    argv = normalize_argv(sys.argv[1:])
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.help or args.command is None:
        print(TOP_HELP)
        return 0
    command = args.command
    if command in ("submit", "s"):
        return call_legacy_main(codeserver_submit, "cs submit", legacy_args(args, args.profile))
    if command in ("status", "stat", "i"):
        return call_legacy_main(codeserver_status, "cs status", [*( [args.target] if args.target else [] ), "--config", args.config])
    if command in ("stop", "x"):
        return call_legacy_main(codeserver_stop, "cs stop", [*( [args.target] if args.target else [] ), "--config", args.config])
    if command == "extend":
        if args.duration is None:
            if args.target and not args.target.isdigit():
                args.duration = args.target
                args.target = None
            else:
                parser.error("extend requires DURATION, for example: cs extend cpu 10h")
        return cmd_extend(args)
    if command in ("continue", "c", "proxy", "p"):
        return cmd_connect(args)
    if command in ("list", "l"):
        return cmd_list(args)
    if command == "profiles":
        return cmd_profiles(args)
    if command == "config":
        return cmd_config(args)
    if command == "complete":
        return cmd_complete(args)
    if command == "completion":
        return cmd_completion(args)
    parser.error(f"unknown command: {command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

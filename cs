#!/usr/bin/env python3
import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tomllib
from typing import Any, Dict, List, Optional

import codeserver_status
import codeserver_stop
import codeserver_submit
from codeserver_lib import (
    ConfigError,
    default_config_path,
    die,
    expand_path,
    load_config,
    load_json,
    profile_names,
    query_job_status,
    resolve_session_dir,
    run_capture,
)


TOP_HELP = """usage: cs [command] [options] [target]

Manage VS Code tunnel sessions on Slurm.

commands:
  submit, s [profile]        Submit a new code tunnel session.
  status, stat, i [target]   Show status for latest, profile, session id, or job id.
  list, l                    List known sessions and Slurm state.
  stop, x [target]           Cancel a session by latest, profile, session id, or job id.
  extend [target] DURATION   Add time to a running/pending Slurm job.
  continue, c [target]       Continue/connect to a running session node.
  proxy, p [target]          Proxy stdin/stdout to SSH on the session node.
  profiles                   Show configured profiles.
  config                     Show resolved config path and key settings.
  completion [bash|zsh]      Print shell completion setup.

global options:
  -h, --help                 Show this help message and exit.
  -c, --config PATH          Use a custom codeserver.toml.

short action flags:
  -s [profile]               Alias for: submit [profile]
  -i [target]                Alias for: status [target]
  -l                         Alias for: list
  -x [target]                Alias for: stop [target]
  -p [target]                Alias for: proxy [target]

examples:
  cs submit                  Submit the default profile.
  cs -s cpu                  Submit the CPU profile.
  cs status gpu              Show current GPU tunnel status.
  cs -i                      Show latest tunnel status.
  cs list --active           List active sessions only.
  cs stop cpu                Cancel current CPU tunnel session.
  cs extend cpu 10h          Add 10 hours to the current CPU tunnel job.
  cs continue cpu            Continue/connect to the current CPU session node.
  cs proxy codeserver-cpu    Proxy to SSH on the CPU job node.
"""

SHORT_ACTIONS = {
    "-s": "submit",
    "-i": "status",
    "-l": "list",
    "-x": "stop",
    "-p": "proxy",
}

ACTIVE_STATES = {
    "BOOT_FAIL",
    "CONFIGURING",
    "COMPLETING",
    "PENDING",
    "PREEMPTED",
    "RUNNING",
    "RESIZING",
    "REQUEUED",
    "REQUEUE_FED",
    "REQUEUE_HOLD",
    "REQUEUE_HOLD_FED",
    "SIGNALING",
    "SPECIAL_EXIT",
    "STAGE_OUT",
    "STOPPED",
    "SUSPENDED",
}


def normalize_argv(argv: List[str]) -> List[str]:
    if not argv:
        return argv
    if argv[0] in ("-h", "--help"):
        return argv
    if argv[0] in SHORT_ACTIONS:
        return [SHORT_ACTIONS[argv[0]]] + argv[1:]
    return argv


def add_config_arg(parser: argparse.ArgumentParser, *, default: Any = str(default_config_path())) -> None:
    parser.add_argument(
        "-c",
        "--config",
        default=default,
        help="Path to the TOML config file.",
    )


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


def status_from_text(status: Optional[str]) -> str:
    if not status:
        return "unknown"
    for field in status.split():
        if field.startswith("state="):
            return field.removeprefix("state=")
    return "unknown"


def is_active_state(state: str) -> bool:
    return state.upper() in ACTIVE_STATES


def session_rows(cfg: Dict[str, Any]) -> List[Dict[str, str]]:
    root = expand_path(cfg["root_dir"])
    rows: List[Dict[str, str]] = []
    logs_dir = root / "logs"
    if not logs_dir.exists():
        return rows

    for meta_path in sorted(logs_dir.glob("*/meta.json"), reverse=True):
        try:
            meta = load_json(meta_path)
        except (OSError, json.JSONDecodeError):
            continue

        job_id = str(meta.get("job_id") or "")
        status = query_job_status(job_id) if job_id else None
        state = status_from_text(status)
        rows.append(
            {
                "session": str(meta.get("session_id") or meta_path.parent.name),
                "profile": str(meta.get("profile") or "-"),
                "job": job_id or "-",
                "state": state,
                "status": status or "-",
                "dir": str(meta_path.parent),
            }
        )
    return rows


def print_table(rows: List[Dict[str, str]]) -> None:
    headers = ["SESSION", "PROFILE", "JOB", "STATE", "DIR"]
    data = [
        [row["session"], row["profile"], row["job"], row["state"], row["dir"]]
        for row in rows
    ]
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
    rows = session_rows(cfg)
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
    profile_job = job_name_for_profile(cfg, name)
    return profile_job or name


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


def squeue_job_ids(*, user: str, target: str, by_job_id: bool) -> List[str]:
    selector = ["-j", target] if by_job_id else ["-n", target]
    rc, out, err = run_capture(["squeue", "-h", "-u", user, *selector, "-t", "PENDING,RUNNING", "-o", "%i"])
    if rc != 0:
        die(f"squeue failed:\n{err.strip() or out.strip()}", code=127)
    return [line.strip() for line in out.splitlines() if line.strip()]


def resolve_slurm_job_id(cfg: Dict[str, Any], target: Optional[str], *, command_name: str) -> str:
    if shutil.which("squeue") is None:
        die("squeue not found on this host", code=127)

    user = os.environ.get("USER", "")
    requested = target or cfg["default_profile"]
    job_id = requested if requested.isdigit() else job_id_from_session_target(cfg, target)

    if job_id and job_id != "DRY-RUN":
        matches = squeue_job_ids(user=user, target=job_id, by_job_id=True)
        label = f"job id: {job_id}"
    else:
        job_name = resolve_proxy_job_name(cfg, target)
        matches = squeue_job_ids(user=user, target=job_name, by_job_id=False)
        label = f"job name: {job_name}"

    if not matches:
        die(f"no pending or running allocation found for {label}", code=127)
    if len(matches) > 1:
        print(
            f"WARNING: {len(matches)} pending/running allocations match {label}; "
            f"{command_name} will use only the first one listed: {matches[0]}",
            file=sys.stderr,
        )
    return matches[0]


def first_node_for_target(cfg: Dict[str, Any], target: Optional[str], *, command_name: str) -> str:
    for cmd in ("squeue", "scontrol"):
        if shutil.which(cmd) is None:
            die(f"{cmd} not found on this host", code=127)

    user = os.environ.get("USER", "")
    requested = target or cfg["default_profile"]
    job_id = requested if requested.isdigit() else job_id_from_session_target(cfg, target)

    if job_id:
        matches = squeue_running_nodes(user=user, target=job_id, by_job_id=True)
        label = f"job id: {job_id}"
    else:
        job_name = resolve_proxy_job_name(cfg, target)
        matches = squeue_running_nodes(user=user, target=job_name, by_job_id=False)
        label = f"job name: {job_name}"

    nodelist = matches[0] if matches else ""
    if not nodelist:
        die(f"no running allocation found for {label}", code=127)
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


def exec_ssh_proxy(node: str) -> None:
    if shutil.which("nc"):
        os.execvp("nc", ["nc", node, "22"])
    if shutil.which("socat"):
        os.execvp("socat", ["socat", "-", f"TCP:{node}:22"])
    os.execvp("ssh", ["ssh", "-o", "BatchMode=yes", "-W", f"{node}:22", "localhost"])


def cmd_proxy(args: argparse.Namespace) -> int:
    cfg = load_cfg(args.config)
    node = first_node_for_target(cfg, args.target, command_name=args.command)
    exec_ssh_proxy(node)
    return 127


def cmd_continue(args: argparse.Namespace) -> int:
    cfg = load_cfg(args.config)
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


def parse_slurm_time_seconds(value: str) -> Optional[int]:
    raw = value.strip()
    if raw in {"UNLIMITED", "NOT_SET", "INVALID"}:
        return None

    day_part = 0
    if "-" in raw:
        days, raw = raw.split("-", 1)
        if not days.isdigit():
            return None
        day_part = int(days)

    parts = raw.split(":")
    if not all(part.isdigit() for part in parts):
        return None
    nums = [int(part) for part in parts]
    if len(nums) == 1:
        hours, minutes, seconds = 0, nums[0], 0
    elif len(nums) == 2:
        hours, minutes, seconds = 0, nums[0], nums[1]
    elif len(nums) == 3:
        hours, minutes, seconds = nums
    else:
        return None
    if minutes >= 60 or seconds >= 60:
        return None
    return (((day_part * 24) + hours) * 60 + minutes) * 60 + seconds


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


def cmd_extend(args: argparse.Namespace) -> int:
    cfg = load_cfg(args.config)
    if shutil.which("scontrol") is None:
        die("scontrol not found on this host", code=127)

    job_id = resolve_slurm_job_id(cfg, args.target, command_name=args.command)
    extra_seconds = parse_duration_seconds(args.duration)

    rc, out, err = run_capture(["scontrol", "show", "job", "-o", job_id])
    if rc != 0:
        die(f"scontrol show job failed:\n{err.strip() or out.strip()}", code=127)

    current_raw = job_field(out, "TimeLimit")
    current_seconds = parse_slurm_time_seconds(current_raw or "")
    if current_raw is None or current_seconds is None:
        die(f"could not parse current TimeLimit for job {job_id}: {current_raw or 'missing'}")

    new_seconds = current_seconds + extra_seconds
    new_limit = format_slurm_time(new_seconds)
    rc, update_out, update_err = run_capture(["scontrol", "update", f"JobId={job_id}", f"TimeLimit={new_limit}"])
    if rc != 0:
        die(f"scontrol update failed:\n{update_err.strip() or update_out.strip()}")

    print(f"extended job:    {job_id}")
    print(f"old time limit:  {current_raw}")
    print(f"added:           {format_slurm_time(extra_seconds)}")
    print(f"new time limit:  {new_limit}")
    return 0


def cmd_profiles(args: argparse.Namespace) -> int:
    cfg = load_cfg(args.config)
    print(f"default: {cfg['default_profile']}")
    for name in profile_names(cfg):
        profile = cfg["profiles"][name]
        enabled = "enabled" if profile["enabled"] else "disabled"
        job_name = job_name_for_profile(cfg, name) or "-"
        print(f"{name}: {enabled}, job={job_name}")
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cs",
        add_help=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=TOP_HELP,
    )
    parser.add_argument("-h", "--help", action="store_true", help=argparse.SUPPRESS)
    add_config_arg(parser)

    subparsers = parser.add_subparsers(dest="command")

    submit = subparsers.add_parser("submit", aliases=["s"], help="Submit a new tunnel session.")
    add_config_arg(submit, default=argparse.SUPPRESS)
    submit.add_argument("profile", nargs="?", help="Profile from codeserver.toml.")
    submit.add_argument(
        "--dry-run",
        action="store_true",
        help="Write session files and show the sbatch command without submitting.",
    )

    status = subparsers.add_parser("status", aliases=["stat", "i"], help="Show session status.")
    add_config_arg(status, default=argparse.SUPPRESS)
    status.add_argument("target", nargs="?", help="latest, profile name, session id, or job id.")

    stop = subparsers.add_parser("stop", aliases=["x"], help="Cancel a session.")
    add_config_arg(stop, default=argparse.SUPPRESS)
    stop.add_argument("target", nargs="?", help="latest, profile name, session id, or job id.")

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

    list_parser = subparsers.add_parser("list", aliases=["l"], help="List known sessions.")
    add_config_arg(list_parser, default=argparse.SUPPRESS)
    list_parser.add_argument("-a", "--active", action="store_true", help="Show only active sessions.")
    list_parser.add_argument("--all", action="store_true", help="Include historical sessions.")
    list_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    proxy = subparsers.add_parser("proxy", aliases=["p"], help="Proxy SSH to the session node.")
    add_config_arg(proxy, default=argparse.SUPPRESS)
    proxy.add_argument("target", nargs="?", help="latest, profile name, session id, job id, or job name.")

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


def legacy_args(
    base: List[str], config: str, value: Optional[str], *, dry_run: bool = False
) -> List[str]:
    out = list(base)
    if value:
        out.append(value)
    if dry_run:
        out.append("--dry-run")
    out.extend(["--config", config])
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
        return call_legacy_main(
            codeserver_submit,
            "cs submit",
            legacy_args([], args.config, args.profile, dry_run=args.dry_run),
        )
    if command in ("status", "stat", "i"):
        return call_legacy_main(
            codeserver_status,
            "cs status",
            legacy_args([], args.config, args.target),
        )
    if command in ("stop", "x"):
        return call_legacy_main(
            codeserver_stop,
            "cs stop",
            legacy_args([], args.config, args.target),
        )
    if command == "extend":
        if args.duration is None:
            if args.target and not args.target.isdigit():
                args.duration = args.target
                args.target = None
            else:
                parser.error("extend requires DURATION, for example: cs extend cpu 10h")
        return cmd_extend(args)
    if command in ("continue", "c"):
        return cmd_continue(args)
    if command in ("list", "l"):
        return cmd_list(args)
    if command in ("proxy", "p"):
        return cmd_proxy(args)
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

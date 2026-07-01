#!/usr/bin/env python3.11
import datetime as dt
import json
import os
import pathlib
import re
import subprocess
import sys
import tomllib
from typing import Any, Dict, List, Optional, Tuple


AUTH_PATTERNS = [
    re.compile(r"How would you like to log in to Visual Studio Code\?"),
    re.compile(r"To grant access to the server, please log into .* and use code [A-Z0-9-]+"),
    re.compile(r"github\.com/login/device"),
    re.compile(r"microsoft\.com/devicelogin"),
]


class ConfigError(RuntimeError):
    pass


def die(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(code)


def script_dir() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent


def default_config_path() -> pathlib.Path:
    return script_dir() / "codeserver.toml"


def load_text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def dump_json(path: pathlib.Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_json(path: pathlib.Path) -> Any:
    return json.loads(load_text(path))


def now_timestamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def session_id_for(profile: str) -> str:
    return f"{now_timestamp()}-{profile}"


def expand_path(s: str) -> pathlib.Path:
    return pathlib.Path(os.path.expandvars(os.path.expanduser(s))).resolve()


def tail_lines(path: pathlib.Path, n: int = 40) -> List[str]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[-n:]


def run_capture(cmd: List[str]) -> Tuple[int, str, str]:
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def query_job_status(job_id: str) -> Optional[str]:
    rc, out, _ = run_capture(
        ["squeue", "-h", "-j", job_id, "-o", "state=%T node=%N elapsed=%M limit=%L partition=%P"]
    )
    if rc == 0 and out.strip():
        return out.strip()

    rc, out, _ = run_capture(
        [
            "sacct",
            "-n",
            "-P",
            "-j",
            job_id,
            "--format=JobIDRaw,State,NodeList,Elapsed,Timelimit,Partition",
        ]
    )
    if rc == 0 and out.strip():
        for line in out.splitlines():
            parts = line.split("|")
            if len(parts) < 6:
                continue
            if parts[0] == job_id:
                return (
                    f"state={parts[1]} node={parts[2] or '-'} "
                    f"elapsed={parts[3]} limit={parts[4]} partition={parts[5]}"
                )
    return None


def state_from_status(status: Optional[str]) -> str:
    """Extract the Slurm state from a query_job_status() line ('state=RUNNING ...')."""
    if not status:
        return "unknown"
    for field in status.split():
        if field.startswith("state="):
            return field.removeprefix("state=")
    return "unknown"


def parse_slurm_seconds(value: str) -> Optional[int]:
    """Parse a Slurm duration (Elapsed/TimeLimit) like D-HH:MM:SS, HH:MM:SS,
    MM:SS, or bare minutes into seconds. Returns None for empty or sentinel
    values (UNLIMITED, N/A, NOT_SET, ...)."""
    raw = value.strip()
    if not raw or raw in {"INVALID", "N/A", "NOT_SET", "UNLIMITED", "Unknown"}:
        return None

    days = 0
    if "-" in raw:
        day_text, raw = raw.split("-", 1)
        if not day_text.isdigit():
            return None
        days = int(day_text)

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
    return (((days * 24) + hours) * 60 + minutes) * 60 + seconds


def find_auth_block(
    path: pathlib.Path, context_before: int = 4, context_after: int = 12
) -> Optional[str]:
    if not path.exists():
        return None
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for idx, line in enumerate(lines):
        if any(p.search(line) for p in AUTH_PATTERNS):
            start = max(0, idx - context_before)
            end = min(len(lines), idx + context_after + 1)
            return "\n".join(lines[start:end])
    return None


def ensure_mapping(value: Any, ctx: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{ctx} must be a table/mapping")
    return value


def ensure_string(value: Any, ctx: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{ctx} must be a string")
    return value


def ensure_bool(value: Any, ctx: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{ctx} must be a boolean")
    return value


def ensure_list_of_strings(value: Any, ctx: str) -> List[str]:
    if not isinstance(value, list):
        raise ConfigError(f"{ctx} must be a list")
    out: List[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str):
            raise ConfigError(f"{ctx}[{i}] must be a string")
        out.append(item)
    return out


def ensure_str_dict(value: Any, ctx: str) -> Dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{ctx} must be a table/mapping")
    out: Dict[str, str] = {}
    for k, v in value.items():
        if not isinstance(k, str):
            raise ConfigError(f"{ctx} contains a non-string key")
        if not isinstance(v, str):
            raise ConfigError(f"{ctx}.{k} must be a string")
        out[k] = v
    return out


def load_config(config_path: pathlib.Path) -> Dict[str, Any]:
    with config_path.open("rb") as f:
        raw = tomllib.load(f)

    if not isinstance(raw, dict):
        raise ConfigError("top-level TOML document must be a table")

    cfg: Dict[str, Any] = {}
    cfg["config_path"] = str(config_path.resolve())
    cfg["root_dir"] = ensure_string(raw["root_dir"], "root_dir")
    cfg["default_profile"] = ensure_string(raw["default_profile"], "default_profile")
    cfg["code_bin"] = ensure_string(raw["code_bin"], "code_bin")
    cfg["code_tunnel_args"] = ensure_list_of_strings(raw["code_tunnel_args"], "code_tunnel_args")
    cfg["env"] = ensure_str_dict(raw.get("env", {}), "env")

    profiles_raw = ensure_mapping(raw.get("profiles"), "profiles")
    if not profiles_raw:
        raise ConfigError("profiles must not be empty")

    profiles: Dict[str, Dict[str, Any]] = {}
    for name, prof_raw in profiles_raw.items():
        if not isinstance(name, str):
            raise ConfigError("profile names must be strings")
        prof = ensure_mapping(prof_raw, f"profiles.{name}")

        profiles[name] = {
            "enabled": ensure_bool(prof.get("enabled", True), f"profiles.{name}.enabled"),
            "sbatch_args": ensure_list_of_strings(
                prof.get("sbatch_args", []), f"profiles.{name}.sbatch_args"
            ),
            "pre_commands": ensure_list_of_strings(
                prof.get("pre_commands", []), f"profiles.{name}.pre_commands"
            ),
            "env": ensure_str_dict(prof.get("env", {}), f"profiles.{name}.env"),
            "max_time": prof.get("max_time"),
            "default_time": prof.get("default_time"),
            "relay_overlap": prof.get("relay_overlap"),
            "relay_ready_timeout": prof.get("relay_ready_timeout"),
            "relay_enabled": ensure_bool(
                prof.get("relay_enabled", True), f"profiles.{name}.relay_enabled"
            ),
        }
        for key in ("max_time", "default_time", "relay_overlap", "relay_ready_timeout"):
            if profiles[name][key] is not None:
                ensure_string(profiles[name][key], f"profiles.{name}.{key}")

    if cfg["default_profile"] not in profiles:
        raise ConfigError("default_profile is not present in profiles")

    cfg["profiles"] = profiles
    return cfg


def profile_names(cfg: Dict[str, Any]) -> List[str]:
    return sorted(cfg["profiles"].keys())


def get_profile(cfg: Dict[str, Any], name: str) -> Dict[str, Any]:
    if name not in cfg["profiles"]:
        raise ConfigError(f"unknown profile '{name}'")
    prof = cfg["profiles"][name]
    if not prof["enabled"]:
        raise ConfigError(f"profile '{name}' is disabled")
    return prof


def merged_env(cfg: Dict[str, Any], profile_name: str) -> Dict[str, str]:
    prof = get_profile(cfg, profile_name)
    env = dict(cfg.get("env", {}))
    env.update(prof.get("env", {}))
    return env


def ensure_root_dirs(cfg: Dict[str, Any]) -> pathlib.Path:
    root = expand_path(cfg["root_dir"])
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "state").mkdir(parents=True, exist_ok=True)
    return root


def is_chain_dir(path: pathlib.Path) -> bool:
    return (path / "chain.json").exists()


def resolve_session_dir(cfg: Dict[str, Any], target: str) -> pathlib.Path:
    """Resolve a target (latest, profile name, session/chain id, or job id) to a
    session or relay-chain directory."""
    root_dir = expand_path(cfg["root_dir"])
    state_dir = root_dir / "state"
    logs_dir = root_dir / "logs"

    if target == "latest":
        link = state_dir / "current"
        if not link.exists():
            raise FileNotFoundError(f"no current session symlink at {link}")
        return link.resolve()

    if target in cfg["profiles"]:
        link = state_dir / f"current-{target}"
        if not link.exists():
            raise FileNotFoundError(f"no current session symlink for profile '{target}' at {link}")
        return link.resolve()

    if target.isdigit() and logs_dir.exists():
        for chain_path in sorted(logs_dir.glob("*/chain.json"), reverse=True):
            try:
                chain = load_json(chain_path)
            except (OSError, json.JSONDecodeError):
                continue
            for job in chain.get("jobs", []):
                if str(job.get("job_id", "")) == target:
                    return chain_path.parent.resolve()
        for meta_path in sorted(logs_dir.glob("*/meta.json"), reverse=True):
            try:
                meta = load_json(meta_path)
            except (OSError, json.JSONDecodeError):
                continue
            if str(meta.get("job_id", "")) == target:
                return meta_path.parent.resolve()

    return (logs_dir / target).resolve()

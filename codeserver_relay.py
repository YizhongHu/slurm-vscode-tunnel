#!/usr/bin/env python3.11
import re
from typing import Any, Dict, List, Optional

from codeserver_lib import ConfigError

READY_PATTERNS = [
    re.compile(r"\bREADY\b"),
    re.compile(r"Open this link in your browser", re.IGNORECASE),
    re.compile(r"Server started", re.IGNORECASE),
    re.compile(r"session is running", re.IGNORECASE),
    re.compile(r"tunnel.+(ready|running|started|listening)", re.IGNORECASE),
    re.compile(r"(ready|running|started|listening).+tunnel", re.IGNORECASE),
]


def parse_duration(value: str) -> int:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("duration must be a non-empty string")
    text = value.strip().lower()
    m = re.fullmatch(r"(\d+)([smhd])", text)
    if m:
        amount = int(m.group(1))
        return amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]

    parts = text.split(":")
    if len(parts) == 2:
        hours = "0"
        minutes, seconds = parts
    elif len(parts) == 3:
        hours, minutes, seconds = parts
    else:
        raise ConfigError(f"invalid duration '{value}'")
    try:
        h = int(hours)
        m_ = int(minutes)
        s = int(seconds)
    except ValueError as exc:
        raise ConfigError(f"invalid duration '{value}'") from exc
    if h < 0 or m_ < 0 or s < 0 or m_ >= 60 or s >= 60:
        raise ConfigError(f"invalid duration '{value}'")
    return h * 3600 + m_ * 60 + s


def format_duration(seconds: int) -> str:
    h, rem = divmod(max(0, seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def sbatch_arg_value(args: List[str], name: str) -> Optional[str]:
    prefix = f"--{name}="
    for i, arg in enumerate(args):
        if arg.startswith(prefix):
            return arg.split("=", 1)[1]
        if arg == f"--{name}" and i + 1 < len(args):
            return args[i + 1]
    return None


def filter_sbatch_args(args: List[str], names: List[str]) -> List[str]:
    out: List[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        matched = False
        for name in names:
            if arg == f"--{name}":
                skip_next = True
                matched = True
                break
            if arg.startswith(f"--{name}="):
                matched = True
                break
        if not matched:
            out.append(arg)
    return out


def profile_time(profile: Dict[str, Any], key: str) -> Optional[int]:
    value = profile.get(key)
    if value:
        return parse_duration(str(value))
    if key in ("max_time", "default_time"):
        sbatch_time = sbatch_arg_value(list(profile.get("sbatch_args", [])), "time")
        if sbatch_time:
            return parse_duration(sbatch_time)
    return None


def slurm_begin_offset(seconds: int) -> str:
    # Slurm supports begin specs like now+5minutes. Round up so relay starts no earlier than planned.
    minutes = max(1, (seconds + 59) // 60)
    return f"now+{minutes}minutes"


def plan_relay(requested: int, max_time: int, overlap: int) -> List[Dict[str, int]]:
    if requested <= 0:
        raise ConfigError("requested duration must be positive")
    if max_time <= 0:
        raise ConfigError("max_time must be positive")
    if overlap < 0 or overlap >= max_time:
        raise ConfigError("relay_overlap must be smaller than max_time")
    if requested <= max_time:
        return [{"index": 1, "begin_offset": 0, "duration": requested, "coverage_end": requested}]

    step = max_time - overlap
    segments: List[Dict[str, int]] = []
    begin = 0
    index = 1
    while begin < requested:
        remaining = requested - begin
        duration = min(max_time, remaining)
        # Keep every non-final segment alive through the next overlap window.
        if remaining > max_time and duration < max_time:
            duration = max_time
        segments.append(
            {
                "index": index,
                "begin_offset": begin,
                "duration": duration,
                "coverage_end": min(requested, begin + duration),
            }
        )
        if begin + max_time >= requested:
            break
        begin += step
        index += 1
    return segments

#!/usr/bin/env python3.11
import argparse
import pathlib
import subprocess
import tomllib
from typing import List

from codeserver_lib import (
    ConfigError,
    default_config_path,
    die,
    is_chain_dir,
    load_config,
    load_json,
    resolve_session_dir,
)


def scancel(job_id: str) -> None:
    proc = subprocess.run(
        ["scancel", str(job_id)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        die(f"scancel {job_id} failed:\n{proc.stderr.strip() or proc.stdout.strip()}")


def stop_chain(session_dir: pathlib.Path) -> int:
    chain = load_json(session_dir / "chain.json")
    job_ids: List[str] = []
    for job in chain.get("jobs", []):
        job_id = str(job.get("job_id") or "")
        if job_id and not job_id.startswith("DRY-RUN"):
            scancel(job_id)
            job_ids.append(job_id)
    print(f"stopped relay chain: {chain.get('chain_id', session_dir.name)}")
    print(f"profile:             {chain.get('profile', '-')}")
    print(f"canceled jobs:       {', '.join(job_ids) if job_ids else '-'}")
    print(f"chain dir:           {session_dir}")
    return 0


def stop_session(session_dir: pathlib.Path, target: str) -> int:
    meta_path = session_dir / "meta.json"
    if not meta_path.exists():
        die(f"missing metadata: {meta_path}")

    meta = load_json(meta_path)
    job_id = meta.get("job_id")
    if not job_id:
        die(f"session '{meta.get('session_id', target)}' has no job_id to stop")
    if not str(job_id).startswith("DRY-RUN"):
        scancel(str(job_id))

    print(f"stopped session: {meta.get('session_id', target)}")
    print(f"profile:         {meta.get('profile', '-')}")
    print(f"job id:          {job_id}")
    print(f"session dir:     {session_dir}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Stop a session or relay chain.")
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

    if is_chain_dir(session_dir):
        return stop_chain(session_dir)
    return stop_session(session_dir, args.target)


if __name__ == "__main__":
    raise SystemExit(main())

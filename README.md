Slurm VS Code Tunnel
=====================

`slurm-vscode-tunnel` is a small Python CLI for running VS Code `code tunnel` inside Slurm allocations. It submits CPU or GPU tunnel jobs, persists session metadata and logs, detects device-login prompts, reports Slurm state, cancels sessions, and can proxy SSH to the allocated compute node.

Layout
------

- `cs`: primary command-line entry point.

Runtime files are written under `runs/` by default. That directory contains
generated logs, session metadata, and `state/current*` symlinks.

Requirements
------------

- Python 3.11 or newer.
- Slurm commands available on the login node: `sbatch`, `squeue`, `sacct`,
  and `scancel`.
- VS Code CLI available as `code` on the compute node.
- For `codeserver-proxy`, one of `nc`, `socat`, or an SSH setup that supports
  `ssh -W`.

Profiles
--------

Profiles live in `codeserver.toml`.

- `cpu`: default profile, submits to the `day` partition with 4 CPUs, 32 GB RAM,
  and a 24 hour limit.
- `gpu`: submits to the `gpu_devel` partition with 1 GPU, 4 CPUs, 32 GB RAM,
  and a 6 hour limit.

Edit `codeserver.toml` if partitions, resource limits, or environment variables
need to change.

Usage
-----

`cs` is the preferred interface:

```text
usage: cs [command] [options] [target]

Manage VS Code tunnel sessions on Slurm.

commands:
  submit, s [profile]        Submit a new code tunnel session.
  status, stat, i [target]   Show status for latest, profile, session id, or job id.
  list, l                    List known sessions and Slurm state.
  stop, x [target]           Cancel a session by latest, profile, session id, or job id.
  proxy, p [profile|job]     Proxy stdin/stdout to SSH on the session node.
  profiles                   Show configured profiles.
  config                     Show resolved config path and key settings.

global options:
  -h, --help                 Show this help message and exit.
  -c, --config PATH          Use a custom codeserver.toml.

short action flags:
  -s [profile]               Alias for: submit [profile]
  -i [target]                Alias for: status [target]
  -l                         Alias for: list
  -x [target]                Alias for: stop [target]
  -p [profile|job]           Alias for: proxy [profile|job]
```

Submit the default CPU tunnel:

```bash
cs submit
cs s
```

Submit a GPU tunnel:

```bash
cs submit gpu
cs -s gpu
```

Check the most recent session:

```bash
cs status
cs -i
```

Check a specific profile, session id, or Slurm job id:

```bash
cs status cpu
cs status gpu
cs status 20260501-120000-cpu
cs status 1234567
```

List known sessions:

```bash
cs list
cs list --active
cs list --json
```

Stop the latest session:

```bash
cs stop
cs -x
```

Stop a specific profile, session id, or Slurm job id:

```bash
cs stop gpu
cs stop 20260501-120000-gpu
cs stop 1234567
```

Proxy to the SSH port of the node running a matching Slurm job:

```bash
cs proxy cpu
cs proxy gpu
cs proxy codeserver-cpu
```

Show configuration:

```bash
cs profiles
cs config
```

The older `codeserver_submit.py`, `codeserver_status.py`, `codeserver_stop.py`,
and `codeserver-proxy` commands are kept as compatibility wrappers/tools.

Authentication
--------------

The first run may require VS Code tunnel authentication. Run `cs status` after
submitting a job. If the logs contain a device-login prompt, the status command
prints the relevant block and reports `NEEDS_REAUTH=yes`.

Command Setup
-------------

The scripts are executable. To run them as commands from any directory, make
sure this folder is on `PATH`:

```bash
export PATH="$HOME/Apps/codeserver:$PATH"
```

The current shell setup in `~/.bashrc` includes that path.

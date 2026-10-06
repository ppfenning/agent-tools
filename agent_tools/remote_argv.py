"""Pure argv builders for ssh, rsync and git fetch; a location is a caller-formed string."""

import re
import shlex

# Every lane-host call must give up within this many seconds.
LANE_HOST_TIMEOUT_S = 30
_ALIVE_INTERVAL_S = 10
_ALIVE_COUNT_MAX = LANE_HOST_TIMEOUT_S // _ALIVE_INTERVAL_S
_CONNECT_OPTION = f"ConnectTimeout={LANE_HOST_TIMEOUT_S}"
# No BatchMode: Tailscale SSH check mode must still print its login URL.
_SSH_BOUND_OPTIONS = [
    "-o", _CONNECT_OPTION,
    "-o", f"ServerAliveInterval={_ALIVE_INTERVAL_S}",
    "-o", f"ServerAliveCountMax={_ALIVE_COUNT_MAX}",
]
_RSYNC_BOUND_OPTIONS = ["-a", f"--timeout={LANE_HOST_TIMEOUT_S}", "-e", f"ssh -o {_CONNECT_OPTION}"]


def ssh_argv(ssh: str, remote_argv: list[str]) -> list[str]:
    """`ssh` is a destination such as user@host; the remote shell sees one quoted command."""
    return ["ssh", *_SSH_BOUND_OPTIONS, ssh, shlex.join(remote_argv)]


def doctor_argv() -> list[str]:
    return ["cox", "setup", "doctor"]


def auth_status_argv() -> list[str]:
    return ["claude", "auth", "status"]


def env_check_argv(var: str) -> list[str]:
    return ["printenv", var]


def launch_argv(initiative: str, run_id: str, label: str) -> list[str]:
    return [
        "cox", "route", "launch", "epic",
        "--initiative", initiative,
        "--run-id", run_id,
        "--label", label,
        "--no-claim",
    ]


def rsync_push_argv(src_dir: str, dest_location: str, *, mirror: bool = False) -> list[str]:
    """Trailing slashes on both ends copy directory contents; deletes at the destination only when `mirror`."""
    return ["rsync", *_RSYNC_BOUND_OPTIONS, *(["--delete"] if mirror else []), src_dir.rstrip("/") + "/", dest_location.rstrip("/") + "/"]


def rsync_pull_argv(src_location: str, dest: str) -> list[str]:
    """One directory or one file, exactly as the caller formed it."""
    return ["rsync", *_RSYNC_BOUND_OPTIONS, src_location, dest]


def git_fetch_argv(repo_location: str, run: str) -> list[str]:
    """The initiative is `run` without a trailing `-<digits>` phase/attempt suffix."""
    initiative = re.sub(r"-\d+$", "", run)
    run_refspec = f"refs/heads/agents/{run}/*:refs/heads/agents/{run}/*"
    phase_refspec = f"+refs/heads/epic/{initiative}/*:refs/heads/epic/{initiative}/*"
    return ["git", "fetch", repo_location, run_refspec, phase_refspec]


# Each guard exits before any later line runs: a missing path must not fall through to
# whatever repository the ssh session starts in. Untracked files are not "dirty" here.
_SYNC_SCRIPT = "\n".join((
    'cd "$1" 2>/dev/null || { echo "sync: cannot enter $1" >&2; exit 1; }',
    'top=$(git rev-parse --show-toplevel 2>/dev/null)',
    '[ "$top" = "$(pwd -P)" ] || { echo "sync: $1 is not the root of a git checkout" >&2; exit 1; }',
    "default=$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##')",
    'default=${default:-main}',
    'current=$(git symbolic-ref --short -q HEAD)',
    '[ "$current" = "$default" ] || '
    '{ echo "sync: checkout is on ${current:-a detached HEAD}, not $default" >&2; exit 1; }',
    '[ -z "$(git status --porcelain --untracked-files=no)" ] || '
    '{ echo "sync: tracked changes in the working tree" >&2; exit 1; }',
    'git fetch origin && git merge --ff-only "origin/$default"',
))


def sync_argv(repo_path: str) -> list[str]:
    """Fast-forward `repo_path` to origin's default branch, or print one reason to stderr and exit 1."""
    return ["bash", "-c", _SYNC_SCRIPT, "sync", repo_path]


def epic_refspec(initiative: str) -> str:
    return f"refs/heads/epic/{initiative}/*:refs/heads/epic/{initiative}/*"


def agent_refspec(run: str, task: str) -> str:
    return f"refs/heads/agents/{run}/{task}:refs/heads/agents/{run}/{task}"


def branch_push_argv(local_repo: str, ssh: str, repo_path: str, refspec: str) -> list[str]:
    """`-C local_repo` because the executor's cwd is the work store, not the repo holding the branches."""
    return ["git", "-C", local_repo, "push", "--force", f"ssh://{ssh}/{repo_path.lstrip('/')}", refspec]

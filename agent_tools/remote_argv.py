"""Pure argv builders for ssh, rsync and git fetch; a location is a caller-formed string."""

import shlex


def ssh_argv(ssh: str, remote_argv: list[str]) -> list[str]:
    """`ssh` is a destination such as user@host; the remote shell sees one quoted command."""
    return ["ssh", ssh, shlex.join(remote_argv)]


def doctor_argv() -> list[str]:
    return ["cox", "setup", "doctor"]


def auth_status_argv() -> list[str]:
    return ["claude", "auth", "status"]


def launch_argv(initiative: str, run_id: str, label: str) -> list[str]:
    return [
        "cox", "route", "launch", "epic",
        "--initiative", initiative,
        "--run-id", run_id,
        "--label", label,
        "--no-claim",
    ]


def rsync_push_argv(src_dir: str, dest_location: str) -> list[str]:
    """Trailing slashes on both ends copy directory contents; never deletes."""
    return ["rsync", "-a", src_dir.rstrip("/") + "/", dest_location.rstrip("/") + "/"]


def rsync_pull_argv(src_location: str, dest: str) -> list[str]:
    """One directory or one file, exactly as the caller formed it."""
    return ["rsync", "-a", src_location, dest]


def git_fetch_argv(repo_location: str, run: str) -> list[str]:
    refspec = f"refs/heads/agents/{run}/*:refs/heads/agents/{run}/*"
    return ["git", "fetch", repo_location, refspec]

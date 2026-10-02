"""A drop folder: every `*.md` directly in a configured folder is an intake candidate.

The profile's `sources.folder.repos` lists folders (`~` allowed), and `repo_map` maps each folder,
spelled as configured, to the repository its tickets target. A file's first `# heading` is its title,
else its name; the rest is the body. Filing moves the file into the folder's `done/`, so the folder
holds only what is still waiting. The link carries a digest of the text, so a file dropped again
under an old name, with new text, files again.
"""

from __future__ import annotations

import hashlib
import shlex
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from agent_tools.sources import Candidate, Ref, SourceConfig

DONE = "done"
_LIST = (
    "import json, pathlib, sys\n"
    "root = pathlib.Path(sys.argv[1]).expanduser()\n"
    "files = sorted(root.glob('*.md')) if root.is_dir() else []\n"
    "json.dump([{'folder': sys.argv[1], 'path': str(p.resolve()), 'text': p.read_text(encoding='utf-8')}"
    " for p in files if p.is_file()], sys.stdout)\n"
)


def _link(raw: Mapping) -> str:
    digest = hashlib.sha256(raw["text"].encode("utf-8")).hexdigest()[:12]
    return f"file://{raw['path']}#{digest}"


def _path(link: str) -> Path:
    return Path(link.removeprefix("file://").partition("#")[0])


def _title_and_body(raw: Mapping) -> tuple[str, str]:
    lines = raw["text"].strip().splitlines()
    if lines and lines[0].startswith("# "):
        return lines[0][2:].strip(), "\n".join(lines[1:]).strip()
    stem = Path(raw["path"]).stem.replace("-", " ").replace("_", " ").strip()
    return stem, "\n".join(lines).strip()


def candidates(config: SourceConfig, listing: Sequence[Mapping]) -> tuple[Ref, ...]:
    return tuple(Ref(link=_link(raw), repo=raw["folder"]) for raw in listing if raw["folder"] in config.repos)


def read(raw: Mapping) -> Candidate:
    title, body = _title_and_body(raw)
    return Candidate(title=title, body=body, repo=raw["folder"], link=_link(raw))


def taken(link: str, intake_links: frozenset[str]) -> bool:
    return link in intake_links


def list_argv(config: SourceConfig, repo: str) -> list[str]:
    """`repo` is one configured folder; a missing folder lists nothing rather than failing the pull."""
    return [sys.executable, "-c", _LIST, repo]


def mark_argv(ref: Ref, intake_path: str) -> list[str]:
    path = _path(ref.link)
    done = shlex.quote(str(path.parent / DONE))
    return ["sh", "-c", f"mkdir -p {done} && mv {shlex.quote(str(path))} {done}/"]

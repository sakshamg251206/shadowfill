"""Run provenance: what produced a number, not just what the number is.

Invariant 7 says every run writes a manifest pinning the git SHA, the input
hash and the full config. Both the ground-truth runner and the dataset
extractor have to do that identically -- two copies of a manifest builder is
how a field silently stops matching between them.
"""

from __future__ import annotations

import hashlib
import platform
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa


def sha256_file(path: str | Path) -> str:
    """Stream a file through SHA-256, so a 3.5 GB input needs no more memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def describe_head(repo: str | Path | None = None) -> str:
    """HEAD's commit, suffixed ``-dirty`` if the working tree differs from it.

    Untracked files count: a new module can be imported by the run, so a tree
    with one is not the commit it claims to be. Returns ``"unknown"`` outside a
    checkout.
    """
    cwd = None if repo is None else str(repo)
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL, cwd=cwd
        ).strip()
        status = subprocess.check_output(
            ["git", "status", "--porcelain"], text=True, stderr=subprocess.DEVNULL, cwd=cwd
        )
    except (subprocess.CalledProcessError, FileNotFoundError, NotADirectoryError):
        return "unknown"
    return f"{sha}-dirty" if status.strip() else sha


#: Read once, when this module is first imported -- which is when a run starts,
#: since every runner imports it at the top. The first version read HEAD when
#: the manifest was *written*, at the end: a latency sweep started at 52239a4
#: finished after f4aeb8c was committed and was credited to f4aeb8c, code it
#: never ran. Capturing at start makes the pin describe what was loaded.
_AT_START = describe_head()


def git_sha() -> str:
    """The commit that produced this run, as of when the run started.

    ``-dirty`` means the run included uncommitted changes, so no commit fully
    describes it; a result carrying that suffix should be regenerated from a
    clean tree before it is cited.
    """
    return _AT_START


def environment() -> dict[str, str]:
    """Amendment H: pin the interpreter, platform and library versions."""
    env = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "pyarrow": pa.__version__,
        "compiler": "n/a (python engine)",
    }
    try:
        from shadowfill import _core

        env["compiler"] = str(_core.compiler)
    except ImportError:
        pass
    return env

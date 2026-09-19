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


def git_sha() -> str:
    """The commit that produced a run, or ``"unknown"`` outside a checkout."""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


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

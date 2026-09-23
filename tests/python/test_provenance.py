"""Invariant 7: a manifest must name the code that actually ran.

Two ways the original git_sha() could name the wrong code, both observed on
2026-09-24: it read HEAD when the manifest was *written*, at the end of a run,
so a commit made mid-run was credited with a result it did not produce; and it
said nothing when the working tree had uncommitted changes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from shadowfill.provenance import describe_head


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "code.py").write_text("x = 1\n")
    _git(tmp_path, "add", "code.py")
    _git(tmp_path, "commit", "-qm", "one")
    return tmp_path


def test_a_clean_tree_is_reported_as_the_bare_commit(repo):
    assert describe_head(repo) == _git(repo, "rev-parse", "HEAD")


def test_an_edited_tracked_file_marks_the_commit_dirty(repo):
    (repo / "code.py").write_text("x = 2\n")
    assert describe_head(repo) == _git(repo, "rev-parse", "HEAD") + "-dirty"


def test_a_new_untracked_file_marks_the_commit_dirty(repo):
    """An untracked module can be imported by the run, so it counts."""
    (repo / "new_module.py").write_text("y = 1\n")
    assert describe_head(repo).endswith("-dirty")


def test_outside_a_checkout_it_says_unknown(tmp_path):
    assert describe_head(tmp_path / "not-a-repo") == "unknown"


def test_git_sha_is_fixed_for_the_life_of_the_process():
    """Read once, at import -- i.e. when a run starts -- never at write time."""
    from shadowfill import provenance

    assert provenance.git_sha() == provenance.git_sha() == provenance._AT_START

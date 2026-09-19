# Coinbase L3 Recorder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record every Coinbase `level3` message for a configured set of products, verbatim and durably, with every discontinuity detected and written down.

**Architecture:** One asyncio process. A reader task timestamps frames and enqueues them; a writer task appends the raw bytes to a zstd-compressed append-only tape *before* parsing them, then validates sequence numbers into a side-car ledger. Under backpressure the pipeline blocks rather than drops. Reconnection opens a new session, fetches a REST level-3 snapshot into the tape, and records the boundary.

**Tech Stack:** Python 3.11, asyncio, `websockets`, `zstandard`, pytest.

**Spec:** `docs/superpowers/specs/2026-09-19-coinbase-l3-recorder-design.md` — read it before Task 1.

## Global Constraints

- Python 3.11. `ruff` line-length 100, lint select `E,F,I,N,UP,B,SIM,RUF`. `mypy` strict over `python/shadowfill`.
- `known-first-party = ["shadowfill"]` in ruff isort config; do not merge `shadowfill` imports into the third-party block.
- No network in CI, ever. Every test in this plan runs offline except Task 10, which is marked `needs_network` and is never run by CI.
- No third-party data and no recorded tape is committed to the repository.
- API credentials come from the environment. They are never logged, never written to a manifest, never committed, and never appear in a test fixture as a real value.
- The tape is append-only and never edited, filtered or reordered. Gaps are recorded, never repaired.
- Every artifact that leaves this system carries provenance: git SHA, SHA-256 of the bytes, and the resolved environment. This mirrors Plan 1's run manifests.
- Commit at every step boundary using the exact messages given. Never mark a step complete without running the command and reading its output.

## File Structure

| File | Responsibility |
|---|---|
| `python/shadowfill/provenance.py` | shared git SHA / file SHA-256 / environment helpers (extracted from `ground_truth.py`) |
| `python/shadowfill/record/__init__.py` | package marker, public re-exports |
| `python/shadowfill/record/sequence.py` | `SequenceTracker` — pure per-product sequence classification |
| `python/shadowfill/record/tape.py` | framing, `TapeWriter`, `read_tape` |
| `python/shadowfill/record/ledger.py` | `GapLedger` — append-only JSONL of discontinuities |
| `python/shadowfill/record/manifest.py` | `SessionManifest` — summarise and hash a finished tape |
| `python/shadowfill/record/transport.py` | `Transport` protocol, `FakeTransport` |
| `python/shadowfill/record/coinbase.py` | signing, subscribe payload, `CoinbaseTransport`, snapshot fetch |
| `python/shadowfill/record/recorder.py` | `Recorder` — task wiring, backpressure, rotation, reconnect |
| `python/shadowfill/record/__main__.py` | CLI entry point |
| `tests/python/test_record_sequence.py` | Task 1 |
| `tests/python/test_record_tape.py` | Task 2 |
| `tests/python/test_record_ledger.py` | Task 3 |
| `tests/python/test_record_manifest.py` | Task 4 |
| `tests/python/test_record_transport.py` | Task 5 |
| `tests/python/test_record_coinbase.py` | Task 6 |
| `tests/python/test_record_recorder.py` | Tasks 7–8 |
| `tests/python/test_record_live.py` | Task 10, `needs_network` |
| `deploy/shadowfill-recorder.service` | systemd unit |

---

## Task 1: Per-product sequence tracking

**Files:**
- Create: `python/shadowfill/record/__init__.py`, `python/shadowfill/record/sequence.py`
- Test: `tests/python/test_record_sequence.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `SeqStatus` (enum: `FIRST`, `OK`, `GAP`, `STALE`), `SeqResult(status, product_id, got, expected, missing)`, `SequenceTracker.observe(product_id: str, sequence: int) -> SeqResult`.

**Context for the engineer:** Coinbase sequence numbers increase by exactly one per product. A jump means messages were lost in transit. A value at or below the last seen is a duplicate or a late arrival, and must **not** move the tracker backwards — if it did, the next in-order message would look like a gap and you would manufacture a discontinuity that never happened. `noop` messages carry sequence numbers and no book change; they exist precisely to keep the sequence contiguous, so they are observed like anything else.

- [ ] **Step 1: Write the failing test**

`tests/python/test_record_sequence.py`:
```python
from shadowfill.record.sequence import SeqStatus, SequenceTracker


def test_first_message_for_a_product_has_no_expectation():
    t = SequenceTracker()
    r = t.observe("BTC-USD", 100)
    assert r.status is SeqStatus.FIRST
    assert r.expected is None
    assert r.missing == 0


def test_consecutive_sequences_are_ok():
    t = SequenceTracker()
    t.observe("BTC-USD", 100)
    r = t.observe("BTC-USD", 101)
    assert r.status is SeqStatus.OK
    assert r.missing == 0


def test_a_jump_is_a_gap_and_counts_what_was_missed():
    t = SequenceTracker()
    t.observe("BTC-USD", 100)
    r = t.observe("BTC-USD", 105)
    assert r.status is SeqStatus.GAP
    assert r.expected == 101
    assert r.got == 105
    assert r.missing == 4


def test_a_repeat_is_stale_and_does_not_move_the_tracker_backwards():
    """A late duplicate must not make the next in-order message look like a gap."""
    t = SequenceTracker()
    t.observe("BTC-USD", 100)
    t.observe("BTC-USD", 101)
    stale = t.observe("BTC-USD", 100)
    assert stale.status is SeqStatus.STALE
    assert t.observe("BTC-USD", 102).status is SeqStatus.OK


def test_products_are_tracked_independently():
    t = SequenceTracker()
    t.observe("BTC-USD", 100)
    t.observe("ETH-USD", 900)
    assert t.observe("BTC-USD", 101).status is SeqStatus.OK
    assert t.observe("ETH-USD", 901).status is SeqStatus.OK


def test_gap_then_resume_is_ok_not_a_second_gap():
    t = SequenceTracker()
    t.observe("BTC-USD", 100)
    t.observe("BTC-USD", 105)
    assert t.observe("BTC-USD", 106).status is SeqStatus.OK
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/python/test_record_sequence.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'shadowfill.record'`

- [ ] **Step 3: Write minimal implementation**

`python/shadowfill/record/__init__.py`:
```python
"""Venue recorders. Writes raw exchange tape; interprets nothing."""
```

`python/shadowfill/record/sequence.py`:
```python
"""Per-product sequence validation for an exchange feed.

Pure and synchronous on purpose: this is the one piece of the recorder whose
correctness decides whether a tape can be trusted, so it is testable without a
socket, a clock or a filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class SeqStatus(Enum):
    """How one message's sequence number relates to what was expected."""

    FIRST = "first"
    OK = "ok"
    GAP = "gap"
    STALE = "stale"


@dataclass(frozen=True)
class SeqResult:
    status: SeqStatus
    product_id: str
    got: int
    expected: int | None
    missing: int


@dataclass
class SequenceTracker:
    """Classifies each message against the last sequence seen for its product."""

    last: dict[str, int] = field(default_factory=dict)

    def observe(self, product_id: str, sequence: int) -> SeqResult:
        previous = self.last.get(product_id)
        if previous is None:
            self.last[product_id] = sequence
            return SeqResult(SeqStatus.FIRST, product_id, sequence, None, 0)

        expected = previous + 1
        if sequence == expected:
            self.last[product_id] = sequence
            return SeqResult(SeqStatus.OK, product_id, sequence, expected, 0)

        if sequence <= previous:
            # A duplicate or a late arrival. Deliberately does not rewind
            # `last`: rewinding would make the next in-order message look like
            # a gap and invent a discontinuity that never happened.
            return SeqResult(SeqStatus.STALE, product_id, sequence, expected, 0)

        self.last[product_id] = sequence
        return SeqResult(SeqStatus.GAP, product_id, sequence, expected, sequence - expected)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/python/test_record_sequence.py -v`
Expected: PASS, 6 passed

- [ ] **Step 5: Commit**

```bash
git add python/shadowfill/record/__init__.py python/shadowfill/record/sequence.py tests/python/test_record_sequence.py
git commit -m "feat(record): per-product sequence validation"
```

---

## Task 2: The raw tape

**Files:**
- Create: `python/shadowfill/record/tape.py`
- Modify: `pyproject.toml` (add `zstandard` to `dependencies`)
- Test: `tests/python/test_record_tape.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `frame(recv_ts_ns: int, payload: bytes) -> bytes`, `frame_snapshot(recv_ts_ns: int, product_id: str, payload: bytes) -> bytes`, `TapeWriter(path)` with `.write(line: bytes)`, `.flush()`, `.close()`, `.messages_written`, `.bytes_written`; `read_tape(path) -> Iterator[dict]`.

**Context for the engineer:** The tape must contain exactly the bytes the venue sent. `frame` splices the raw payload into the line rather than decoding and re-encoding it, which is both cheaper and the only way to guarantee byte-identity. A snapshot is not a socket frame, so it carries `"k":"snapshot"`; absence of `k` means it came off the socket.

`flush()` ends the zstd frame and fsyncs. That costs a little compression ratio at each boundary and buys you a tape that is readable up to the last flush if the machine dies — the right trade for data you cannot re-record.

- [ ] **Step 1: Write the failing test**

`tests/python/test_record_tape.py`:
```python
import json

from shadowfill.record.tape import TapeWriter, frame, frame_snapshot, read_tape


def test_frame_embeds_the_payload_byte_for_byte():
    payload = b'{"type":"open","product_id":"BTC-USD","sequence":7}'
    line = frame(1_700_000_000_123_456_789, payload)
    assert payload in line
    record = json.loads(line)
    assert record["t"] == 1_700_000_000_123_456_789
    assert record["m"]["sequence"] == 7
    assert "k" not in record


def test_snapshot_frames_are_distinguishable_from_socket_frames():
    line = frame_snapshot(1, "BTC-USD", b'{"bids":[],"asks":[],"sequence":42}')
    record = json.loads(line)
    assert record["k"] == "snapshot"
    assert record["product_id"] == "BTC-USD"
    assert record["m"]["sequence"] == 42


def test_payload_containing_braces_and_newlines_round_trips(tmp_path):
    """JSON escaping means a newline inside a string can never split a tape line."""
    payload = json.dumps({"note": "a}b{c\nd", "sequence": 1}).encode()
    path = tmp_path / "tape.jsonl.zst"
    writer = TapeWriter(path)
    writer.write(frame(5, payload))
    writer.close()
    (record,) = list(read_tape(path))
    assert record["m"]["note"] == "a}b{c\nd"


def test_every_message_appears_exactly_once_in_order(tmp_path):
    """The recorder's whole contract, at the storage layer."""
    payloads = [json.dumps({"sequence": i}).encode() for i in range(500)]
    path = tmp_path / "tape.jsonl.zst"
    writer = TapeWriter(path)
    for i, payload in enumerate(payloads):
        writer.write(frame(i, payload))
    writer.close()

    records = list(read_tape(path))
    assert len(records) == 500
    assert [r["m"]["sequence"] for r in records] == list(range(500))
    assert [r["t"] for r in records] == list(range(500))


def test_counters_track_what_was_written(tmp_path):
    path = tmp_path / "tape.jsonl.zst"
    writer = TapeWriter(path)
    line = frame(1, b'{"sequence":1}')
    writer.write(line)
    writer.write(line)
    assert writer.messages_written == 2
    assert writer.bytes_written == 2 * len(line)
    writer.close()


def test_tape_is_readable_after_flush_without_close(tmp_path):
    """A machine that dies mid-session must still leave a readable tape."""
    path = tmp_path / "tape.jsonl.zst"
    writer = TapeWriter(path)
    writer.write(frame(1, b'{"sequence":1}'))
    writer.flush()
    assert len(list(read_tape(path))) == 1
    writer.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/python/test_record_tape.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'shadowfill.record.tape'`

- [ ] **Step 3: Write minimal implementation**

Add `"zstandard>=0.22"` to the `dependencies` list in `pyproject.toml`, then install with `pip install -e ".[dev]"`.

`python/shadowfill/record/tape.py`:
```python
"""Append-only raw tape: exactly the bytes the venue sent, plus a receive clock.

Nothing here interprets a message. The tape is the one artifact that cannot be
regenerated, so the code that writes it stays small enough to audit by reading.
"""

from __future__ import annotations

import io
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import zstandard as zstd


def frame(recv_ts_ns: int, payload: bytes) -> bytes:
    """Wrap a socket frame, splicing the payload rather than re-encoding it.

    Splicing is what makes byte-identity with the venue provable: the bytes in
    the file are the bytes that arrived, not a re-serialisation that happens to
    parse the same way.
    """
    return b'{"t":' + str(recv_ts_ns).encode() + b',"m":' + payload + b"}\n"


def frame_snapshot(recv_ts_ns: int, product_id: str, payload: bytes) -> bytes:
    """Wrap a REST snapshot body. Absence of `k` means the line came off the socket."""
    return (
        b'{"t":'
        + str(recv_ts_ns).encode()
        + b',"k":"snapshot","product_id":'
        + json.dumps(product_id).encode()
        + b',"m":'
        + payload
        + b"}\n"
    )


class TapeWriter:
    """One compressed append-only file. Rotation is the caller's business."""

    def __init__(self, path: str | Path, level: int = 3) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("wb")
        self._writer = zstd.ZstdCompressor(level=level).stream_writer(self._fh)
        self.messages_written = 0
        self.bytes_written = 0

    def write(self, line: bytes) -> None:
        self._writer.write(line)
        self.messages_written += 1
        self.bytes_written += len(line)

    def flush(self) -> None:
        """End the zstd frame and fsync, so the tape is readable to this point.

        Costs a little ratio at each boundary; buys a tape that survives the
        machine dying. For data that cannot be re-recorded that is the right way
        round.
        """
        self._writer.flush(zstd.FLUSH_FRAME)
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def close(self) -> None:
        self._writer.close()
        self._fh.close()


def read_tape(path: str | Path) -> Iterator[dict[str, Any]]:
    """Yield each tape record in arrival order."""
    dctx = zstd.ZstdDecompressor()
    with Path(path).open("rb") as fh:
        with dctx.stream_reader(fh) as reader:
            for line in io.TextIOWrapper(reader, encoding="utf-8"):
                if line.strip():
                    yield json.loads(line)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/python/test_record_tape.py -v`
Expected: PASS, 6 passed

- [ ] **Step 5: Commit**

```bash
git add python/shadowfill/record/tape.py tests/python/test_record_tape.py pyproject.toml
git commit -m "feat(record): append-only zstd tape with byte-identical framing"
```

---
## Task 3: The gap ledger

**Files:**
- Create: `python/shadowfill/record/ledger.py`
- Test: `tests/python/test_record_ledger.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `GapLedger(path)` with `.record(kind: str, *, ts_ns: int, product_id: str | None = None, **fields) -> None`, `.counts: dict[str, int]`, `.close()`; module constant `LEDGER_KINDS: frozenset[str]`.

**Context for the engineer:** The ledger is the audit trail that makes the tape usable. It is tiny compared to the tape, so every entry is flushed immediately — losing the record of a gap is worse than losing throughput, because a gap you did not write down is indistinguishable from data that was never there.

`record` rejects an unknown `kind`. A typo'd kind would sit silently in the file and never be counted by anything downstream.

- [ ] **Step 1: Write the failing test**

`tests/python/test_record_ledger.py`:
```python
import json

import pytest

from shadowfill.record.ledger import GapLedger


def read(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_records_an_exchange_gap_with_both_sequences(tmp_path):
    path = tmp_path / "gaps.jsonl"
    ledger = GapLedger(path)
    ledger.record("exchange_gap", ts_ns=5, product_id="BTC-USD", expected=101, got=105, missing=4)
    ledger.close()
    (entry,) = read(path)
    assert entry == {
        "kind": "exchange_gap", "ts_ns": 5, "product_id": "BTC-USD",
        "expected": 101, "got": 105, "missing": 4,
    }


def test_entries_are_durable_before_close(tmp_path):
    """A gap that is not on disk when the process dies never happened."""
    path = tmp_path / "gaps.jsonl"
    ledger = GapLedger(path)
    ledger.record("stall", ts_ns=1, duration_ns=2_000_000)
    assert len(read(path)) == 1
    ledger.close()


def test_counts_are_tallied_by_kind(tmp_path):
    ledger = GapLedger(tmp_path / "gaps.jsonl")
    ledger.record("exchange_gap", ts_ns=1, product_id="BTC-USD", expected=2, got=3, missing=1)
    ledger.record("stale", ts_ns=2, product_id="BTC-USD", expected=4, got=3)
    ledger.record("stale", ts_ns=3, product_id="ETH-USD", expected=9, got=8)
    ledger.close()
    assert ledger.counts == {"exchange_gap": 1, "stale": 2}


def test_product_id_is_omitted_when_it_does_not_apply(tmp_path):
    path = tmp_path / "gaps.jsonl"
    ledger = GapLedger(path)
    ledger.record("session_start", ts_ns=1, reason="startup")
    ledger.close()
    assert "product_id" not in read(path)[0]


def test_an_unknown_kind_is_rejected(tmp_path):
    """A typo'd kind would sit in the file and be counted by nothing."""
    ledger = GapLedger(tmp_path / "gaps.jsonl")
    with pytest.raises(ValueError, match="unknown ledger kind"):
        ledger.record("exchagne_gap", ts_ns=1)
    ledger.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/python/test_record_ledger.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'shadowfill.record.ledger'`

- [ ] **Step 3: Write minimal implementation**

`python/shadowfill/record/ledger.py`:
```python
"""Append-only record of everything that interrupted a clean recording."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

LEDGER_KINDS = frozenset(
    {
        "exchange_gap",   # sequence jumped: messages were lost in transit
        "stale",          # sequence at or below the last seen: duplicate or late
        "stall",          # the writer blocked; carries duration_ns
        "unparseable",    # frame is on the tape but could not be read
        "session_start",  # a recording session began; carries reason
        "session_end",    # a recording session ended; carries reason
        "snapshot",       # a REST snapshot was written into the tape
    }
)


class GapLedger:
    """One JSON object per line, flushed on write.

    Flushing every entry costs nothing at these volumes and means a gap is on
    disk before the next message is handled. A discontinuity that was not
    written down is indistinguishable from data that never existed.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")
        self.counts: Counter[str] = Counter()

    def record(
        self,
        kind: str,
        *,
        ts_ns: int,
        product_id: str | None = None,
        **fields: Any,
    ) -> None:
        if kind not in LEDGER_KINDS:
            raise ValueError(f"unknown ledger kind: {kind!r}")
        entry: dict[str, Any] = {"kind": kind, "ts_ns": ts_ns}
        if product_id is not None:
            entry["product_id"] = product_id
        entry.update(fields)
        self._fh.write(json.dumps(entry) + "\n")
        self._fh.flush()
        self.counts[kind] += 1

    def close(self) -> None:
        self._fh.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/python/test_record_ledger.py -v`
Expected: PASS, 5 passed

Note: `ledger.counts == {...}` compares a `Counter` to a `dict`, which is true when the contents match.

- [ ] **Step 5: Commit**

```bash
git add python/shadowfill/record/ledger.py tests/python/test_record_ledger.py
git commit -m "feat(record): append-only gap ledger"
```

---

## Task 4: Shared provenance, and the session manifest

**Files:**
- Create: `python/shadowfill/provenance.py`, `python/shadowfill/record/manifest.py`
- Modify: `python/shadowfill/ground_truth.py` (drop `_sha256`, `_git_sha`, `_environment`; import them)
- Test: `tests/python/test_record_manifest.py`

**Interfaces:**
- Consumes: `GapLedger.counts` from Task 3, `TapeWriter` counters from Task 2.
- Produces: `provenance.sha256_file(path) -> str`, `provenance.git_sha() -> str`, `provenance.environment() -> dict[str, str]`; `manifest.write_session_manifest(...) -> dict[str, Any]`.

**Context for the engineer:** `ground_truth.py` already has private `_sha256`, `_git_sha` and `_environment` helpers. Do not copy them. Move them into `provenance.py` and have `ground_truth.py` import them, so the recorder and the engine stamp artifacts the same way. Plan 1's full suite must still pass afterwards — that is the check that the extraction was clean.

`environment()` in `ground_truth.py` reports the C++ compiler. Keep that behaviour exactly; the recorder simply will not have a compiler to report and gets `"n/a (python engine)"`, which is accurate.

- [ ] **Step 1: Write the failing test**

`tests/python/test_record_manifest.py`:
```python
import hashlib
import json

from shadowfill.provenance import environment, git_sha, sha256_file
from shadowfill.record.manifest import write_session_manifest


def test_sha256_file_matches_hashlib(tmp_path):
    path = tmp_path / "blob.bin"
    path.write_bytes(b"shadowfill" * 1000)
    assert sha256_file(path) == hashlib.sha256(path.read_bytes()).hexdigest()


def test_environment_reports_what_produced_the_artifact():
    env = environment()
    assert env["python"].startswith("3.")
    for key in ("platform", "numpy", "pandas", "pyarrow"):
        assert env[key]


def test_git_sha_is_a_sha_or_unknown():
    sha = git_sha()
    assert sha == "unknown" or len(sha) == 40


def test_manifest_pins_the_tape_and_its_sequences(tmp_path):
    tape = tmp_path / "session-0001.jsonl.zst"
    tape.write_bytes(b"not really zstd, only hashed here")

    manifest = write_session_manifest(
        tape_path=tape,
        products=["BTC-USD", "ETH-USD"],
        session_id=1,
        started_ts_ns=1_000,
        ended_ts_ns=2_000,
        messages_written=17,
        bytes_written=512,
        first_seq={"BTC-USD": 100, "ETH-USD": 900},
        last_seq={"BTC-USD": 140, "ETH-USD": 930},
        ledger_counts={"exchange_gap": 1, "stale": 2},
        end_reason="rotation",
    )

    on_disk = json.loads((tmp_path / "session-0001.manifest.json").read_text())
    assert on_disk == manifest
    assert on_disk["tape_sha256"] == sha256_file(tape)
    assert len(on_disk["tape_sha256"]) == 64
    assert on_disk["first_seq"]["BTC-USD"] == 100
    assert on_disk["last_seq"]["ETH-USD"] == 930
    assert on_disk["ledger_counts"]["exchange_gap"] == 1
    assert on_disk["messages_written"] == 17
    assert on_disk["end_reason"] == "rotation"
    assert on_disk["git_sha"]
    assert on_disk["environment"]["python"].startswith("3.")


def test_manifest_never_contains_credentials(tmp_path, monkeypatch):
    """Nothing in the environment stamp may leak a secret."""
    monkeypatch.setenv("COINBASE_API_SECRET", "super-secret-value")
    tape = tmp_path / "session-0002.jsonl.zst"
    tape.write_bytes(b"x")
    manifest = write_session_manifest(
        tape_path=tape, products=["BTC-USD"], session_id=2,
        started_ts_ns=0, ended_ts_ns=1, messages_written=0, bytes_written=0,
        first_seq={}, last_seq={}, ledger_counts={}, end_reason="shutdown",
    )
    assert "super-secret-value" not in json.dumps(manifest)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/python/test_record_manifest.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'shadowfill.provenance'`

- [ ] **Step 3: Write minimal implementation**

`python/shadowfill/provenance.py`:
```python
"""Where an artifact came from: the code, the bytes, and the machine.

Shared by the ground-truth runner and the recorder so both stamp their output
the same way. A result that cannot be traced to an environment is not
reproducible, and that claim is load-bearing in this repository.
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
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def environment() -> dict[str, str]:
    """Only version strings and platform data. Never anything from the process env."""
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
```

In `python/shadowfill/ground_truth.py`: delete the `_sha256`, `_git_sha` and `_environment` function definitions and the now-unused `hashlib`, `platform`, `subprocess` imports, add `from .provenance import environment, git_sha, sha256_file`, and replace the three call sites in `run_ground_truth` with `git_sha()`, `sha256_file(message_path)` and `environment()`.

`python/shadowfill/record/manifest.py`:
```python
"""Summarise and hash a finished tape."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..provenance import environment, git_sha, sha256_file


def write_session_manifest(
    *,
    tape_path: str | Path,
    products: list[str],
    session_id: int,
    started_ts_ns: int,
    ended_ts_ns: int,
    messages_written: int,
    bytes_written: int,
    first_seq: dict[str, int],
    last_seq: dict[str, int],
    ledger_counts: dict[str, int],
    end_reason: str,
    queue_high_water: int = 0,
    free_bytes_at_close: int = 0,
) -> dict[str, Any]:
    """Write `<tape stem>.manifest.json` beside the tape and return it.

    The sequence ranges are what let a later reader check a tape against its
    neighbours without decompressing either.
    """
    tape_path = Path(tape_path)
    manifest: dict[str, Any] = {
        "session_id": session_id,
        "tape_path": tape_path.name,
        "tape_sha256": sha256_file(tape_path),
        "products": sorted(products),
        "started_ts_ns": started_ts_ns,
        "ended_ts_ns": ended_ts_ns,
        "end_reason": end_reason,
        "messages_written": messages_written,
        "bytes_written": bytes_written,
        "first_seq": first_seq,
        "last_seq": last_seq,
        "ledger_counts": dict(ledger_counts),
        "queue_high_water": queue_high_water,
        "free_bytes_at_close": free_bytes_at_close,
        "git_sha": git_sha(),
        "environment": environment(),
    }
    out = tape_path.with_suffix("").with_suffix(".manifest.json")
    out.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return manifest
```

Note on the output path: the tape is named `session-0001-....jsonl.zst`, so two `with_suffix` calls strip `.zst` then `.jsonl`. Verify the resulting filename in the test before moving on.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/python/test_record_manifest.py -v && pytest -q`
Expected: manifest tests PASS (5 passed); the full suite still passes at 71 passed, 2 skipped, proving the `ground_truth.py` extraction was clean.

- [ ] **Step 5: Commit**

```bash
git add python/shadowfill/provenance.py python/shadowfill/record/manifest.py python/shadowfill/ground_truth.py tests/python/test_record_manifest.py
git commit -m "feat(record): session manifests, over shared provenance helpers"
```

---
## Task 5: Transport seam

**Files:**
- Create: `python/shadowfill/record/transport.py`
- Test: `tests/python/test_record_transport.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Transport` (Protocol: `async connect()`, `async send(str)`, `async recv() -> bytes`, `async close()`), `FeedClosed` (exception), `FakeTransport(script: list[bytes | Exception])` with `.sent: list[str]`, `.connects: int`.

**Context for the engineer:** This is the seam that makes every later task testable without a network. `FakeTransport` replays a script; an `Exception` in the script is raised instead of returned, which is how tests inject disconnects. When the script runs out it raises `FeedClosed`, so a recorder loop terminates naturally instead of hanging a test.

Keep `Transport` a `Protocol`, not a base class. Nothing needs the inheritance, and a Protocol keeps `FakeTransport` honest — if it drifts from the real client's signature, mypy says so.

- [ ] **Step 1: Write the failing test**

`tests/python/test_record_transport.py`:
```python
import pytest

from shadowfill.record.transport import FakeTransport, FeedClosed


async def test_replays_its_script_in_order():
    t = FakeTransport([b"one", b"two"])
    await t.connect()
    assert await t.recv() == b"one"
    assert await t.recv() == b"two"


async def test_exhausted_script_closes_the_feed():
    t = FakeTransport([b"one"])
    await t.connect()
    await t.recv()
    with pytest.raises(FeedClosed):
        await t.recv()


async def test_an_exception_in_the_script_is_raised():
    """This is how tests inject a mid-stream disconnect."""
    t = FakeTransport([b"one", FeedClosed("dropped"), b"two"])
    await t.connect()
    assert await t.recv() == b"one"
    with pytest.raises(FeedClosed, match="dropped"):
        await t.recv()
    assert await t.recv() == b"two"


async def test_sends_and_connects_are_observable():
    t = FakeTransport([])
    await t.connect()
    await t.send('{"type":"subscribe"}')
    await t.close()
    assert t.connects == 1
    assert t.sent == ['{"type":"subscribe"}']
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/python/test_record_transport.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'shadowfill.record.transport'`

If instead every test is skipped with a warning about async support, the asyncio config in Step 3 has not been added yet — add it and rerun.

- [ ] **Step 3: Write minimal implementation**

Add to `pyproject.toml` under `[tool.pytest.ini_options]`:
```toml
asyncio_mode = "auto"
```
and add `"pytest-asyncio>=0.23"` to the `dev` extra. Install with `pip install -e ".[dev]"`.

`python/shadowfill/record/transport.py`:
```python
"""The seam between the recorder and a socket.

Everything downstream of here is tested against FakeTransport, which is why no
test in this plan needs a network.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class FeedClosed(Exception):
    """The feed is no longer delivering messages and must be reconnected."""


@runtime_checkable
class Transport(Protocol):
    async def connect(self) -> None: ...
    async def send(self, payload: str) -> None: ...
    async def recv(self) -> bytes: ...
    async def close(self) -> None: ...


class FakeTransport:
    """Replays a scripted sequence. An Exception entry is raised, not returned."""

    def __init__(self, script: list[bytes | Exception]) -> None:
        self._script = list(script)
        self._index = 0
        self.sent: list[str] = []
        self.connects = 0
        self.closed = False

    async def connect(self) -> None:
        self.connects += 1
        self.closed = False

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def recv(self) -> bytes:
        if self._index >= len(self._script):
            raise FeedClosed("script exhausted")
        item = self._script[self._index]
        self._index += 1
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/python/test_record_transport.py -v`
Expected: PASS, 4 passed

- [ ] **Step 5: Commit**

```bash
git add python/shadowfill/record/transport.py tests/python/test_record_transport.py pyproject.toml
git commit -m "feat(record): injectable transport seam with a scripted fake"
```

---

## Task 6: Coinbase authentication and subscription

**Files:**
- Create: `python/shadowfill/record/coinbase.py`
- Modify: `pyproject.toml` (add `websockets` to `dependencies`)
- Test: `tests/python/test_record_coinbase.py`

**Interfaces:**
- Consumes: `Transport`, `FeedClosed` from Task 5.
- Produces: `WS_URL`, `REST_URL`, `SIGNATURE_PATH`, `Credentials(key, secret, passphrase)`, `Credentials.from_env() -> Credentials`, `sign(secret_b64, timestamp, method, path, body="") -> str`, `subscribe_payload(creds, products, timestamp) -> str`, `CoinbaseTransport(url=WS_URL)`, `async fetch_snapshot(product_id, *, fetch) -> bytes`.

**Context for the engineer:** Verified against Coinbase's documentation on 2026-09-19. The `level3` channel requires authentication; the historical public Coinbase Pro `full` feed did not, so any older tutorial is wrong. The signed string is `{timestamp}GET/users/self/verify`, the secret is base64-**decoded** before use as the HMAC key, and the digest is base64-**encoded**. Getting the decode/encode order wrong produces a plausible-looking signature that is always rejected.

The test vector below is real — it was computed with `hmac.new(base64.b64decode(secret), msg, hashlib.sha256)`. If your implementation does not reproduce it exactly, your signing is wrong, and no amount of live debugging against the exchange will tell you why as clearly.

`Credentials.from_env` must fail loudly at startup. A recorder that starts, fails to authenticate and silently records nothing is the worst possible outcome.

- [ ] **Step 1: Write the failing test**

`tests/python/test_record_coinbase.py`:
```python
import base64
import json

import pytest

from shadowfill.record.coinbase import (
    SIGNATURE_PATH,
    Credentials,
    sign,
    subscribe_payload,
)

# Computed with hmac.new(base64.b64decode(secret), msg, sha256).digest(), base64'd.
SECRET = "c2hhZG93ZmlsbC10ZXN0LXNlY3JldA=="
TIMESTAMP = "1758253500"
EXPECTED_SIGNATURE = "RKZfA1tLfY3PCoKZLzVCfp3C2kBCgv3wAjitOqmXZYA="


def test_signature_matches_the_known_vector():
    assert sign(SECRET, TIMESTAMP, "GET", SIGNATURE_PATH) == EXPECTED_SIGNATURE


def test_signature_path_is_the_one_coinbase_documents():
    assert SIGNATURE_PATH == "/users/self/verify"


def test_secret_is_base64_decoded_before_use_as_the_key():
    """Signing with the undecoded secret is the classic failure; pin against it."""
    import hashlib
    import hmac

    wrong = base64.b64encode(
        hmac.new(SECRET.encode(), f"{TIMESTAMP}GET{SIGNATURE_PATH}".encode(), hashlib.sha256).digest()
    ).decode()
    assert sign(SECRET, TIMESTAMP, "GET", SIGNATURE_PATH) != wrong


def test_subscribe_payload_requests_level3_for_every_product():
    creds = Credentials(key="k", secret=SECRET, passphrase="p")
    payload = json.loads(subscribe_payload(creds, ["BTC-USD", "ETH-USD"], TIMESTAMP))  # noqa: E501
    assert payload["type"] == "subscribe"
    assert payload["channels"] == [{"name": "level3", "product_ids": ["BTC-USD", "ETH-USD"]}]
    assert payload["key"] == "k"
    assert payload["passphrase"] == "p"
    assert payload["timestamp"] == TIMESTAMP
    assert payload["signature"] == EXPECTED_SIGNATURE


def test_subscribe_payload_never_contains_the_raw_secret():
    creds = Credentials(key="k", secret=SECRET, passphrase="p")
    assert SECRET not in subscribe_payload(creds, ["BTC-USD"], TIMESTAMP)


def test_credentials_from_env_fails_loudly_when_unset(monkeypatch):
    """Starting up and silently recording nothing is the worst outcome."""
    for name in ("COINBASE_API_KEY", "COINBASE_API_SECRET", "COINBASE_API_PASSPHRASE"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="COINBASE_API_KEY"):
        Credentials.from_env()


def test_credentials_repr_does_not_leak_the_secret():
    """A secret in a traceback or a log line is a disclosure, not a bug report."""
    creds = Credentials(key="key-value", secret=SECRET, passphrase="pass-phrase-value")
    text = repr(creds)
    assert SECRET not in text
    assert "pass-phrase-value" not in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/python/test_record_coinbase.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'shadowfill.record.coinbase'`

- [ ] **Step 3: Write minimal implementation**

Add `"websockets>=12.0"` to `dependencies` in `pyproject.toml` and install.

`python/shadowfill/record/coinbase.py`:
```python
"""Coinbase Exchange level3 feed: credentials, subscription, transport.

Verified against https://docs.cdp.coinbase.com/exchange/websocket-feed/ on
2026-09-19. The level3 channel requires authentication; the historical public
Coinbase Pro `full` feed did not, so older examples are wrong.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Any

import websockets

WS_URL = "wss://ws-feed.exchange.coinbase.com"
REST_URL = "https://api.exchange.coinbase.com"
SIGNATURE_PATH = "/users/self/verify"


def sign(secret_b64: str, timestamp: str, method: str, path: str, body: str = "") -> str:
    """HMAC-SHA256 of `{timestamp}{method}{path}{body}`.

    The secret arrives base64-encoded and is decoded before use as the key; the
    digest is then base64-encoded. Swapping those two steps yields a
    plausible-looking signature that the exchange always rejects, with no useful
    error, which is why there is a pinned test vector for this function.
    """
    message = f"{timestamp}{method}{path}{body}".encode()
    digest = hmac.new(base64.b64decode(secret_b64), message, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


@dataclass(frozen=True)
class Credentials:
    key: str
    secret: str
    passphrase: str

    def __repr__(self) -> str:  # pragma: no cover - trivial, but load-bearing
        return f"Credentials(key={self.key[:4]}..., secret=<redacted>, passphrase=<redacted>)"

    @classmethod
    def from_env(cls) -> Credentials:
        """Read credentials, failing loudly if any is missing."""
        names = ("COINBASE_API_KEY", "COINBASE_API_SECRET", "COINBASE_API_PASSPHRASE")
        values = [os.environ.get(name) for name in names]
        missing = [name for name, value in zip(names, values, strict=True) if not value]
        if missing:
            raise RuntimeError(f"missing credentials in environment: {', '.join(missing)}")
        return cls(key=values[0] or "", secret=values[1] or "", passphrase=values[2] or "")


def subscribe_payload(creds: Credentials, products: list[str], timestamp: str) -> str:
    payload: dict[str, Any] = {
        "type": "subscribe",
        "channels": [{"name": "level3", "product_ids": list(products)}],
        "key": creds.key,
        "passphrase": creds.passphrase,
        "timestamp": timestamp,
        "signature": sign(creds.secret, timestamp, "GET", SIGNATURE_PATH),
    }
    return json.dumps(payload)


def now_timestamp() -> str:
    return str(int(time.time()))


class CoinbaseTransport:
    """Real WebSocket transport. Satisfies the Transport protocol from Task 5."""

    def __init__(self, url: str = WS_URL) -> None:
        self.url = url
        self._ws: Any = None

    async def connect(self) -> None:
        # ping_interval keeps a silent feed from looking alive; max_size None
        # because a level-3 snapshot frame can be large.
        self._ws = await websockets.connect(self.url, ping_interval=20, max_size=None)

    async def send(self, payload: str) -> None:
        await self._ws.send(payload)

    async def recv(self) -> bytes:
        message = await self._ws.recv()
        return message.encode() if isinstance(message, str) else message

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/python/test_record_coinbase.py -v`
Expected: PASS, 7 passed

- [ ] **Step 5: Commit**

```bash
git add python/shadowfill/record/coinbase.py tests/python/test_record_coinbase.py pyproject.toml
git commit -m "feat(record): coinbase level3 auth, subscription and transport"
```

---
## Task 7: The recorder, single session

**Files:**
- Create: `python/shadowfill/record/recorder.py`
- Test: `tests/python/test_record_recorder.py`

**Interfaces:**
- Consumes: `SequenceTracker`/`SeqStatus` (Task 1), `frame`/`TapeWriter`/`read_tape` (Task 2), `GapLedger` (Task 3), `write_session_manifest` (Task 4), `Transport`/`FeedClosed` (Task 5), `Credentials`/`subscribe_payload` (Task 6).
- Produces: `RecorderConfig`, `Recorder(config, transport_factory, credentials, *, clock=time.time_ns)` with `async run() -> None`, `.sessions_written: int`.

**Context for the engineer:** Two asyncio tasks share a bounded queue. The reader does nothing but timestamp and enqueue, because any work it does is time the socket is not being drained. The writer **appends to the tape before parsing** — tape integrity must never depend on the parser being right.

`await queue.put(...)` blocks when the queue is full. That is the design: if the writer falls behind, backpressure reaches the socket and Coinbase eventually disconnects us as a slow consumer, which is loud and recoverable. Dropping would be quiet, and a quietly incomplete tape corrupts every statistic derived from it without ever saying so. Stalls past the threshold are recorded so the condition is visible before it becomes a disconnect.

Messages without a `sequence` (subscription acknowledgements, errors, heartbeats) are recorded to the tape and skipped by the validator. They are not gaps.

This task handles exactly one session, ending when the feed closes. Reconnection is Task 8.

- [ ] **Step 1: Write the failing test**

`tests/python/test_record_recorder.py`:
```python
import json

from shadowfill.record.coinbase import Credentials
from shadowfill.record.recorder import Recorder, RecorderConfig
from shadowfill.record.tape import read_tape
from shadowfill.record.transport import FakeTransport, FeedClosed

SECRET = "c2hhZG93ZmlsbC10ZXN0LXNlY3JldA=="
CREDS = Credentials(key="k", secret=SECRET, passphrase="p")


def msg(sequence, product_id="BTC-USD", type_="open"):
    return json.dumps(
        {"type": type_, "product_id": product_id, "sequence": sequence, "order_id": "abc"}
    ).encode()


class StepClock:
    """Deterministic nanosecond clock; each call advances by `step`."""

    def __init__(self, start=0, step=1_000_000_000):
        self.now, self.step = start, step

    def __call__(self):
        self.now += self.step
        return self.now


def build(tmp_path, script, *, clock=None, rotate_seconds=3600):
    transport = FakeTransport(script)
    config = RecorderConfig(
        products=["BTC-USD"], out_dir=tmp_path, rotate_seconds=rotate_seconds
    )
    recorder = Recorder(
        config, lambda: transport, CREDS, clock=clock or StepClock()
    )
    return recorder, transport


def tapes(tmp_path):
    return sorted(tmp_path.rglob("*.jsonl.zst"))


def raw_tape_bytes(path):
    """Decompressed tape bytes. Used where a line is deliberately not JSON, so
    read_tape would raise; stream_reader also handles the multiple zstd frames
    that flush() creates."""
    import zstandard

    with path.open("rb") as fh, zstandard.ZstdDecompressor().stream_reader(fh) as reader:
        return reader.read()


def ledger_entries(tmp_path):
    entries = []
    for path in sorted(tmp_path.rglob("*.gaps.jsonl")):
        entries += [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    return entries


async def test_clean_session_writes_every_message_in_order(tmp_path):
    recorder, _ = build(tmp_path, [msg(1), msg(2), msg(3)])
    await recorder.run()

    (tape,) = tapes(tmp_path)
    records = list(read_tape(tape))
    assert [r["m"]["sequence"] for r in records] == [1, 2, 3]
    assert not [e for e in ledger_entries(tmp_path) if e["kind"] in {"exchange_gap", "stale"}]


async def test_it_subscribes_to_level3_on_connect(tmp_path):
    recorder, transport = build(tmp_path, [msg(1)])
    await recorder.run()
    (sent,) = transport.sent
    payload = json.loads(sent)
    assert payload["channels"] == [{"name": "level3", "product_ids": ["BTC-USD"]}]
    assert payload["signature"]


async def test_a_gap_is_recorded_and_recording_continues(tmp_path):
    recorder, _ = build(tmp_path, [msg(1), msg(5), msg(6)])
    await recorder.run()

    gaps = [e for e in ledger_entries(tmp_path) if e["kind"] == "exchange_gap"]
    assert len(gaps) == 1
    assert gaps[0]["expected"] == 2 and gaps[0]["got"] == 5 and gaps[0]["missing"] == 3
    assert [r["m"]["sequence"] for r in read_tape(tapes(tmp_path)[0])] == [1, 5, 6]


async def test_a_duplicate_is_recorded_as_stale(tmp_path):
    recorder, _ = build(tmp_path, [msg(1), msg(2), msg(2)])
    await recorder.run()
    stale = [e for e in ledger_entries(tmp_path) if e["kind"] == "stale"]
    assert len(stale) == 1 and stale[0]["got"] == 2


async def test_an_unparseable_frame_is_still_on_the_tape(tmp_path):
    """The parser failing must never cost us the bytes."""
    recorder, _ = build(tmp_path, [msg(1), b"{not json", msg(2)])
    await recorder.run()

    assert b"{not json" in raw_tape_bytes(tapes(tmp_path)[0])
    assert [e["kind"] for e in ledger_entries(tmp_path) if e["kind"] == "unparseable"]


async def test_messages_without_a_sequence_are_not_gaps(tmp_path):
    ack = json.dumps({"type": "subscriptions", "channels": []}).encode()
    recorder, _ = build(tmp_path, [ack, msg(1), msg(2)])
    await recorder.run()
    assert not [e for e in ledger_entries(tmp_path) if e["kind"] in {"exchange_gap", "stale"}]
    assert len(list(read_tape(tapes(tmp_path)[0]))) == 3


async def test_session_manifest_pins_the_sequence_range(tmp_path):
    recorder, _ = build(tmp_path, [msg(10), msg(11), msg(12)])
    await recorder.run()

    (manifest_path,) = sorted(tmp_path.rglob("*.manifest.json"))
    manifest = json.loads(manifest_path.read_text())
    assert manifest["first_seq"]["BTC-USD"] == 10
    assert manifest["last_seq"]["BTC-USD"] == 12
    assert manifest["messages_written"] == 3
    assert manifest["products"] == ["BTC-USD"]
    assert len(manifest["tape_sha256"]) == 64


async def test_rotation_starts_a_new_tape_and_manifest(tmp_path):
    # The clock advances one second per call, so a 2s rotation period splits
    # this stream into more than one tape.
    recorder, _ = build(
        tmp_path, [msg(i) for i in range(1, 9)], clock=StepClock(step=1_000_000_000),
        rotate_seconds=2,
    )
    await recorder.run()

    assert len(tapes(tmp_path)) > 1
    assert len(sorted(tmp_path.rglob("*.manifest.json"))) == len(tapes(tmp_path))

    seen = []
    for tape in tapes(tmp_path):
        seen += [r["m"]["sequence"] for r in read_tape(tape)]
    assert seen == list(range(1, 9)), "rotation must not lose or reorder a message"


async def test_every_delivered_message_appears_exactly_once(tmp_path):
    """The recorder's whole contract."""
    script = [msg(i) for i in range(1, 201)]
    recorder, _ = build(tmp_path, script)
    await recorder.run()

    seen = []
    for tape in tapes(tmp_path):
        seen += [r["m"]["sequence"] for r in read_tape(tape)]
    assert seen == list(range(1, 201))


async def test_queue_high_water_mark_is_reported(tmp_path):
    """Spec section 11: queue_maxsize cannot be tuned without observing the real peak."""
    recorder, _ = build(tmp_path, [msg(i) for i in range(1, 21)])
    await recorder.run()
    (manifest_path,) = sorted(tmp_path.rglob("*.manifest.json"))
    manifest = json.loads(manifest_path.read_text())
    assert "queue_high_water" in manifest
    assert 0 <= manifest["queue_high_water"] <= 20


async def test_low_disk_is_recorded_loudly(tmp_path, monkeypatch):
    """Silently filling the disk and dying at 3am is the classic recorder failure."""
    monkeypatch.setattr(
        "shadowfill.record.recorder.shutil.disk_usage", lambda _p: _Usage(100, 99, 1)
    )
    recorder, _ = build(tmp_path, [msg(1), msg(2)])
    await recorder.run()
    (manifest_path,) = sorted(tmp_path.rglob("*.manifest.json"))
    assert json.loads(manifest_path.read_text())["free_bytes_at_close"] == 1


class _Usage:
    def __init__(self, total, used, free):
        self.total, self.used, self.free = total, used, free


async def test_feed_closing_ends_the_session_cleanly(tmp_path):
    recorder, _ = build(tmp_path, [msg(1), FeedClosed("gone")])
    await recorder.run()
    kinds = [e["kind"] for e in ledger_entries(tmp_path)]
    assert "session_start" in kinds and "session_end" in kinds
    assert recorder.sessions_written == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/python/test_record_recorder.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'shadowfill.record.recorder'`

- [ ] **Step 3: Write minimal implementation**

`python/shadowfill/record/recorder.py`:
```python
"""Wiring: socket in, tape and ledger out.

The recorder interprets nothing beyond what it needs to detect a discontinuity.
Everything else is Plan 2b's problem, working from the tape.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .coinbase import Credentials, now_timestamp, subscribe_payload
from .ledger import GapLedger
from .manifest import write_session_manifest
from .sequence import SeqStatus, SequenceTracker
from .tape import TapeWriter, frame
from .transport import FeedClosed, Transport

_SENTINEL = object()


@dataclass
class RecorderConfig:
    products: list[str]
    out_dir: Path
    rotate_seconds: int = 3600
    queue_maxsize: int = 100_000
    stall_warn_seconds: float = 1.0
    disk_min_free_gb: float = 5.0


@dataclass
class _Session:
    """One tape, its ledger, and the counters its manifest will need."""

    session_id: int
    tape: TapeWriter
    ledger: GapLedger
    started_ts_ns: int
    first_seq: dict[str, int] = field(default_factory=dict)
    last_seq: dict[str, int] = field(default_factory=dict)
    queue_high_water: int = 0

    def note_sequence(self, product_id: str, sequence: int) -> None:
        self.first_seq.setdefault(product_id, sequence)
        self.last_seq[product_id] = sequence


class Recorder:
    def __init__(
        self,
        config: RecorderConfig,
        transport_factory: Callable[[], Transport],
        credentials: Credentials,
        *,
        clock: Callable[[], int] = time.time_ns,
    ) -> None:
        self.config = config
        self._transport_factory = transport_factory
        self._credentials = credentials
        self._clock = clock
        self._tracker = SequenceTracker()
        self._session: _Session | None = None
        self._next_session_id = 1
        self.sessions_written = 0

    # -- session lifecycle -------------------------------------------------

    def _open_session(self, reason: str) -> _Session:
        started = self._clock()
        stamp = datetime.fromtimestamp(started / 1e9, tz=UTC)
        day = stamp.strftime("%Y-%m-%d")
        stem = f"session-{self._next_session_id:04d}-{stamp.strftime('%Y%m%dT%H%M%SZ')}"
        directory = Path(self.config.out_dir) / day
        session = _Session(
            session_id=self._next_session_id,
            tape=TapeWriter(directory / f"{stem}.jsonl.zst"),
            ledger=GapLedger(directory / f"{stem}.gaps.jsonl"),
            started_ts_ns=started,
        )
        session.ledger.record("session_start", ts_ns=started, reason=reason)
        self._next_session_id += 1
        return session

    def _close_session(self, session: _Session, reason: str) -> None:
        ended = self._clock()
        # Checked once per session rather than per message: rotation is hourly,
        # which is frequent enough to notice a disk filling and cheap enough to
        # keep off the hot path.
        free = shutil.disk_usage(session.tape.path.parent).free
        if free < self.config.disk_min_free_gb * 1e9:
            logging.warning(
                "low disk: %.2f GB free at %s", free / 1e9, session.tape.path.parent
            )
        session.ledger.record("session_end", ts_ns=ended, reason=reason)
        session.tape.flush()
        session.tape.close()
        write_session_manifest(
            tape_path=session.tape.path,
            products=self.config.products,
            session_id=session.session_id,
            started_ts_ns=session.started_ts_ns,
            ended_ts_ns=ended,
            messages_written=session.tape.messages_written,
            bytes_written=session.tape.bytes_written,
            first_seq=session.first_seq,
            last_seq=session.last_seq,
            ledger_counts=dict(session.ledger.counts),
            end_reason=reason,
            queue_high_water=session.queue_high_water,
            free_bytes_at_close=free,
        )
        session.ledger.close()
        self.sessions_written += 1

    # -- tasks -------------------------------------------------------------

    async def _reader(self, transport: Transport, queue: asyncio.Queue[Any]) -> None:
        stall_ns = int(self.config.stall_warn_seconds * 1e9)
        while True:
            try:
                payload = await transport.recv()
            except FeedClosed:
                await queue.put(_SENTINEL)
                return
            recv_ts = self._clock()
            before = time.perf_counter_ns()
            # Blocks when the queue is full. Backpressure reaches the socket and
            # Coinbase disconnects us as a slow consumer -- loud and
            # recoverable. Dropping here would be silent and unrecoverable.
            await queue.put((recv_ts, payload))
            blocked = time.perf_counter_ns() - before
            if blocked > stall_ns and self._session is not None:
                self._session.ledger.record("stall", ts_ns=recv_ts, duration_ns=blocked)

    async def _writer(self, queue: asyncio.Queue[Any]) -> None:
        rotate_ns = self.config.rotate_seconds * 1_000_000_000
        while True:
            item = await queue.get()
            if item is _SENTINEL:
                return
            recv_ts, payload = item
            assert self._session is not None
            self._session.queue_high_water = max(self._session.queue_high_water, queue.qsize())
            if recv_ts - self._session.started_ts_ns >= rotate_ns:
                self._close_session(self._session, "rotation")
                self._session = self._open_session("rotation")
            # Tape first: integrity must not depend on the parser being right.
            self._session.tape.write(frame(recv_ts, payload))
            self._validate(recv_ts, payload, self._session)

    def _validate(self, recv_ts: int, payload: bytes, session: _Session) -> None:
        try:
            message = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            session.ledger.record(
                "unparseable", ts_ns=recv_ts, byte_offset=session.tape.bytes_written
            )
            return
        if not isinstance(message, dict):
            return
        product_id = message.get("product_id")
        sequence = message.get("sequence")
        # Subscription acks, errors and heartbeats carry no sequence. They are
        # recorded like anything else and are not discontinuities.
        if not isinstance(product_id, str) or not isinstance(sequence, int):
            return

        result = self._tracker.observe(product_id, sequence)
        session.note_sequence(product_id, sequence)
        if result.status is SeqStatus.GAP:
            session.ledger.record(
                "exchange_gap", ts_ns=recv_ts, product_id=product_id,
                expected=result.expected, got=result.got, missing=result.missing,
            )
        elif result.status is SeqStatus.STALE:
            session.ledger.record(
                "stale", ts_ns=recv_ts, product_id=product_id,
                expected=result.expected, got=result.got,
            )

    # -- entry point -------------------------------------------------------

    async def run(self) -> None:
        """Record one session, returning when the feed closes."""
        transport = self._transport_factory()
        await transport.connect()
        await transport.send(
            subscribe_payload(self._credentials, self.config.products, now_timestamp())
        )
        self._session = self._open_session("startup")
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=self.config.queue_maxsize)
        try:
            await asyncio.gather(self._reader(transport, queue), self._writer(queue))
        finally:
            if self._session is not None:
                self._close_session(self._session, "feed_closed")
                self._session = None
            await transport.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/python/test_record_recorder.py -v`
Expected: PASS, 12 passed

If `test_rotation_starts_a_new_tape_and_manifest` fails with only one tape, check that `StepClock` is advancing on the reader's `self._clock()` call — rotation is driven by the receive timestamp, not by wall time.

- [ ] **Step 5: Commit**

```bash
git add python/shadowfill/record/recorder.py tests/python/test_record_recorder.py
git commit -m "feat(record): single-session recorder with backpressure and rotation"
```

---
## Task 8: Reconnection, snapshots and session boundaries

**Files:**
- Modify: `python/shadowfill/record/recorder.py`, `python/shadowfill/record/coinbase.py`
- Test: `tests/python/test_record_recorder.py` (append)

**Interfaces:**
- Consumes: everything from Task 7.
- Produces: `coinbase.fetch_snapshot(product_id: str) -> bytes`; `RecorderConfig.max_sessions: int | None`, `.reconnect_base_delay: float`, `.reconnect_max_delay: float`; `Recorder(..., snapshot_fetcher=None, sleeper=asyncio.sleep)`.

**Context for the engineer:** A disconnect means messages were missed. The discontinuity is already recorded by the `session_end`/`session_start` pair, so the sequence tracker is **reset** on reconnect rather than left to report one enormous `exchange_gap` — recording both would double-count the same event, and a gap of unknown size is not more informative than a session boundary.

On reconnect, Coinbase's documented recovery is to fetch a REST `level=3` snapshot and discard queued messages at or below its sequence. The recorder does not do that discarding — it is not reconstructing a book. It writes the snapshot into the tape so Plan 2b has everything it needs to do the discarding itself, offline, where a mistake is recoverable.

Snapshot fetching uses `urllib.request` on a worker thread rather than adding an async HTTP dependency. The call happens once per product per reconnect, so it does not need to be fast, and stdlib is one less thing to pin.

Backoff is exponential with jitter, so a venue-side outage does not produce a synchronised reconnect storm from every recorder anyone is running.

- [ ] **Step 1: Write the failing test**

Append to `tests/python/test_record_recorder.py`:
```python
async def test_a_disconnect_starts_a_new_session_and_writes_a_snapshot(tmp_path):
    transport = FakeTransport([msg(1), FeedClosed("dropped"), msg(2)])
    config = RecorderConfig(
        products=["BTC-USD"], out_dir=tmp_path, max_sessions=2, reconnect_base_delay=0.0
    )
    snapshots = []

    async def fake_snapshot(product_id):
        snapshots.append(product_id)
        return json.dumps({"sequence": 500, "bids": [], "asks": []}).encode()

    recorder = Recorder(
        config, lambda: transport, CREDS, clock=StepClock(),
        snapshot_fetcher=fake_snapshot, sleeper=_no_sleep,
    )
    await recorder.run()

    assert recorder.sessions_written == 2
    assert snapshots == ["BTC-USD"], "one snapshot per product per reconnect"

    second = list(read_tape(tapes(tmp_path)[1]))
    assert second[0]["k"] == "snapshot"
    assert second[0]["product_id"] == "BTC-USD"
    assert second[0]["m"]["sequence"] == 500
    assert [e["kind"] for e in ledger_entries(tmp_path)].count("snapshot") == 1


async def test_reconnect_resubscribes(tmp_path):
    transport = FakeTransport([msg(1), FeedClosed("dropped"), msg(2)])
    config = RecorderConfig(
        products=["BTC-USD"], out_dir=tmp_path, max_sessions=2, reconnect_base_delay=0.0
    )
    recorder = Recorder(
        config, lambda: transport, CREDS, clock=StepClock(),
        snapshot_fetcher=_empty_snapshot, sleeper=_no_sleep,
    )
    await recorder.run()
    assert transport.connects == 2
    assert len(transport.sent) == 2


async def test_reconnect_does_not_report_one_enormous_gap(tmp_path):
    """The session boundary already records the discontinuity; do not double-count."""
    transport = FakeTransport([msg(1), FeedClosed("dropped"), msg(90_000)])
    config = RecorderConfig(
        products=["BTC-USD"], out_dir=tmp_path, max_sessions=2, reconnect_base_delay=0.0
    )
    recorder = Recorder(
        config, lambda: transport, CREDS, clock=StepClock(),
        snapshot_fetcher=_empty_snapshot, sleeper=_no_sleep,
    )
    await recorder.run()

    assert not [e for e in ledger_entries(tmp_path) if e["kind"] == "exchange_gap"]
    boundaries = [e["kind"] for e in ledger_entries(tmp_path)]
    assert boundaries.count("session_end") == 2 and boundaries.count("session_start") == 2


async def test_backoff_grows_between_failed_reconnects(tmp_path):
    delays = []

    async def record_sleep(seconds):
        delays.append(seconds)

    transport = FakeTransport(
        [msg(1), FeedClosed("a"), FeedClosed("b"), FeedClosed("c"), msg(2)]
    )
    config = RecorderConfig(
        products=["BTC-USD"], out_dir=tmp_path, max_sessions=4,
        reconnect_base_delay=1.0, reconnect_max_delay=8.0,
    )
    recorder = Recorder(
        config, lambda: transport, CREDS, clock=StepClock(),
        snapshot_fetcher=_empty_snapshot, sleeper=record_sleep,
    )
    await recorder.run()

    assert len(delays) >= 2
    assert all(0 < d <= 8.0 for d in delays), "jittered, and capped at the maximum"
    assert delays[1] >= delays[0], "backoff grows while reconnects keep failing"


async def _no_sleep(seconds):
    return None


async def _empty_snapshot(product_id):
    return json.dumps({"sequence": 1, "bids": [], "asks": []}).encode()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/python/test_record_recorder.py -v`
Expected: the four new tests FAIL with `TypeError: RecorderConfig.__init__() got an unexpected keyword argument 'max_sessions'`. The twelve from Task 7 still pass.

- [ ] **Step 3: Write minimal implementation**

Add to `python/shadowfill/record/coinbase.py`:
```python
import asyncio
import urllib.request


async def fetch_snapshot(product_id: str, *, timeout: float = 10.0) -> bytes:
    """Fetch a REST level-3 book snapshot.

    urllib on a worker thread rather than an async HTTP dependency: this runs
    once per product per reconnect, so it does not need to be fast, and it is
    one fewer package to pin.
    """

    def _get() -> bytes:
        url = f"{REST_URL}/products/{product_id}/book?level=3"
        request = urllib.request.Request(url, headers={"User-Agent": "shadowfill-recorder"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return bytes(response.read())

    return await asyncio.to_thread(_get)
```

In `python/shadowfill/record/recorder.py`, add to `RecorderConfig`:
```python
    max_sessions: int | None = None
    reconnect_base_delay: float = 1.0
    reconnect_max_delay: float = 60.0
```

Add to the `Recorder.__init__` signature and body:
```python
        snapshot_fetcher: Callable[[str], Awaitable[bytes]] | None = None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
```
```python
        self._snapshot_fetcher = snapshot_fetcher or fetch_snapshot
        self._sleeper = sleeper
```
with `from collections.abc import Awaitable, Callable`, `import random`, `from .coinbase import fetch_snapshot` and `from .tape import TapeWriter, frame, frame_snapshot`.

Rename the existing `run` to `_run_session`, returning the reason the session ended, and give it the snapshot step:

```python
    async def _run_session(self, *, is_reconnect: bool) -> None:
        transport = self._transport_factory()
        await transport.connect()
        await transport.send(
            subscribe_payload(self._credentials, self.config.products, now_timestamp())
        )
        self._session = self._open_session("reconnect" if is_reconnect else "startup")
        try:
            if is_reconnect:
                await self._write_snapshots()
            queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=self.config.queue_maxsize)
            await asyncio.gather(self._reader(transport, queue), self._writer(queue))
        finally:
            if self._session is not None:
                self._close_session(self._session, "feed_closed")
                self._session = None
            await transport.close()

    async def _write_snapshots(self) -> None:
        """Record a REST snapshot per product into the tape.

        The recorder does not apply the snapshot or discard superseded
        messages -- it is not reconstructing a book. It stores what Plan 2b
        needs to do that offline, where a mistake costs a re-run rather than
        the data.
        """
        assert self._session is not None
        for product_id in self.config.products:
            payload = await self._snapshot_fetcher(product_id)
            ts = self._clock()
            self._session.tape.write(frame_snapshot(ts, product_id, payload))
            self._session.ledger.record("snapshot", ts_ns=ts, product_id=product_id)

    async def run(self) -> None:
        """Record until max_sessions is reached, reconnecting with backoff."""
        attempt = 0
        is_reconnect = False
        while True:
            await self._run_session(is_reconnect=is_reconnect)
            # A disconnect means messages were missed. The session boundary
            # already records that, so the tracker is reset rather than left to
            # report one enormous gap of unknown size -- recording both would
            # double-count the same event.
            self._tracker = SequenceTracker()
            is_reconnect = True

            if self.config.max_sessions is not None and self.sessions_written >= self.config.max_sessions:
                return

            attempt += 1
            delay = min(
                self.config.reconnect_max_delay,
                self.config.reconnect_base_delay * (2 ** (attempt - 1)),
            )
            if delay > 0:
                # Jitter so a venue-side outage does not produce a synchronised
                # reconnect storm from every recorder running against it.
                await self._sleeper(delay * (0.5 + random.random() / 2))
```

Note: `attempt` deliberately never resets in this task. A recorder that reconnects successfully and then drops again within seconds is in trouble, and backing off further is the right response; resetting on every successful connect would produce a tight reconnect loop against a flapping feed.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/python/test_record_recorder.py -v`
Expected: PASS, 16 passed

- [ ] **Step 5: Commit**

```bash
git add python/shadowfill/record/recorder.py python/shadowfill/record/coinbase.py tests/python/test_record_recorder.py
git commit -m "feat(record): reconnect with backoff, snapshots and session boundaries"
```

---

## Task 9: CLI, deployment and documentation

**Files:**
- Create: `python/shadowfill/record/__main__.py`, `deploy/shadowfill-recorder.service`
- Modify: `Makefile`, `README.md`
- Test: `tests/python/test_record_cli.py`

**Interfaces:**
- Consumes: `RecorderConfig`, `Recorder`, `Credentials.from_env`, `CoinbaseTransport`.
- Produces: `build_config(argv: list[str] | None = None) -> RecorderConfig`, `main(argv: list[str] | None = None) -> int`.

**Context for the engineer:** Argument parsing is split from the async entry point so it can be tested without a network or an event loop. The defaults are the three products from the spec, chosen to span relative-tick regimes.

The systemd unit uses `Restart=always` and reads credentials from an environment file that is **not** in the repository. Add `deploy/*.env` to `.gitignore` in this task.

- [ ] **Step 1: Write the failing test**

`tests/python/test_record_cli.py`:
```python
from pathlib import Path

from shadowfill.record.__main__ import build_config


def test_defaults_span_the_tick_regimes():
    config = build_config([])
    assert config.products == ["BTC-USD", "ETH-USD", "ADA-USD"]
    assert config.out_dir == Path("data/raw/coinbase")
    assert config.rotate_seconds == 3600
    assert config.max_sessions is None


def test_products_can_be_overridden():
    config = build_config(["--products", "BTC-USD,SOL-USD"])
    assert config.products == ["BTC-USD", "SOL-USD"]


def test_max_sessions_is_available_for_smoke_runs():
    assert build_config(["--max-sessions", "1"]).max_sessions == 1


def test_out_dir_and_rotation_are_configurable(tmp_path):
    config = build_config(["--out-dir", str(tmp_path), "--rotate-seconds", "60"])
    assert config.out_dir == tmp_path
    assert config.rotate_seconds == 60
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/python/test_record_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'shadowfill.record.__main__'`

- [ ] **Step 3: Write minimal implementation**

`python/shadowfill/record/__main__.py`:
```python
"""Command line entry point: `python -m shadowfill.record`."""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from .coinbase import CoinbaseTransport, Credentials
from .recorder import Recorder, RecorderConfig

DEFAULT_PRODUCTS = ["BTC-USD", "ETH-USD", "ADA-USD"]


def build_config(argv: list[str] | None = None) -> RecorderConfig:
    """Parse arguments. Separate from `main` so it is testable without a loop.

    `None` means "read sys.argv", which is what argparse does by default and
    what `main` relies on.
    """
    parser = argparse.ArgumentParser(description="Record the Coinbase level3 feed")
    parser.add_argument(
        "--products",
        default=",".join(DEFAULT_PRODUCTS),
        help="comma-separated product ids; the default spans relative-tick regimes",
    )
    parser.add_argument("--out-dir", default="data/raw/coinbase", type=Path)
    parser.add_argument("--rotate-seconds", type=int, default=3600)
    parser.add_argument("--queue-maxsize", type=int, default=100_000)
    parser.add_argument(
        "--max-sessions", type=int, default=None, help="stop after N sessions; for smoke runs"
    )
    args = parser.parse_args(argv)
    return RecorderConfig(
        products=[p.strip() for p in args.products.split(",") if p.strip()],
        out_dir=args.out_dir,
        rotate_seconds=args.rotate_seconds,
        queue_maxsize=args.queue_maxsize,
        max_sessions=args.max_sessions,
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    config = build_config(argv)
    # Fails here, loudly, rather than starting up and recording nothing.
    credentials = Credentials.from_env()
    recorder = Recorder(config, CoinbaseTransport, credentials)
    logging.info("recording %s into %s", ",".join(config.products), config.out_dir)
    asyncio.run(recorder.run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

`deploy/shadowfill-recorder.service`:
```ini
[Unit]
Description=ShadowFill Coinbase L3 recorder
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=shadowfill
WorkingDirectory=/opt/shadowfill
# Credentials live here, outside the repository. chmod 600, never committed.
EnvironmentFile=/etc/shadowfill/coinbase.env
ExecStart=/opt/shadowfill/.venv/bin/python -m shadowfill.record --out-dir /var/lib/shadowfill/raw
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Add to `.gitignore`:
```
deploy/*.env
```

Add to `Makefile`:
```make
record:
	python -m shadowfill.record
```
and add `record` to the `.PHONY` line.

Add a section to `README.md` after "Using real data":

```markdown
## Recording Coinbase L3

    export COINBASE_API_KEY=... COINBASE_API_SECRET=... COINBASE_API_PASSPHRASE=...
    python -m shadowfill.record --out-dir data/raw/coinbase

The `level3` channel requires authentication; read-only market-data keys are
enough. Each session writes three files: a zstd tape of the raw messages exactly
as the venue sent them, a ledger of every discontinuity, and a manifest pinning
the tape's SHA-256, sequence ranges and the code that recorded it.

The recorder never repairs a gap and never deletes a tape. Under backpressure it
blocks rather than dropping, so a stalled writer eventually shows up as a
disconnect rather than as silently missing data.

For continuous recording see `deploy/shadowfill-recorder.service`.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/python/test_record_cli.py -v && make lint`
Expected: PASS, 4 passed; lint clean.

- [ ] **Step 5: Commit**

```bash
git add python/shadowfill/record/__main__.py deploy/shadowfill-recorder.service tests/python/test_record_cli.py Makefile README.md .gitignore
git commit -m "feat(record): cli, systemd unit and recording docs"
```

---

## Task 10: Live smoke test

**Files:**
- Create: `tests/python/test_record_live.py`
- Modify: `pyproject.toml` (register the `needs_network` marker)

**Interfaces:**
- Consumes: everything.
- Produces: nothing importable.

**Context for the engineer:** This is the only test that touches the network, and CI never runs it. It exists because every other test in this plan runs against `FakeTransport`, which proves the recorder's logic and proves nothing about whether the real signature is accepted or the real message shape matches what the validator expects. Those are exactly the failures that would otherwise be discovered after a night of recording nothing.

It skips rather than fails when credentials are absent, because a contributor without a Coinbase account should still be able to run the suite.

- [ ] **Step 1: Write the failing test**

Register the marker in `pyproject.toml`:
```toml
markers = [
    "needs_lobster: requires the manually downloaded LOBSTER sample",
    "needs_network: talks to a live venue; never run in CI",
]
```

`tests/python/test_record_live.py`:
```python
"""Live smoke test. Never run in CI; see the needs_network marker."""

import asyncio
import json
import os

import pytest

from shadowfill.record.coinbase import CoinbaseTransport, Credentials
from shadowfill.record.recorder import Recorder, RecorderConfig
from shadowfill.record.tape import read_tape

pytestmark = pytest.mark.needs_network


@pytest.fixture
def credentials():
    try:
        return Credentials.from_env()
    except RuntimeError as exc:
        pytest.skip(str(exc))


def test_records_a_real_session(tmp_path, credentials):
    """Proves the signature is accepted and the real message shape validates."""
    config = RecorderConfig(
        products=["BTC-USD"], out_dir=tmp_path, rotate_seconds=5, max_sessions=1
    )
    recorder = Recorder(config, CoinbaseTransport, credentials)

    async def run_briefly():
        await asyncio.wait_for(recorder.run(), timeout=20)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(run_briefly())

    tapes = sorted(tmp_path.rglob("*.jsonl.zst"))
    assert tapes, "no tape written: authentication or subscription failed"

    records = [r for tape in tapes for r in read_tape(tape)]
    assert len(records) > 10, "feed produced almost nothing; check the subscription"

    typed = [r["m"] for r in records if isinstance(r["m"], dict) and "sequence" in r["m"]]
    assert typed, "no message carried a sequence number"
    assert {m["type"] for m in typed} <= {"open", "change", "match", "done", "noop"}

    ledgers = sorted(tmp_path.rglob("*.gaps.jsonl"))
    entries = [json.loads(x) for p in ledgers for x in p.read_text().splitlines() if x.strip()]
    assert not [e for e in entries if e["kind"] == "unparseable"], (
        "the live feed produced a frame the validator could not read"
    )


def test_credentials_are_never_written_to_disk(tmp_path, credentials):
    """A secret reaching a tape or manifest would be a disclosure, not a bug."""
    config = RecorderConfig(
        products=["BTC-USD"], out_dir=tmp_path, rotate_seconds=5, max_sessions=1
    )
    recorder = Recorder(config, CoinbaseTransport, credentials)
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(asyncio.wait_for(recorder.run(), timeout=15))

    secret = os.environ["COINBASE_API_SECRET"]
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -m needs_network -v`
Expected: with no credentials set, both tests SKIP with the missing-credentials message. That skip is the correct first observation; it proves the marker and the guard work.

- [ ] **Step 3: Write minimal implementation**

No implementation. This task is the acceptance test for Tasks 1–9.

Confirm CI does not pick it up: `.github/workflows/ci.yml` runs `pytest -v -m "not needs_lobster"`. Change that to:
```yaml
      - run: pytest -v -m "not needs_lobster and not needs_network"
```

- [ ] **Step 4: Run test to verify it passes**

With credentials exported, run: `pytest -m needs_network -v`
Expected: PASS, 2 passed. Then confirm the default suite excludes it: `pytest -q` should report the same count as before this task, with the live tests deselected.

**Do not mark this step complete on a skip.** A skipped smoke test proves nothing about the live feed, which is the entire reason this task exists. If credentials are not yet available, stop here and say so rather than ticking the box.

- [ ] **Step 5: Commit**

```bash
git add tests/python/test_record_live.py pyproject.toml .github/workflows/ci.yml
git commit -m "test(record): live smoke test behind the needs_network marker"
```

---

## Definition of done for Plan 2a

- [ ] `make test`, `make lint` pass on a clean clone with no network and no credentials
- [ ] `pytest -m needs_network` passes on a machine with Coinbase credentials present
- [ ] Every delivered message appears exactly once, in order, across rotation and reconnection — pinned by `test_every_delivered_message_appears_exactly_once`
- [ ] A tape written by a killed process is still readable to its last flush
- [ ] Credentials appear in no tape, ledger, manifest, log line or committed file
- [ ] A session's manifest pins the tape SHA-256, per-product sequence range, ledger counts, git SHA and environment
- [ ] The recorder has run continuously for 24 hours on the target host, and the resulting ledgers have been read rather than assumed empty
- [ ] Observed bytes/day and messages/day are recorded in `docs/` so the disk can be sized from measurement rather than from a guess

## Explicitly out of scope

Canonical event conversion, Parquet output, Databento, book reconstruction, and any analysis. These are Plan 2b and later. The recorder does not know what an order book is, and that is the point: it cannot corrupt what it does not interpret.

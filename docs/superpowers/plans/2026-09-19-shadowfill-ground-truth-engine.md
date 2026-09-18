# ShadowFill Ground-Truth Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a validated engine that, given a market-by-order (MBO) event stream, computes the exact fill outcome of hypothetical never-cancel limit orders ("shadow orders") by queue arithmetic, and emits a reproducible ground-truth outcome table.

**Architecture:** A canonical event schema sits between data adapters (LOBSTER first) and two independent replay implementations: a slow, obviously-correct Python reference and a fast C++20 engine. The two are held to bit-identical outputs by an equivalence test, so the Python version is the oracle and the C++ version is the workhorse. Shadow orders are never inserted into the book — they are tracked as a `queue-ahead` counter that only ever decreases, which is what makes the fill time computable rather than modelled.

**Tech Stack:** Python 3.11, NumPy, pandas, pyarrow, pytest, Hypothesis; C++20, CMake ≥ 3.24, pybind11, Catch2 v3, scikit-build-core; ruff, mypy, pre-commit, GitHub Actions.

---

## Why this is Plan 1

Every downstream claim in the research (estimator bias, adverse-selection offset, L2 penalty, policy ranking inversions) is a comparison **against** the number this engine produces. If the queue arithmetic is wrong, every result is wrong and no statistical sophistication rescues it. Therefore this plan produces exactly one deliverable and tests it to death:

> Given `(MBO event stream, placement schedule)` → `outcome table` with, per shadow order: status, first-fill timestamp, full-fill timestamp, filled quantity, queue-ahead at insert, queue-ahead at end.

No estimators, no plots, no crypto recorder. Those are Plans 2–4.

---

## Domain primer (read before Task 1)

**MBO / L3 data** gives one row per order lifecycle event, each carrying an `order_id`. This is the whole reason the project is possible: with order IDs you know *which* order was cancelled, so you know whether it was ahead of or behind your hypothetical order. Aggregated L2 data forces you to guess (cancel-from-front / back / uniform), and that guess is exactly the error source Plan 3 will measure.

**The shadow-order trick.** Place a hypothetical passive order at price `p`, side `s`, at time `t`. Under strict price–time priority, *every order resting at `p` at time `t` is ahead of it*, and *every order arriving afterwards is behind it*. So maintain a single integer, `ahead`:

- an order ahead is cancelled or deleted → `ahead -= removed_size`
- a trade executes at `p` → it eats the front of the queue → `consumed = min(size, ahead)`, `ahead -= consumed`; any residual would have hit the shadow → that's a fill
- an order is added at `p` → ignore, it is behind

`ahead` is monotone non-increasing. When it hits zero and a trade arrives, the shadow fills. This is arithmetic, not a model. The only assumption is that the shadow order itself does not change other participants' behaviour — reasonable for small sizes, and stress-tested in Plan 4.

**Critical correctness details** (each becomes a test):

1. **Hidden executions do not consume the visible queue.** LOBSTER event type 5 is the execution of a hidden order. It never sat in the visible book, so it must not decrement `ahead`. Getting this wrong inflates fill rates.
2. **Level-band truncation.** LOBSTER files are requested at a fixed depth (level 5, 10, 50). Events at prices outside the recorded band are absent. Shadow orders are therefore placed at top-of-book only in this plan, and any shadow whose price level exits the band is flagged.
3. **Unknown order IDs.** Cancels and executions can reference orders that were added before the recording window began. They must still decrement `ahead` (they are genuinely ahead), and must be counted in diagnostics.
4. **Time parsing.** LOBSTER timestamps are seconds after midnight with up to nanosecond decimals. Parsing them as `float64` silently loses nanoseconds. Parse as string, split on the decimal point, build `int64` nanoseconds.
5. **Match before apply.** When an event arrives, shadow accounting must run *before* the book applies the event, because determining whether a cancelled order was ahead requires looking up its arrival sequence in the book before it is deleted.

**Data access note.** The LOBSTER sample files are downloaded manually (see Task 3) and are **not committed**. CI runs against a committed synthetic fixture, so the entire test suite passes on a clean clone with no data and no network.

---

## File Structure

```
shadowfill/
├── CMakeLists.txt                          # C++ build: core lib, Catch2 tests, pybind module
├── pyproject.toml                          # scikit-build-core, deps, ruff/mypy config
├── Makefile                                # build, test, bench, reproduce
├── .pre-commit-config.yaml
├── .github/workflows/ci.yml
├── src/shadowfill_core/
│   ├── include/shadowfill/event.hpp        # Event POD, EventType, Side enums
│   ├── include/shadowfill/book.hpp         # OrderBook declaration
│   ├── include/shadowfill/shadow.hpp       # Placement, Outcome, ShadowTracker declarations
│   ├── book.cpp                            # OrderBook implementation
│   └── shadow.cpp                          # ShadowTracker implementation
├── src/shadowfill_bindings/
│   └── module.cpp                          # pybind11 `shadowfill._core`
├── python/shadowfill/
│   ├── __init__.py
│   ├── events.py                           # canonical schema, enums, EVENT_DTYPE
│   ├── lobster.py                          # LOBSTER message/orderbook file adapters
│   ├── replay.py                           # RefBook + RefShadowTracker (the oracle)
│   ├── placements.py                        # top-of-book grid placement generator
│   ├── ground_truth.py                     # run_ground_truth() + Parquet/manifest writer
│   └── synthetic.py                        # deterministic synthetic MBO fixture generator
├── tests/cpp/
│   ├── test_book.cpp
│   └── test_shadow.cpp
├── tests/python/
│   ├── conftest.py
│   ├── test_events.py
│   ├── test_lobster.py
│   ├── test_refbook.py
│   ├── test_refbook_vs_lobster_orderbook.py
│   ├── test_shadow_tracker.py
│   ├── test_invariants.py
│   └── test_cpp_equivalence.py
├── tests/fixtures/
│   └── synthetic_mbo_v1.csv                # committed, deterministic
├── benchmarks/
│   └── bench_replay.py
├── scripts/
│   └── fetch_lobster_sample.sh             # manual data download helper
└── docs/superpowers/plans/                 # this file
```

**Responsibility boundaries:** `events.py` owns the schema and nothing else. `lobster.py` only translates vendor format → canonical events. `replay.py` owns book state and shadow accounting and knows nothing about files. `ground_truth.py` is the only module that touches disk for outputs. The C++ mirrors `replay.py` exactly; if the two ever disagree, the Python one is right by definition and the C++ is the bug.

---

## Task 1: Repository scaffolding and CI

**Files:**
- Create: `pyproject.toml`, `CMakeLists.txt`, `Makefile`, `.pre-commit-config.yaml`, `.github/workflows/ci.yml`, `python/shadowfill/__init__.py`, `tests/python/test_smoke.py`

- [x] **Step 1: Write the failing test**

`tests/python/test_smoke.py`:
```python
def test_package_imports_and_exposes_version():
    import shadowfill

    assert isinstance(shadowfill.__version__, str)
    assert shadowfill.__version__.count(".") >= 2
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/python/test_smoke.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'shadowfill'`

- [x] **Step 3: Write minimal implementation**

`python/shadowfill/__init__.py`:
```python
"""ShadowFill: counterfactual fill estimation from market-by-order data."""

__version__ = "0.1.0"
```

`pyproject.toml`:
```toml
[build-system]
requires = ["scikit-build-core>=0.9", "pybind11>=2.12"]
build-backend = "scikit_build_core.build"

[project]
name = "shadowfill"
version = "0.1.0"
description = "Counterfactual fill estimation from market-by-order data"
requires-python = ">=3.11"
dependencies = [
    "numpy>=1.26",
    "pandas>=2.2",
    "pyarrow>=16.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "hypothesis>=6.100",
    "ruff>=0.5",
    "mypy>=1.10",
    "pre-commit>=3.7",
]

[tool.scikit-build]
wheel.packages = ["python/shadowfill"]
cmake.version = ">=3.24"
build-dir = "build/{wheel_tag}"

[tool.pytest.ini_options]
testpaths = ["tests/python"]
addopts = "-ra --strict-markers"
markers = ["needs_lobster: requires the manually downloaded LOBSTER sample"]

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "N", "UP", "B", "SIM", "RUF"]

[tool.mypy]
python_version = "3.11"
strict = true
files = ["python/shadowfill"]
```

`CMakeLists.txt`:
```cmake
cmake_minimum_required(VERSION 3.24)
project(shadowfill_core LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

add_library(shadowfill_core
    src/shadowfill_core/book.cpp
    src/shadowfill_core/shadow.cpp
)
target_include_directories(shadowfill_core PUBLIC src/shadowfill_core/include)
target_compile_options(shadowfill_core PRIVATE -O3 -Wall -Wextra -Wpedantic -Werror)

option(SHADOWFILL_BUILD_TESTS "Build C++ tests" OFF)
if(SHADOWFILL_BUILD_TESTS)
    include(FetchContent)
    FetchContent_Declare(Catch2
        GIT_REPOSITORY https://github.com/catchorg/Catch2.git
        GIT_TAG v3.6.0)
    FetchContent_MakeAvailable(Catch2)
    add_executable(cpp_tests tests/cpp/test_book.cpp tests/cpp/test_shadow.cpp)
    target_link_libraries(cpp_tests PRIVATE shadowfill_core Catch2::Catch2WithMain)
    enable_testing()
    add_test(NAME cpp_tests COMMAND cpp_tests)
endif()

find_package(pybind11 CONFIG QUIET)
if(pybind11_FOUND)
    pybind11_add_module(_core src/shadowfill_bindings/module.cpp)
    target_link_libraries(_core PRIVATE shadowfill_core)
    install(TARGETS _core DESTINATION shadowfill)
endif()
```

`Makefile`:
```make
.PHONY: install test test-cpp lint bench reproduce clean

install:
	pip install -e ".[dev]"

test:
	pytest -v

test-cpp:
	cmake -B build/cpp -DSHADOWFILL_BUILD_TESTS=ON
	cmake --build build/cpp -j
	ctest --test-dir build/cpp --output-on-failure

lint:
	ruff check python tests
	ruff format --check python tests
	mypy

bench:
	python benchmarks/bench_replay.py

reproduce: test test-cpp bench
	python -m shadowfill.ground_truth --config configs/ground_truth_synthetic.yaml

clean:
	rm -rf build dist *.egg-info .pytest_cache .mypy_cache
```

`.pre-commit-config.yaml`:
```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.5.7
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v4.6.0
    hooks:
      - id: trailing-whitespace
      - id: end-of-file-fixer
      - id: check-yaml
```

`.github/workflows/ci.yml`:
```yaml
name: ci
on: [push, pull_request]

jobs:
  python:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -e ".[dev]"
      - run: make lint
      - run: pytest -v -m "not needs_lobster"

  cpp:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: make test-cpp
```

- [x] **Step 4: Run test to verify it passes**

Run: `pip install -e ".[dev]" && pytest tests/python/test_smoke.py -v`
Expected: PASS, 1 passed

- [x] **Step 5: Commit**

```bash
git add pyproject.toml CMakeLists.txt Makefile .pre-commit-config.yaml .github python/shadowfill/__init__.py tests/python/test_smoke.py
git commit -m "chore: scaffold shadowfill package, cmake build, and CI"
```

---

## Task 2: Canonical event schema

**Files:**
- Create: `python/shadowfill/events.py`
- Test: `tests/python/test_events.py`

- [ ] **Step 1: Write the failing test**

`tests/python/test_events.py`:
```python
import numpy as np

from shadowfill.events import EVENT_DTYPE, EventType, Side, empty_events


def test_event_type_values_match_lobster_codes():
    assert EventType.ADD == 1
    assert EventType.CANCEL_PARTIAL == 2
    assert EventType.DELETE == 3
    assert EventType.EXECUTE == 4
    assert EventType.EXECUTE_HIDDEN == 5
    assert EventType.CROSS == 6
    assert EventType.HALT == 7


def test_side_encodes_resting_side():
    assert Side.BID == 1
    assert Side.ASK == -1


def test_event_dtype_field_order_and_widths():
    assert EVENT_DTYPE.names == (
        "ts_ns",
        "seq",
        "order_id",
        "price",
        "size",
        "type",
        "side",
    )
    assert EVENT_DTYPE["ts_ns"] == np.dtype("<i8")
    assert EVENT_DTYPE["price"] == np.dtype("<i8")
    assert EVENT_DTYPE["size"] == np.dtype("<i8")


def test_empty_events_allocates_correct_shape():
    ev = empty_events(3)
    assert ev.shape == (3,)
    assert ev.dtype == EVENT_DTYPE
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/python/test_events.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'shadowfill.events'`

- [ ] **Step 3: Write minimal implementation**

`python/shadowfill/events.py`:
```python
"""Canonical MBO event schema.

Every data adapter converts vendor formats into this schema. Nothing downstream
knows about LOBSTER, Databento, or exchange websocket payloads.

Units:
    ts_ns    nanoseconds since local midnight of the session
    price    integer price ticks (LOBSTER convention: dollars * 10_000)
    size     integer shares/contracts/base units
    seq      monotone arrival sequence, defines price-time priority
"""

from __future__ import annotations

from enum import IntEnum

import numpy as np


class EventType(IntEnum):
    """Event codes, aligned with LOBSTER message types."""

    ADD = 1
    CANCEL_PARTIAL = 2
    DELETE = 3
    EXECUTE = 4
    EXECUTE_HIDDEN = 5
    CROSS = 6
    HALT = 7


class Side(IntEnum):
    """Side of the *resting* order the event refers to."""

    BID = 1
    ASK = -1


EVENT_DTYPE = np.dtype(
    [
        ("ts_ns", "<i8"),
        ("seq", "<u8"),
        ("order_id", "<u8"),
        ("price", "<i8"),
        ("size", "<i8"),
        ("type", "u1"),
        ("side", "i1"),
    ]
)

CONSUMES_VISIBLE_QUEUE = frozenset(
    {EventType.CANCEL_PARTIAL, EventType.DELETE, EventType.EXECUTE}
)


def empty_events(n: int) -> np.ndarray:
    """Allocate an uninitialised canonical event array of length ``n``."""
    return np.zeros(n, dtype=EVENT_DTYPE)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/python/test_events.py -v`
Expected: PASS, 4 passed

- [ ] **Step 5: Commit**

```bash
git add python/shadowfill/events.py tests/python/test_events.py
git commit -m "feat: canonical MBO event schema"
```

---

## Task 3: LOBSTER adapter and data fetch script

**Files:**
- Create: `python/shadowfill/lobster.py`, `scripts/fetch_lobster_sample.sh`
- Test: `tests/python/test_lobster.py`

**Context for the engineer:** LOBSTER message files are headerless CSV with six columns: `Time, Type, OrderID, Size, Price, Direction`. `Time` is seconds after midnight with a fractional part of up to nine digits. `Direction` is `1` for a buy (bid-side) limit order and `-1` for a sell (ask-side) limit order, and for execution events it refers to the side of the *resting* order — so a bid-side execution is a seller-initiated trade. Never parse `Time` as a float: `57600.123456789` does not survive `float64` round-tripping at nanosecond resolution.

- [ ] **Step 1: Write the failing test**

`tests/python/test_lobster.py`:
```python
import numpy as np
import pytest

from shadowfill.events import EVENT_DTYPE, EventType, Side
from shadowfill.lobster import load_lobster_messages, parse_seconds_to_ns

RAW = """34200.123456789,1,11885113,21,2238100,-1
34200.123456790,3,11885113,21,2238100,-1
34200.200000000,4,11885114,100,2238000,1
34201.000000000,5,0,50,2238050,-1
"""


@pytest.fixture
def message_file(tmp_path):
    p = tmp_path / "TEST_2012-06-21_34200000_57600000_message_10.csv"
    p.write_text(RAW)
    return p


def test_parse_seconds_to_ns_is_exact_at_nanosecond_resolution():
    import pandas as pd

    out = parse_seconds_to_ns(pd.Series(["34200.123456789", "1", "0.000000001"]))
    assert out.tolist() == [34200123456789, 1_000_000_000, 1]


def test_load_returns_canonical_dtype_and_monotone_seq(message_file):
    ev = load_lobster_messages(message_file)
    assert ev.dtype == EVENT_DTYPE
    assert ev.shape == (4,)
    assert np.array_equal(ev["seq"], np.arange(4, dtype=np.uint64))


def test_direction_maps_to_resting_side(message_file):
    ev = load_lobster_messages(message_file)
    assert ev["side"][0] == Side.ASK
    assert ev["side"][2] == Side.BID


def test_types_and_prices_preserved_as_integers(message_file):
    ev = load_lobster_messages(message_file)
    assert ev["type"][0] == EventType.ADD
    assert ev["type"][3] == EventType.EXECUTE_HIDDEN
    assert ev["price"][0] == 2238100
    assert ev["size"][2] == 100


def test_timestamps_are_nondecreasing(message_file):
    ev = load_lobster_messages(message_file)
    assert np.all(np.diff(ev["ts_ns"]) >= 0)


def test_rejects_out_of_order_timestamps(tmp_path):
    p = tmp_path / "bad_message_10.csv"
    p.write_text("34200.000000002,1,1,10,100,1\n34200.000000001,1,2,10,100,1\n")
    with pytest.raises(ValueError, match="not sorted by timestamp"):
        load_lobster_messages(p)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/python/test_lobster.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'shadowfill.lobster'`

- [ ] **Step 3: Write minimal implementation**

`python/shadowfill/lobster.py`:
```python
"""Adapter: LOBSTER message/orderbook CSV files -> canonical events."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .events import EVENT_DTYPE, Side, empty_events

MESSAGE_COLUMNS = ["time", "type", "order_id", "size", "price", "direction"]


def parse_seconds_to_ns(col: pd.Series) -> np.ndarray:
    """Convert 'seconds-after-midnight' decimal strings to int64 nanoseconds.

    Parsing via float64 loses nanosecond precision for intraday timestamps,
    so the integer and fractional parts are handled separately as strings.
    """
    parts = col.astype(str).str.strip().str.split(".", n=1, expand=True)
    seconds = parts[0].astype("int64")
    if parts.shape[1] == 1:
        frac = pd.Series(np.zeros(len(col), dtype="int64"), index=col.index)
    else:
        frac_str = parts[1].fillna("").str.slice(0, 9).str.ljust(9, "0")
        frac = frac_str.replace("", "0").astype("int64")
    return (seconds * 1_000_000_000 + frac).to_numpy(dtype="int64")


def load_lobster_messages(path: str | Path) -> np.ndarray:
    """Load a LOBSTER message file into a canonical event array."""
    df = pd.read_csv(
        path,
        header=None,
        names=MESSAGE_COLUMNS,
        dtype={"time": str, "type": "int64", "order_id": "int64",
               "size": "int64", "price": "int64", "direction": "int64"},
    )

    ts_ns = parse_seconds_to_ns(df["time"])
    if np.any(np.diff(ts_ns) < 0):
        raise ValueError(f"{path}: message file is not sorted by timestamp")

    ev = empty_events(len(df))
    ev["ts_ns"] = ts_ns
    ev["seq"] = np.arange(len(df), dtype=np.uint64)
    ev["order_id"] = df["order_id"].to_numpy(dtype="uint64")
    ev["price"] = df["price"].to_numpy(dtype="int64")
    ev["size"] = df["size"].to_numpy(dtype="int64")
    ev["type"] = df["type"].to_numpy(dtype="uint8")
    ev["side"] = np.where(
        df["direction"].to_numpy() == 1, int(Side.BID), int(Side.ASK)
    ).astype("int8")
    assert ev.dtype == EVENT_DTYPE
    return ev


def load_lobster_orderbook(path: str | Path, levels: int) -> pd.DataFrame:
    """Load a LOBSTER orderbook snapshot file (one row per message row).

    Columns repeat as ask_price_1, ask_size_1, bid_price_1, bid_size_1, ...
    """
    names: list[str] = []
    for i in range(1, levels + 1):
        names += [f"ask_price_{i}", f"ask_size_{i}", f"bid_price_{i}", f"bid_size_{i}"]
    return pd.read_csv(path, header=None, names=names, dtype="int64")
```

`scripts/fetch_lobster_sample.sh`:
```bash
#!/usr/bin/env bash
# Downloads the free LOBSTER sample files used by the validation tests.
# The sample is NOT committed to this repository: it is third-party data with
# its own terms of use, and CI runs against tests/fixtures/synthetic_mbo_v1.csv.
#
# Usage:  ./scripts/fetch_lobster_sample.sh  data/lobster
#
# Go to https://lobsterdata.com/info/DataSamples.php, download the level-10
# sample archive(s) for the tickers you want, and unzip them into $1.
# Expected layout after unzipping:
#   data/lobster/AMZN_2012-06-21_34200000_57600000_message_10.csv
#   data/lobster/AMZN_2012-06-21_34200000_57600000_orderbook_10.csv
set -euo pipefail
DEST="${1:-data/lobster}"
mkdir -p "$DEST"
echo "Place the unzipped LOBSTER sample CSV files in: $DEST"
echo "Then run: pytest -m needs_lobster -v"
ls -1 "$DEST" || true
```

- [ ] **Step 4: Run test to verify it passes**

Run: `chmod +x scripts/fetch_lobster_sample.sh && pytest tests/python/test_lobster.py -v`
Expected: PASS, 6 passed

- [ ] **Step 5: Commit**

```bash
git add python/shadowfill/lobster.py scripts/fetch_lobster_sample.sh tests/python/test_lobster.py
git commit -m "feat: LOBSTER message/orderbook adapter with exact nanosecond parsing"
```

---

## Task 4: Python reference order book

**Files:**
- Create: `python/shadowfill/replay.py`
- Test: `tests/python/test_refbook.py`

**Context for the engineer:** This is the oracle. Optimise for obviousness, not speed — `best_bid()` doing a linear `max()` over a dict is fine here because the C++ engine is the fast path. The one non-obvious rule is that `EXECUTE_HIDDEN`, `CROSS` and `HALT` leave the visible book untouched.

- [ ] **Step 1: Write the failing test**

`tests/python/test_refbook.py`:
```python
from shadowfill.events import EventType, Side
from shadowfill.replay import RefBook


def add(book, seq, oid, price, size, side):
    book.apply(seq * 1000, seq, oid, price, size, EventType.ADD, side)


def test_add_accumulates_level_size():
    b = RefBook()
    add(b, 0, 1, 100, 10, Side.BID)
    add(b, 1, 2, 100, 5, Side.BID)
    assert b.level_size(Side.BID, 100) == 15
    assert b.best_bid() == 100


def test_delete_removes_order_and_level_when_empty():
    b = RefBook()
    add(b, 0, 1, 100, 10, Side.BID)
    b.apply(1000, 1, 1, 100, 10, EventType.DELETE, Side.BID)
    assert b.level_size(Side.BID, 100) == 0
    assert b.best_bid() is None
    assert b.find(1) is None


def test_partial_cancel_reduces_size_but_keeps_order():
    b = RefBook()
    add(b, 0, 1, 100, 10, Side.BID)
    b.apply(1000, 1, 1, 100, 4, EventType.CANCEL_PARTIAL, Side.BID)
    assert b.level_size(Side.BID, 100) == 6
    assert b.find(1).size == 6


def test_execute_consumes_resting_size():
    b = RefBook()
    add(b, 0, 1, 100, 10, Side.ASK)
    b.apply(1000, 1, 1, 100, 10, EventType.EXECUTE, Side.ASK)
    assert b.level_size(Side.ASK, 100) == 0
    assert b.best_ask() is None


def test_hidden_execution_does_not_touch_the_book():
    b = RefBook()
    add(b, 0, 1, 100, 10, Side.ASK)
    b.apply(1000, 1, 0, 100, 7, EventType.EXECUTE_HIDDEN, Side.ASK)
    assert b.level_size(Side.ASK, 100) == 10


def test_unknown_order_still_decrements_level_and_is_counted():
    b = RefBook()
    add(b, 0, 1, 100, 10, Side.BID)
    b.apply(1000, 1, 999, 100, 4, EventType.EXECUTE, Side.BID)
    assert b.level_size(Side.BID, 100) == 6
    assert b.unknown_order_events == 1


def test_best_bid_and_ask_pick_correct_extremes():
    b = RefBook()
    add(b, 0, 1, 100, 10, Side.BID)
    add(b, 1, 2, 101, 10, Side.BID)
    add(b, 2, 3, 105, 10, Side.ASK)
    add(b, 3, 4, 104, 10, Side.ASK)
    assert b.best_bid() == 101
    assert b.best_ask() == 104


def test_arrival_seq_is_recorded_for_priority():
    b = RefBook()
    add(b, 0, 1, 100, 10, Side.BID)
    add(b, 7, 2, 100, 10, Side.BID)
    assert b.find(1).seq == 0
    assert b.find(2).seq == 7
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/python/test_refbook.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'shadowfill.replay'`

- [ ] **Step 3: Write minimal implementation**

`python/shadowfill/replay.py`:
```python
"""Reference (oracle) implementations of book reconstruction and shadow tracking.

Correctness over speed. The C++ engine in ``shadowfill._core`` must produce
byte-identical outcomes to this module; where they differ, this module is right.
"""

from __future__ import annotations

from dataclasses import dataclass

from .events import EventType, Side


@dataclass
class Resting:
    """A live order in the visible book."""

    price: int
    size: int
    seq: int
    side: int


class RefBook:
    """Order-by-order book state keyed by exchange order id."""

    def __init__(self) -> None:
        self.orders: dict[int, Resting] = {}
        self.bids: dict[int, int] = {}
        self.asks: dict[int, int] = {}
        self.unknown_order_events: int = 0

    def _levels(self, side: int) -> dict[int, int]:
        return self.bids if side == Side.BID else self.asks

    def _reduce_level(self, side: int, price: int, qty: int) -> None:
        levels = self._levels(side)
        remaining = levels.get(price, 0) - qty
        if remaining > 0:
            levels[price] = remaining
        else:
            levels.pop(price, None)

    def apply(
        self,
        ts_ns: int,
        seq: int,
        order_id: int,
        price: int,
        size: int,
        etype: int,
        side: int,
    ) -> None:
        """Apply one canonical event to the visible book."""
        if etype == EventType.ADD:
            self.orders[order_id] = Resting(price=price, size=size, seq=seq, side=side)
            levels = self._levels(side)
            levels[price] = levels.get(price, 0) + size
            return

        if etype in (EventType.CANCEL_PARTIAL, EventType.DELETE, EventType.EXECUTE):
            order = self.orders.get(order_id)
            if order is None:
                # Added before the recording window or outside the level band.
                self.unknown_order_events += 1
                self._reduce_level(side, price, size)
                return
            order.size -= size
            self._reduce_level(order.side, order.price, size)
            if order.size <= 0:
                del self.orders[order_id]
            return

        # EXECUTE_HIDDEN, CROSS, HALT leave the visible book unchanged.
        return

    def find(self, order_id: int) -> Resting | None:
        return self.orders.get(order_id)

    def level_size(self, side: int, price: int) -> int:
        return self._levels(side).get(price, 0)

    def best_bid(self) -> int | None:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> int | None:
        return min(self.asks) if self.asks else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/python/test_refbook.py -v`
Expected: PASS, 8 passed

- [ ] **Step 5: Commit**

```bash
git add python/shadowfill/replay.py tests/python/test_refbook.py
git commit -m "feat: reference order-by-order book reconstruction"
```

---

## Task 5: Validate reconstruction against LOBSTER's own snapshots

**Files:**
- Create: `tests/python/test_refbook_vs_lobster_orderbook.py`, `tests/python/conftest.py`
- Modify: `python/shadowfill/replay.py` (add `top_of_book_series`)

**Context for the engineer:** LOBSTER ships an `orderbook` file whose row *i* is the book state immediately **after** message row *i*. Replaying the message file and comparing top-of-book row by row is a free external correctness check on the reconstruction. Rows where the true best price sits outside the recorded level band are excluded — LOBSTER pads those with sentinel prices (`9999999999` on the ask side, `-9999999999` on the bid side).

- [ ] **Step 1: Write the failing test**

`tests/python/conftest.py`:
```python
import os
from pathlib import Path

import pytest

LOBSTER_DIR = Path(os.environ.get("SHADOWFILL_LOBSTER_DIR", "data/lobster"))


@pytest.fixture(scope="session")
def lobster_pair():
    """(message_path, orderbook_path, levels) for the sample, or skip."""
    messages = sorted(LOBSTER_DIR.glob("*_message_10.csv"))
    if not messages:
        pytest.skip(f"no LOBSTER sample in {LOBSTER_DIR}; run scripts/fetch_lobster_sample.sh")
    message_path = messages[0]
    orderbook_path = Path(str(message_path).replace("_message_10.csv", "_orderbook_10.csv"))
    if not orderbook_path.exists():
        pytest.skip(f"missing matching orderbook file for {message_path.name}")
    return message_path, orderbook_path, 10
```

`tests/python/test_refbook_vs_lobster_orderbook.py`:
```python
import numpy as np
import pytest

from shadowfill.lobster import load_lobster_messages, load_lobster_orderbook
from shadowfill.replay import top_of_book_series

SENTINEL = 9_999_999_999


@pytest.mark.needs_lobster
def test_reconstructed_top_of_book_matches_lobster_snapshots(lobster_pair):
    message_path, orderbook_path, levels = lobster_pair
    events = load_lobster_messages(message_path)
    snapshots = load_lobster_orderbook(orderbook_path, levels)
    assert len(events) == len(snapshots)

    tob = top_of_book_series(events)

    expected_ask = snapshots["ask_price_1"].to_numpy()
    expected_bid = snapshots["bid_price_1"].to_numpy()
    in_band = (expected_ask != SENTINEL) & (expected_bid != -SENTINEL)
    in_band &= (tob["best_ask"] != 0) & (tob["best_bid"] != 0)

    assert in_band.mean() > 0.95, "level band excludes too much of the session"
    np.testing.assert_array_equal(tob["best_ask"][in_band], expected_ask[in_band])
    np.testing.assert_array_equal(tob["best_bid"][in_band], expected_bid[in_band])


@pytest.mark.needs_lobster
def test_unknown_order_rate_is_small(lobster_pair):
    from shadowfill.replay import RefBook

    message_path, _, _ = lobster_pair
    events = load_lobster_messages(message_path)
    book = RefBook()
    for e in events:
        book.apply(
            int(e["ts_ns"]), int(e["seq"]), int(e["order_id"]),
            int(e["price"]), int(e["size"]), int(e["type"]), int(e["side"]),
        )
    assert book.unknown_order_events / len(events) < 0.05
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/python/test_refbook_vs_lobster_orderbook.py -v`
Expected: FAIL with `ImportError: cannot import name 'top_of_book_series'` (or SKIP if no data — in that case set `SHADOWFILL_LOBSTER_DIR` first; the import error must be fixed regardless)

- [ ] **Step 3: Write minimal implementation**

Append to `python/shadowfill/replay.py`. **Put `import numpy as np` at the top of the file with the existing imports, not inline** — ruff's isort rule (`I`) fails the build on mid-file imports:

```python
# top of file, alongside `from dataclasses import dataclass`:
#     import numpy as np

TOB_DTYPE = np.dtype(
    [("ts_ns", "<i8"), ("seq", "<u8"), ("best_bid", "<i8"), ("best_ask", "<i8")]
)


def top_of_book_series(events: np.ndarray) -> np.ndarray:
    """Replay ``events`` and return top-of-book *after* each event.

    A best price of 0 means that side of the book was empty.
    """
    book = RefBook()
    out = np.zeros(len(events), dtype=TOB_DTYPE)
    for i, e in enumerate(events):
        book.apply(
            int(e["ts_ns"]),
            int(e["seq"]),
            int(e["order_id"]),
            int(e["price"]),
            int(e["size"]),
            int(e["type"]),
            int(e["side"]),
        )
        out[i]["ts_ns"] = e["ts_ns"]
        out[i]["seq"] = e["seq"]
        out[i]["best_bid"] = book.best_bid() or 0
        out[i]["best_ask"] = book.best_ask() or 0
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `SHADOWFILL_LOBSTER_DIR=data/lobster pytest tests/python/test_refbook_vs_lobster_orderbook.py -v`
Expected: PASS, 2 passed (or 2 skipped on a machine without the sample — then run it once locally with the sample before moving on; do not proceed on skips alone)

- [ ] **Step 5: Commit**

```bash
git add python/shadowfill/replay.py tests/python/conftest.py tests/python/test_refbook_vs_lobster_orderbook.py
git commit -m "test: validate book reconstruction against LOBSTER orderbook snapshots"
```

---

## Task 6: Reference shadow tracker (never-cancel ground truth)

**Files:**
- Modify: `python/shadowfill/replay.py` (add `Placement`, `Outcome`, `Status`, `RefShadowTracker`, `run_reference`)
- Test: `tests/python/test_shadow_tracker.py`

**Context for the engineer:** The processing order within one event is load-bearing and must be exactly:

1. expire actives whose `expiry_ts < ev.ts_ns`
2. activate placements whose `ts_ns + latency_ns <= ev.ts_ns`, reading `ahead` from the book state *before* this event
3. match the event against actives (needs the pre-event book to resolve order ids)
4. apply the event to the book

Swapping 3 and 4 silently breaks the ahead/behind test for cancellations, because the order is gone by the time you look it up.

- [ ] **Step 1: Write the failing test**

`tests/python/test_shadow_tracker.py`:
```python
import numpy as np

from shadowfill.events import EVENT_DTYPE, EventType, Side
from shadowfill.replay import Placement, Status, run_reference

SEC = 1_000_000_000


def make_events(rows):
    ev = np.zeros(len(rows), dtype=EVENT_DTYPE)
    for i, (ts, oid, price, size, etype, side) in enumerate(rows):
        ev[i] = (ts, i, oid, price, size, etype, side)
    return ev


def place(price=100, size=10, ts=0, latency=0, horizon=10 * SEC, side=Side.BID):
    return Placement(
        shadow_id=0, ts_ns=ts, latency_ns=latency, side=int(side),
        price=price, size=size, horizon_ns=horizon,
    )


def test_ahead_at_insert_is_resting_volume_at_that_price():
    events = make_events([
        (0, 1, 100, 30, EventType.ADD, Side.BID),
        (1 * SEC, 2, 100, 20, EventType.ADD, Side.BID),
        (2 * SEC, 3, 100, 5, EventType.ADD, Side.BID),
    ])
    out = run_reference(events, [place(ts=1 * SEC + 1)])[0]
    assert out.ahead_at_insert == 50
    assert out.insert_seq == 2


def test_orders_added_after_insertion_are_behind_and_never_count():
    events = make_events([
        (0, 1, 100, 30, EventType.ADD, Side.BID),
        (1 * SEC, 2, 100, 999, EventType.ADD, Side.BID),
        (2 * SEC, 1, 100, 30, EventType.EXECUTE, Side.BID),
        (3 * SEC, 3, 100, 10, EventType.EXECUTE, Side.BID),
    ])
    out = run_reference(events, [place(ts=0, size=10)])[0]
    assert out.ahead_at_insert == 30
    assert out.status == Status.FILLED
    assert out.first_fill_ts == 3 * SEC


def test_cancellation_ahead_reduces_queue_but_behind_does_not():
    events = make_events([
        (0, 1, 100, 30, EventType.ADD, Side.BID),
        (1 * SEC, 2, 100, 40, EventType.ADD, Side.BID),
        (2 * SEC, 2, 100, 40, EventType.DELETE, Side.BID),
        (3 * SEC, 1, 100, 25, EventType.CANCEL_PARTIAL, Side.BID),
    ])
    out = run_reference(events, [place(ts=500_000_000, size=10)])[0]
    assert out.ahead_at_insert == 30
    assert out.ahead_at_end == 5


def test_hidden_execution_never_consumes_the_queue():
    events = make_events([
        (0, 1, 100, 10, EventType.ADD, Side.BID),
        (1 * SEC, 0, 100, 10, EventType.EXECUTE_HIDDEN, Side.BID),
        (2 * SEC, 0, 100, 10, EventType.EXECUTE_HIDDEN, Side.BID),
    ])
    out = run_reference(events, [place(ts=0, size=10)])[0]
    assert out.ahead_at_end == 10
    assert out.status == Status.EXPIRED


def test_partial_then_full_fill_records_both_timestamps():
    events = make_events([
        (0, 1, 100, 5, EventType.ADD, Side.BID),
        (1 * SEC, 1, 100, 5, EventType.EXECUTE, Side.BID),
        (2 * SEC, 2, 100, 3, EventType.EXECUTE, Side.BID),
        (3 * SEC, 3, 100, 7, EventType.EXECUTE, Side.BID),
    ])
    out = run_reference(events, [place(ts=0, size=10)])[0]
    assert out.first_fill_ts == 2 * SEC
    assert out.full_fill_ts == 3 * SEC
    assert out.filled_qty == 10
    assert out.status == Status.FILLED


def test_single_sweep_larger_than_queue_fills_the_shadow_immediately():
    events = make_events([
        (0, 1, 100, 20, EventType.ADD, Side.BID),
        (1 * SEC, 1, 100, 100, EventType.EXECUTE, Side.BID),
    ])
    out = run_reference(events, [place(ts=0, size=10)])[0]
    assert out.status == Status.FILLED
    assert out.first_fill_ts == out.full_fill_ts == 1 * SEC


def test_latency_puts_intervening_orders_ahead_of_the_shadow():
    events = make_events([
        (0, 1, 100, 10, EventType.ADD, Side.BID),
        (1 * SEC, 2, 100, 40, EventType.ADD, Side.BID),
        (3 * SEC, 1, 100, 10, EventType.EXECUTE, Side.BID),
    ])
    zero_latency = run_reference(events, [place(ts=0, latency=0)])[0]
    with_latency = run_reference(events, [place(ts=0, latency=2 * SEC)])[0]
    assert zero_latency.ahead_at_insert == 10
    assert with_latency.ahead_at_insert == 50


def test_horizon_expiry_marks_expired_not_filled():
    events = make_events([
        (0, 1, 100, 10, EventType.ADD, Side.BID),
        (5 * SEC, 1, 100, 10, EventType.EXECUTE, Side.BID),
    ])
    out = run_reference(events, [place(ts=0, horizon=2 * SEC)])[0]
    assert out.status == Status.EXPIRED
    assert out.first_fill_ts == -1


def test_still_resting_at_end_of_data_is_truncated():
    events = make_events([(0, 1, 100, 10, EventType.ADD, Side.BID)])
    out = run_reference(events, [place(ts=0, horizon=10 * SEC)])[0]
    assert out.status == Status.TRUNCATED


def test_events_at_other_prices_and_sides_are_ignored():
    events = make_events([
        (0, 1, 100, 10, EventType.ADD, Side.BID),
        (1 * SEC, 2, 101, 10, EventType.EXECUTE, Side.BID),
        (2 * SEC, 3, 100, 10, EventType.EXECUTE, Side.ASK),
    ])
    out = run_reference(events, [place(ts=0, price=100, side=Side.BID)])[0]
    assert out.ahead_at_end == 10
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/python/test_shadow_tracker.py -v`
Expected: FAIL with `ImportError: cannot import name 'Placement' from 'shadowfill.replay'`

- [ ] **Step 3: Write minimal implementation**

Append to `python/shadowfill/replay.py`. **Put `from enum import IntEnum` at the top of the file with the existing imports**, for the same ruff reason as Task 5:

```python
# top of file:
#     from enum import IntEnum


class Status(IntEnum):
    RESTING = 0
    FILLED = 1
    EXPIRED = 2
    TRUNCATED = 3


@dataclass(frozen=True)
class Placement:
    """A hypothetical passive order to evaluate."""

    shadow_id: int
    ts_ns: int
    latency_ns: int
    side: int
    price: int
    size: int
    horizon_ns: int

    @property
    def effective_ts(self) -> int:
        return self.ts_ns + self.latency_ns


@dataclass
class Outcome:
    """Ground-truth result for one never-cancel shadow order."""

    shadow_id: int
    status: int = int(Status.RESTING)
    insert_ts: int = -1
    insert_seq: int = -1
    ahead_at_insert: int = -1
    ahead_at_end: int = -1
    first_fill_ts: int = -1
    full_fill_ts: int = -1
    filled_qty: int = 0


@dataclass
class _Active:
    placement: Placement
    outcome: Outcome
    ahead: int
    expiry_ts: int


class RefShadowTracker:
    """Never-cancel shadow-order accounting over a canonical event stream."""

    def __init__(self, book: RefBook, placements: list[Placement]) -> None:
        self.book = book
        self._pending = sorted(placements, key=lambda p: (p.effective_ts, p.shadow_id))
        self._next = 0
        self._active: list[_Active] = []
        self.outcomes: dict[int, Outcome] = {}
        self.unknown_order_assumed_ahead = 0
        self.fifo_violations = 0

    def _expire(self, now_ts: int) -> None:
        still: list[_Active] = []
        for a in self._active:
            if a.expiry_ts < now_ts:
                a.outcome.status = int(Status.EXPIRED)
                a.outcome.ahead_at_end = a.ahead
                self.outcomes[a.outcome.shadow_id] = a.outcome
            else:
                still.append(a)
        self._active = still

    def _activate(self, now_ts: int, now_seq: int) -> None:
        while self._next < len(self._pending):
            p = self._pending[self._next]
            if p.effective_ts > now_ts:
                break
            outcome = Outcome(
                shadow_id=p.shadow_id,
                insert_ts=now_ts,
                insert_seq=now_seq,
                ahead_at_insert=self.book.level_size(p.side, p.price),
            )
            self._active.append(
                _Active(
                    placement=p,
                    outcome=outcome,
                    ahead=outcome.ahead_at_insert,
                    expiry_ts=now_ts + p.horizon_ns,
                )
            )
            self._next += 1

    def _is_ahead(self, order_id: int, insert_seq: int) -> bool:
        order = self.book.find(order_id)
        if order is None:
            self.unknown_order_assumed_ahead += 1
            return True
        return order.seq < insert_seq

    def _match(self, ts_ns, order_id, price, size, etype, side) -> None:
        if etype not in (EventType.CANCEL_PARTIAL, EventType.DELETE, EventType.EXECUTE):
            return  # ADD is behind us; hidden/cross/halt touch no visible queue
        for a in self._active:
            p = a.placement
            if p.side != side or p.price != price:
                continue
            if etype in (EventType.CANCEL_PARTIAL, EventType.DELETE):
                if self._is_ahead(order_id, a.outcome.insert_seq):
                    a.ahead = max(0, a.ahead - size)
                continue
            # EXECUTE: price-time priority means the front of the queue is hit first.
            if a.ahead == 0 and not self._is_ahead(order_id, a.outcome.insert_seq):
                self.fifo_violations += 1
            consumed = min(size, a.ahead)
            a.ahead -= consumed
            residual = size - consumed
            if residual <= 0:
                continue
            remaining = p.size - a.outcome.filled_qty
            fill = min(residual, remaining)
            if fill <= 0:
                continue
            if a.outcome.filled_qty == 0:
                a.outcome.first_fill_ts = ts_ns
            a.outcome.filled_qty += fill
            if a.outcome.filled_qty >= p.size:
                a.outcome.full_fill_ts = ts_ns
                a.outcome.status = int(Status.FILLED)
                a.outcome.ahead_at_end = a.ahead

    def _harvest_filled(self) -> None:
        still: list[_Active] = []
        for a in self._active:
            if a.outcome.status == int(Status.FILLED):
                self.outcomes[a.outcome.shadow_id] = a.outcome
            else:
                still.append(a)
        self._active = still

    def on_event(self, ts_ns, seq, order_id, price, size, etype, side) -> None:
        self._expire(ts_ns)
        self._activate(ts_ns, seq)
        self._match(ts_ns, order_id, price, size, etype, side)
        self._harvest_filled()
        self.book.apply(ts_ns, seq, order_id, price, size, etype, side)

    def finalize(self) -> None:
        for a in self._active:
            a.outcome.status = int(Status.TRUNCATED)
            a.outcome.ahead_at_end = a.ahead
            self.outcomes[a.outcome.shadow_id] = a.outcome
        self._active = []
        while self._next < len(self._pending):
            p = self._pending[self._next]
            self.outcomes[p.shadow_id] = Outcome(
                shadow_id=p.shadow_id, status=int(Status.TRUNCATED)
            )
            self._next += 1


def run_reference(events: np.ndarray, placements: list[Placement]) -> list[Outcome]:
    """Replay ``events`` and return one Outcome per placement, ordered by shadow_id."""
    tracker = RefShadowTracker(RefBook(), placements)
    for e in events:
        tracker.on_event(
            int(e["ts_ns"]), int(e["seq"]), int(e["order_id"]),
            int(e["price"]), int(e["size"]), int(e["type"]), int(e["side"]),
        )
    tracker.finalize()
    return [tracker.outcomes[p.shadow_id] for p in sorted(placements, key=lambda x: x.shadow_id)]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/python/test_shadow_tracker.py -v`
Expected: PASS, 10 passed

- [ ] **Step 5: Commit**

```bash
git add python/shadowfill/replay.py tests/python/test_shadow_tracker.py
git commit -m "feat: never-cancel shadow-order ground-truth tracker"
```

---

## Task 7: Synthetic fixture generator

**Files:**
- Create: `python/shadowfill/synthetic.py`, `tests/fixtures/synthetic_mbo_v1.csv`
- Test: `tests/python/test_synthetic.py`

**Context for the engineer:** CI cannot depend on third-party data, and Plan 4 needs a stream whose censoring mechanism is known by construction. This generator emits a LOBSTER-format message file from a seeded zero-intelligence process, so it round-trips through the same adapter as real data.

- [ ] **Step 1: Write the failing test**

`tests/python/test_synthetic.py`:
```python
import numpy as np

from shadowfill.events import EventType
from shadowfill.lobster import load_lobster_messages
from shadowfill.replay import RefBook
from shadowfill.synthetic import generate_synthetic_messages, write_synthetic_csv


def test_generation_is_deterministic_for_a_seed():
    a = generate_synthetic_messages(n_events=2000, seed=7)
    b = generate_synthetic_messages(n_events=2000, seed=7)
    np.testing.assert_array_equal(a, b)


def test_generation_differs_across_seeds():
    a = generate_synthetic_messages(n_events=2000, seed=7)
    b = generate_synthetic_messages(n_events=2000, seed=8)
    assert not np.array_equal(a, b)


def test_stream_never_produces_negative_level_sizes(tmp_path):
    events = generate_synthetic_messages(n_events=20000, seed=1)
    book = RefBook()
    for e in events:
        book.apply(
            int(e["ts_ns"]), int(e["seq"]), int(e["order_id"]),
            int(e["price"]), int(e["size"]), int(e["type"]), int(e["side"]),
        )
        assert all(v > 0 for v in book.bids.values())
        assert all(v > 0 for v in book.asks.values())


def test_csv_roundtrips_through_the_lobster_adapter(tmp_path):
    events = generate_synthetic_messages(n_events=500, seed=3)
    path = tmp_path / "synthetic_message_10.csv"
    write_synthetic_csv(events, path)
    reloaded = load_lobster_messages(path)
    np.testing.assert_array_equal(events, reloaded)


def test_stream_contains_every_event_type_we_care_about():
    events = generate_synthetic_messages(n_events=20000, seed=5)
    present = set(events["type"].tolist())
    for required in (
        EventType.ADD, EventType.CANCEL_PARTIAL,
        EventType.DELETE, EventType.EXECUTE, EventType.EXECUTE_HIDDEN,
    ):
        assert int(required) in present
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/python/test_synthetic.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'shadowfill.synthetic'`

- [ ] **Step 3: Write minimal implementation**

`python/shadowfill/synthetic.py`:
```python
"""Deterministic synthetic MBO stream, in LOBSTER message-file format.

Used so CI never depends on third-party data, and so Plan 4 has a stream whose
cancellation mechanism is independent of the fill outcome by construction.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .events import EventType, Side, empty_events


def generate_synthetic_messages(
    n_events: int,
    seed: int,
    *,
    tick: int = 100,
    mid_start: int = 1_000_000,
    max_levels: int = 5,
    mean_size: int = 20,
) -> np.ndarray:
    """Generate a canonical event array from a seeded zero-intelligence process."""
    rng = np.random.default_rng(seed)
    events = empty_events(n_events)

    resting: dict[int, tuple[int, int, int]] = {}  # oid -> (price, size, side)
    next_oid = 1
    ts = 0
    mid = mid_start
    written = 0

    while written < n_events:
        ts += int(rng.exponential(1_000_000)) + 1
        side = int(Side.BID) if rng.random() < 0.5 else int(Side.ASK)
        live = [oid for oid, (_, _, s) in resting.items() if s == side]
        roll = rng.random()

        if roll < 0.55 or not live:
            level = int(rng.integers(0, max_levels))
            price = mid - tick * (level + 1) if side == Side.BID else mid + tick * (level + 1)
            size = int(rng.poisson(mean_size)) + 1
            oid = next_oid
            next_oid += 1
            resting[oid] = (price, size, side)
            events[written] = (ts, written, oid, price, size, int(EventType.ADD), side)
            written += 1
            continue

        oid = int(rng.choice(live))
        price, size, _ = resting[oid]

        if roll < 0.70:
            qty = max(1, size // 2)
            if qty >= size:
                del resting[oid]
                etype = int(EventType.DELETE)
                qty = size
            else:
                resting[oid] = (price, size - qty, side)
                etype = int(EventType.CANCEL_PARTIAL)
            events[written] = (ts, written, oid, price, qty, etype, side)
            written += 1
            continue

        if roll < 0.80:
            del resting[oid]
            events[written] = (ts, written, oid, price, size, int(EventType.DELETE), side)
            written += 1
            continue

        if roll < 0.95:
            qty = int(min(size, rng.integers(1, mean_size + 1)))
            if qty >= size:
                del resting[oid]
            else:
                resting[oid] = (price, size - qty, side)
            events[written] = (ts, written, oid, price, qty, int(EventType.EXECUTE), side)
            written += 1
            mid += tick if side == Side.ASK else -tick
            continue

        qty = int(rng.integers(1, mean_size + 1))
        events[written] = (ts, written, 0, price, qty, int(EventType.EXECUTE_HIDDEN), side)
        written += 1

    return events


def write_synthetic_csv(events: np.ndarray, path: str | Path) -> None:
    """Write a canonical event array in LOBSTER message-file format."""
    lines = []
    for e in events:
        seconds, frac = divmod(int(e["ts_ns"]), 1_000_000_000)
        direction = 1 if int(e["side"]) == int(Side.BID) else -1
        lines.append(
            f"{seconds}.{frac:09d},{int(e['type'])},{int(e['order_id'])},"
            f"{int(e['size'])},{int(e['price'])},{direction}"
        )
    Path(path).write_text("\n".join(lines) + "\n")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/python/test_synthetic.py -v`
Expected: PASS, 5 passed

Then generate the committed fixture:
```bash
python -c "
from pathlib import Path
from shadowfill.synthetic import generate_synthetic_messages, write_synthetic_csv
Path('tests/fixtures').mkdir(parents=True, exist_ok=True)
write_synthetic_csv(generate_synthetic_messages(n_events=200_000, seed=20260919),
                    'tests/fixtures/synthetic_mbo_v1.csv')
"
```

- [ ] **Step 5: Commit**

```bash
git add python/shadowfill/synthetic.py tests/python/test_synthetic.py tests/fixtures/synthetic_mbo_v1.csv
git commit -m "feat: deterministic synthetic MBO fixture generator"
```

---

## Task 8: Invariant and leakage tests

**Files:**
- Create: `tests/python/test_invariants.py`

**Context for the engineer:** These are the tests that catch the failure modes nobody notices. `test_prefix_invariance` is the anti-leakage test: an outcome determined at event *k* must not change when events after *k* are appended. If it does, some future information leaked into the accounting.

- [ ] **Step 1: Write the failing test**

`tests/python/test_invariants.py`:
```python
import numpy as np
import pytest

from shadowfill.events import EventType, Side
from shadowfill.replay import (
    Placement, RefBook, RefShadowTracker, Status, run_reference,
)
from shadowfill.synthetic import generate_synthetic_messages

SEC = 1_000_000_000


def grid_placements(events, *, every=500, size=5, horizon=2 * SEC):
    """Place one shadow per side every ``every`` events at the prevailing best."""
    book = RefBook()
    placements = []
    sid = 0
    for i, e in enumerate(events):
        if i % every == 0:
            for side, price in ((Side.BID, book.best_bid()), (Side.ASK, book.best_ask())):
                if price is None:
                    continue
                placements.append(
                    Placement(
                        shadow_id=sid, ts_ns=int(e["ts_ns"]), latency_ns=0,
                        side=int(side), price=price, size=size, horizon_ns=horizon,
                    )
                )
                sid += 1
        book.apply(
            int(e["ts_ns"]), int(e["seq"]), int(e["order_id"]),
            int(e["price"]), int(e["size"]), int(e["type"]), int(e["side"]),
        )
    return placements


@pytest.fixture(scope="module")
def stream():
    events = generate_synthetic_messages(n_events=50_000, seed=42)
    return events, grid_placements(events)


def test_every_placement_gets_exactly_one_terminal_outcome(stream):
    events, placements = stream
    outcomes = run_reference(events, placements)
    assert len(outcomes) == len(placements)
    assert {o.shadow_id for o in outcomes} == {p.shadow_id for p in placements}
    assert all(o.status != int(Status.RESTING) for o in outcomes)


def test_queue_ahead_is_monotone_non_increasing():
    events = generate_synthetic_messages(n_events=20_000, seed=11)
    placements = grid_placements(events, every=2000)
    tracker = RefShadowTracker(RefBook(), placements)
    seen: dict[int, int] = {}
    for e in events:
        tracker.on_event(
            int(e["ts_ns"]), int(e["seq"]), int(e["order_id"]),
            int(e["price"]), int(e["size"]), int(e["type"]), int(e["side"]),
        )
        for a in tracker._active:
            sid = a.outcome.shadow_id
            assert a.ahead <= seen.get(sid, a.ahead)
            assert a.ahead >= 0
            seen[sid] = a.ahead


def test_fill_requires_queue_to_have_been_fully_consumed(stream):
    events, placements = stream
    for outcome in run_reference(events, placements):
        if outcome.status == int(Status.FILLED):
            assert outcome.ahead_at_end == 0
            assert 0 <= outcome.first_fill_ts <= outcome.full_fill_ts


def test_filled_quantity_never_exceeds_placement_size(stream):
    events, placements = stream
    by_id = {p.shadow_id: p for p in placements}
    for outcome in run_reference(events, placements):
        assert 0 <= outcome.filled_qty <= by_id[outcome.shadow_id].size


def test_prefix_invariance_no_future_information_leaks(stream):
    """An outcome settled by event k must not change when later events are added."""
    events, placements = stream
    half = len(events) // 2
    cutoff_ts = int(events[half]["ts_ns"])

    early = [p for p in placements if p.ts_ns + p.horizon_ns < cutoff_ts]
    assert early, "fixture produced no placements that settle before the cutoff"

    partial = {o.shadow_id: o for o in run_reference(events[:half], early)}
    full = {o.shadow_id: o for o in run_reference(events, early)}
    for sid, outcome in partial.items():
        if outcome.status in (int(Status.FILLED), int(Status.EXPIRED)):
            assert outcome == full[sid], f"shadow {sid} changed when future events were added"


def test_replay_is_deterministic_across_runs(stream):
    events, placements = stream
    first = run_reference(events, placements)
    second = run_reference(events, placements)
    assert first == second


def test_longer_horizon_weakly_increases_fill_rate():
    events = generate_synthetic_messages(n_events=50_000, seed=3)
    base = grid_placements(events, every=500, horizon=1 * SEC)
    longer = [
        Placement(**{**p.__dict__, "horizon_ns": 8 * SEC}) for p in base
    ]
    fills_short = sum(o.status == int(Status.FILLED) for o in run_reference(events, base))
    fills_long = sum(o.status == int(Status.FILLED) for o in run_reference(events, longer))
    assert fills_long >= fills_short


def test_larger_order_weakly_decreases_full_fill_rate():
    events = generate_synthetic_messages(n_events=50_000, seed=4)
    small = grid_placements(events, every=500, size=1)
    large = [Placement(**{**p.__dict__, "size": 200}) for p in small]
    assert sum(o.status == int(Status.FILLED) for o in run_reference(events, large)) <= sum(
        o.status == int(Status.FILLED) for o in run_reference(events, small)
    )


def test_hidden_executions_alone_never_fill_anything():
    events = generate_synthetic_messages(n_events=30_000, seed=9)
    placements = grid_placements(events, every=1000)
    baseline = run_reference(events, placements)

    hidden_only = events.copy()
    mask = np.isin(hidden_only["type"], [int(EventType.EXECUTE)])
    hidden_only["type"][mask] = int(EventType.EXECUTE_HIDDEN)
    muted = run_reference(hidden_only, placements)

    assert sum(o.status == int(Status.FILLED) for o in muted) == 0
    assert sum(o.status == int(Status.FILLED) for o in baseline) > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/python/test_invariants.py -v`
Expected: FAIL — `test_hidden_executions_alone_never_fill_anything` and `test_prefix_invariance_no_future_information_leaks` are the ones most likely to expose real bugs. If all nine pass immediately, re-read Task 6 step 3 and confirm `_match` runs before `book.apply`.

- [ ] **Step 3: Write minimal implementation**

No new source code: these tests exercise Tasks 4–7. If any fails, fix `python/shadowfill/replay.py` and re-run. The two most likely defects and their fixes:

```python
# Defect A: hidden executions consuming the queue.
# In RefShadowTracker._match, the early return must exclude EXECUTE_HIDDEN:
if etype not in (EventType.CANCEL_PARTIAL, EventType.DELETE, EventType.EXECUTE):
    return

# Defect B: ordering. In RefShadowTracker.on_event, the book must be updated LAST:
self._expire(ts_ns)
self._activate(ts_ns, seq)
self._match(ts_ns, order_id, price, size, etype, side)
self._harvest_filled()
self.book.apply(ts_ns, seq, order_id, price, size, etype, side)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/python/test_invariants.py -v`
Expected: PASS, 9 passed

- [ ] **Step 5: Commit**

```bash
git add tests/python/test_invariants.py
git commit -m "test: queue monotonicity, prefix invariance, and determinism invariants"
```

---

## Task 9: C++ order book

**Files:**
- Create: `src/shadowfill_core/include/shadowfill/event.hpp`, `src/shadowfill_core/include/shadowfill/book.hpp`, `src/shadowfill_core/book.cpp`
- Test: `tests/cpp/test_book.cpp`

**Context for the engineer:** This mirrors `RefBook` exactly, with one difference: price levels live in `std::map` so `best_bid()`/`best_ask()` are O(log n) instead of O(n). Bids use `std::greater` so `begin()` is the best bid on both sides.

- [ ] **Step 1: Write the failing test**

`tests/cpp/test_book.cpp`:
```cpp
#include <catch2/catch_test_macros.hpp>
#include "shadowfill/book.hpp"

using namespace shadowfill;

namespace {
Event add(std::uint64_t seq, std::uint64_t oid, std::int64_t px,
          std::int64_t sz, Side side) {
  return Event{static_cast<std::int64_t>(seq) * 1000, seq, oid, px, sz,
               EventType::Add, side};
}
}  // namespace

TEST_CASE("add accumulates level size") {
  OrderBook b;
  b.apply(add(0, 1, 100, 10, Side::Bid));
  b.apply(add(1, 2, 100, 5, Side::Bid));
  REQUIRE(b.level_size(Side::Bid, 100) == 15);
  REQUIRE(b.best_bid() == 100);
}

TEST_CASE("delete removes order and empties the level") {
  OrderBook b;
  b.apply(add(0, 1, 100, 10, Side::Bid));
  b.apply(Event{1000, 1, 1, 100, 10, EventType::Delete, Side::Bid});
  REQUIRE(b.level_size(Side::Bid, 100) == 0);
  REQUIRE(b.find(1) == nullptr);
  REQUIRE_FALSE(b.best_bid().has_value());
}

TEST_CASE("partial cancel keeps the order alive") {
  OrderBook b;
  b.apply(add(0, 1, 100, 10, Side::Bid));
  b.apply(Event{1000, 1, 1, 100, 4, EventType::CancelPartial, Side::Bid});
  REQUIRE(b.level_size(Side::Bid, 100) == 6);
  REQUIRE(b.find(1)->size == 6);
}

TEST_CASE("hidden execution leaves the visible book untouched") {
  OrderBook b;
  b.apply(add(0, 1, 100, 10, Side::Ask));
  b.apply(Event{1000, 1, 0, 100, 7, EventType::ExecuteHidden, Side::Ask});
  REQUIRE(b.level_size(Side::Ask, 100) == 10);
}

TEST_CASE("unknown order ids still reduce the level and are counted") {
  OrderBook b;
  b.apply(add(0, 1, 100, 10, Side::Bid));
  b.apply(Event{1000, 1, 999, 100, 4, EventType::Execute, Side::Bid});
  REQUIRE(b.level_size(Side::Bid, 100) == 6);
  REQUIRE(b.unknown_order_events() == 1);
}

TEST_CASE("best prices pick the correct extremes") {
  OrderBook b;
  b.apply(add(0, 1, 100, 10, Side::Bid));
  b.apply(add(1, 2, 101, 10, Side::Bid));
  b.apply(add(2, 3, 105, 10, Side::Ask));
  b.apply(add(3, 4, 104, 10, Side::Ask));
  REQUIRE(b.best_bid() == 101);
  REQUIRE(b.best_ask() == 104);
}

TEST_CASE("arrival sequence is retained for priority comparisons") {
  OrderBook b;
  b.apply(add(0, 1, 100, 10, Side::Bid));
  b.apply(add(7, 2, 100, 10, Side::Bid));
  REQUIRE(b.find(1)->seq == 0);
  REQUIRE(b.find(2)->seq == 7);
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `make test-cpp`
Expected: FAIL at compile time with `fatal error: shadowfill/book.hpp: No such file or directory`

- [ ] **Step 3: Write minimal implementation**

`src/shadowfill_core/include/shadowfill/event.hpp`:
```cpp
#pragma once
#include <cstdint>

namespace shadowfill {

enum class EventType : std::uint8_t {
  Add = 1,
  CancelPartial = 2,
  Delete = 3,
  Execute = 4,
  ExecuteHidden = 5,
  Cross = 6,
  Halt = 7,
};

enum class Side : std::int8_t { Bid = 1, Ask = -1 };

struct Event {
  std::int64_t ts_ns;
  std::uint64_t seq;
  std::uint64_t order_id;
  std::int64_t price;
  std::int64_t size;
  EventType type;
  Side side;
};

constexpr bool consumes_visible_queue(EventType t) noexcept {
  return t == EventType::CancelPartial || t == EventType::Delete ||
         t == EventType::Execute;
}

}  // namespace shadowfill
```

`src/shadowfill_core/include/shadowfill/book.hpp`:
```cpp
#pragma once
#include <cstdint>
#include <functional>
#include <map>
#include <optional>
#include <unordered_map>

#include "shadowfill/event.hpp"

namespace shadowfill {

class OrderBook {
 public:
  struct Resting {
    std::int64_t price;
    std::int64_t size;
    std::uint64_t seq;
    Side side;
  };

  void apply(const Event& ev);

  [[nodiscard]] const Resting* find(std::uint64_t order_id) const;
  [[nodiscard]] std::int64_t level_size(Side side, std::int64_t price) const;
  [[nodiscard]] std::optional<std::int64_t> best_bid() const;
  [[nodiscard]] std::optional<std::int64_t> best_ask() const;
  [[nodiscard]] std::uint64_t unknown_order_events() const noexcept {
    return unknown_order_events_;
  }

 private:
  void reduce_level(Side side, std::int64_t price, std::int64_t qty);

  std::unordered_map<std::uint64_t, Resting> orders_;
  std::map<std::int64_t, std::int64_t, std::greater<>> bids_;
  std::map<std::int64_t, std::int64_t> asks_;
  std::uint64_t unknown_order_events_ = 0;
};

}  // namespace shadowfill
```

`src/shadowfill_core/book.cpp`:
```cpp
#include "shadowfill/book.hpp"

namespace shadowfill {

void OrderBook::reduce_level(Side side, std::int64_t price, std::int64_t qty) {
  if (side == Side::Bid) {
    auto it = bids_.find(price);
    if (it == bids_.end()) return;
    it->second -= qty;
    if (it->second <= 0) bids_.erase(it);
  } else {
    auto it = asks_.find(price);
    if (it == asks_.end()) return;
    it->second -= qty;
    if (it->second <= 0) asks_.erase(it);
  }
}

void OrderBook::apply(const Event& ev) {
  if (ev.type == EventType::Add) {
    orders_[ev.order_id] = Resting{ev.price, ev.size, ev.seq, ev.side};
    if (ev.side == Side::Bid) {
      bids_[ev.price] += ev.size;
    } else {
      asks_[ev.price] += ev.size;
    }
    return;
  }

  if (!consumes_visible_queue(ev.type)) return;

  auto it = orders_.find(ev.order_id);
  if (it == orders_.end()) {
    ++unknown_order_events_;
    reduce_level(ev.side, ev.price, ev.size);
    return;
  }

  it->second.size -= ev.size;
  reduce_level(it->second.side, it->second.price, ev.size);
  if (it->second.size <= 0) orders_.erase(it);
}

const OrderBook::Resting* OrderBook::find(std::uint64_t order_id) const {
  auto it = orders_.find(order_id);
  return it == orders_.end() ? nullptr : &it->second;
}

std::int64_t OrderBook::level_size(Side side, std::int64_t price) const {
  if (side == Side::Bid) {
    auto it = bids_.find(price);
    return it == bids_.end() ? 0 : it->second;
  }
  auto it = asks_.find(price);
  return it == asks_.end() ? 0 : it->second;
}

std::optional<std::int64_t> OrderBook::best_bid() const {
  if (bids_.empty()) return std::nullopt;
  return bids_.begin()->first;
}

std::optional<std::int64_t> OrderBook::best_ask() const {
  if (asks_.empty()) return std::nullopt;
  return asks_.begin()->first;
}

}  // namespace shadowfill
```

- [ ] **Step 4: Run test to verify it passes**

Run: `make test-cpp`
Expected: PASS, `All tests passed (16 assertions in 6 test cases)` (assertion count may differ)

- [ ] **Step 5: Commit**

```bash
git add src/shadowfill_core/include/shadowfill/event.hpp src/shadowfill_core/include/shadowfill/book.hpp src/shadowfill_core/book.cpp tests/cpp/test_book.cpp
git commit -m "feat(cpp): order-by-order book reconstruction"
```

---

## Task 10: C++ shadow tracker

**Files:**
- Create: `src/shadowfill_core/include/shadowfill/shadow.hpp`, `src/shadowfill_core/shadow.cpp`
- Test: `tests/cpp/test_shadow.cpp`

**Context for the engineer:** Port `RefShadowTracker` verbatim, including the four-step event ordering. `Status`, `Placement` and `Outcome` field names must match the Python versions exactly — Task 11 compares them field by field.

- [ ] **Step 1: Write the failing test**

`tests/cpp/test_shadow.cpp`:
```cpp
#include <catch2/catch_test_macros.hpp>
#include <vector>

#include "shadowfill/shadow.hpp"

using namespace shadowfill;

namespace {
constexpr std::int64_t kSec = 1'000'000'000;

Placement place(std::int64_t ts, std::int64_t size = 10,
                std::int64_t latency = 0, std::int64_t horizon = 10 * kSec) {
  return Placement{0, ts, latency, Side::Bid, 100, size, horizon};
}

std::vector<Outcome> run(const std::vector<Event>& events,
                         const std::vector<Placement>& placements) {
  ShadowTracker tracker(placements);
  for (const auto& ev : events) tracker.on_event(ev);
  tracker.finalize();
  return tracker.outcomes();
}
}  // namespace

TEST_CASE("ahead at insert equals resting volume at the price") {
  std::vector<Event> events{
      {0, 0, 1, 100, 30, EventType::Add, Side::Bid},
      {kSec, 1, 2, 100, 20, EventType::Add, Side::Bid},
      {2 * kSec, 2, 3, 100, 5, EventType::Add, Side::Bid},
  };
  auto out = run(events, {place(kSec + 1)});
  REQUIRE(out[0].ahead_at_insert == 50);
  REQUIRE(out[0].insert_seq == 2);
}

TEST_CASE("orders arriving after insertion are behind the shadow") {
  std::vector<Event> events{
      {0, 0, 1, 100, 30, EventType::Add, Side::Bid},
      {kSec, 1, 2, 100, 999, EventType::Add, Side::Bid},
      {2 * kSec, 2, 1, 100, 30, EventType::Execute, Side::Bid},
      {3 * kSec, 3, 3, 100, 10, EventType::Execute, Side::Bid},
  };
  auto out = run(events, {place(0)});
  REQUIRE(out[0].status == Status::Filled);
  REQUIRE(out[0].first_fill_ts == 3 * kSec);
}

TEST_CASE("cancellation ahead shrinks the queue, behind does not") {
  std::vector<Event> events{
      {0, 0, 1, 100, 30, EventType::Add, Side::Bid},
      {kSec, 1, 2, 100, 40, EventType::Add, Side::Bid},
      {2 * kSec, 2, 2, 100, 40, EventType::Delete, Side::Bid},
      {3 * kSec, 3, 1, 100, 25, EventType::CancelPartial, Side::Bid},
  };
  auto out = run(events, {place(500'000'000)});
  REQUIRE(out[0].ahead_at_insert == 30);
  REQUIRE(out[0].ahead_at_end == 5);
}

TEST_CASE("hidden executions never consume the queue") {
  std::vector<Event> events{
      {0, 0, 1, 100, 10, EventType::Add, Side::Bid},
      {kSec, 1, 0, 100, 10, EventType::ExecuteHidden, Side::Bid},
      {2 * kSec, 2, 0, 100, 10, EventType::ExecuteHidden, Side::Bid},
  };
  auto out = run(events, {place(0)});
  REQUIRE(out[0].ahead_at_end == 10);
  REQUIRE(out[0].status == Status::Expired);
}

TEST_CASE("partial then full fill records both timestamps") {
  std::vector<Event> events{
      {0, 0, 1, 100, 5, EventType::Add, Side::Bid},
      {kSec, 1, 1, 100, 5, EventType::Execute, Side::Bid},
      {2 * kSec, 2, 2, 100, 3, EventType::Execute, Side::Bid},
      {3 * kSec, 3, 3, 100, 7, EventType::Execute, Side::Bid},
  };
  auto out = run(events, {place(0, 10)});
  REQUIRE(out[0].first_fill_ts == 2 * kSec);
  REQUIRE(out[0].full_fill_ts == 3 * kSec);
  REQUIRE(out[0].filled_qty == 10);
}

TEST_CASE("latency puts intervening orders ahead") {
  std::vector<Event> events{
      {0, 0, 1, 100, 10, EventType::Add, Side::Bid},
      {kSec, 1, 2, 100, 40, EventType::Add, Side::Bid},
      {3 * kSec, 2, 1, 100, 10, EventType::Execute, Side::Bid},
  };
  REQUIRE(run(events, {place(0, 10, 0)})[0].ahead_at_insert == 10);
  REQUIRE(run(events, {place(0, 10, 2 * kSec)})[0].ahead_at_insert == 50);
}

TEST_CASE("horizon expiry beats a later fill") {
  std::vector<Event> events{
      {0, 0, 1, 100, 10, EventType::Add, Side::Bid},
      {5 * kSec, 1, 1, 100, 10, EventType::Execute, Side::Bid},
  };
  auto out = run(events, {place(0, 10, 0, 2 * kSec)});
  REQUIRE(out[0].status == Status::Expired);
  REQUIRE(out[0].first_fill_ts == -1);
}

TEST_CASE("still resting at end of stream is truncated") {
  std::vector<Event> events{{0, 0, 1, 100, 10, EventType::Add, Side::Bid}};
  REQUIRE(run(events, {place(0)})[0].status == Status::Truncated);
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `make test-cpp`
Expected: FAIL at compile time with `fatal error: shadowfill/shadow.hpp: No such file or directory`

- [ ] **Step 3: Write minimal implementation**

`src/shadowfill_core/include/shadowfill/shadow.hpp`:
```cpp
#pragma once
#include <cstdint>
#include <vector>

#include "shadowfill/book.hpp"
#include "shadowfill/event.hpp"

namespace shadowfill {

enum class Status : std::uint8_t {
  Resting = 0,
  Filled = 1,
  Expired = 2,
  Truncated = 3,
};

struct Placement {
  std::uint64_t shadow_id;
  std::int64_t ts_ns;
  std::int64_t latency_ns;
  Side side;
  std::int64_t price;
  std::int64_t size;
  std::int64_t horizon_ns;

  [[nodiscard]] std::int64_t effective_ts() const noexcept {
    return ts_ns + latency_ns;
  }
};

struct Outcome {
  std::uint64_t shadow_id = 0;
  Status status = Status::Resting;
  std::int64_t insert_ts = -1;
  std::int64_t insert_seq = -1;
  std::int64_t ahead_at_insert = -1;
  std::int64_t ahead_at_end = -1;
  std::int64_t first_fill_ts = -1;
  std::int64_t full_fill_ts = -1;
  std::int64_t filled_qty = 0;
};

class ShadowTracker {
 public:
  explicit ShadowTracker(std::vector<Placement> placements);

  void on_event(const Event& ev);
  void finalize();

  /// Outcomes ordered by shadow_id. Valid only after finalize().
  [[nodiscard]] const std::vector<Outcome>& outcomes() const noexcept {
    return outcomes_;
  }
  [[nodiscard]] std::uint64_t unknown_order_assumed_ahead() const noexcept {
    return unknown_order_assumed_ahead_;
  }
  [[nodiscard]] std::uint64_t fifo_violations() const noexcept {
    return fifo_violations_;
  }
  [[nodiscard]] const OrderBook& book() const noexcept { return book_; }

 private:
  struct Active {
    Placement placement;
    Outcome outcome;
    std::int64_t ahead;
    std::int64_t expiry_ts;
  };

  void expire(std::int64_t now_ts);
  void activate(std::int64_t now_ts, std::uint64_t now_seq);
  void match(const Event& ev);
  void harvest_filled();
  bool is_ahead(std::uint64_t order_id, std::int64_t insert_seq);
  void settle(Outcome outcome);

  OrderBook book_;
  std::vector<Placement> pending_;
  std::size_t next_ = 0;
  std::vector<Active> active_;
  std::vector<Outcome> outcomes_;
  std::uint64_t unknown_order_assumed_ahead_ = 0;
  std::uint64_t fifo_violations_ = 0;
};

}  // namespace shadowfill
```

`src/shadowfill_core/shadow.cpp`:
```cpp
#include "shadowfill/shadow.hpp"

#include <algorithm>

namespace shadowfill {

ShadowTracker::ShadowTracker(std::vector<Placement> placements)
    : pending_(std::move(placements)) {
  std::sort(pending_.begin(), pending_.end(),
            [](const Placement& a, const Placement& b) {
              if (a.effective_ts() != b.effective_ts())
                return a.effective_ts() < b.effective_ts();
              return a.shadow_id < b.shadow_id;
            });
  outcomes_.reserve(pending_.size());
}

void ShadowTracker::settle(Outcome outcome) { outcomes_.push_back(outcome); }

void ShadowTracker::expire(std::int64_t now_ts) {
  auto it = std::stable_partition(
      active_.begin(), active_.end(),
      [now_ts](const Active& a) { return a.expiry_ts >= now_ts; });
  for (auto e = it; e != active_.end(); ++e) {
    e->outcome.status = Status::Expired;
    e->outcome.ahead_at_end = e->ahead;
    settle(e->outcome);
  }
  active_.erase(it, active_.end());
}

void ShadowTracker::activate(std::int64_t now_ts, std::uint64_t now_seq) {
  while (next_ < pending_.size() &&
         pending_[next_].effective_ts() <= now_ts) {
    const Placement& p = pending_[next_];
    Outcome o;
    o.shadow_id = p.shadow_id;
    o.status = Status::Resting;
    o.insert_ts = now_ts;
    o.insert_seq = static_cast<std::int64_t>(now_seq);
    o.ahead_at_insert = book_.level_size(p.side, p.price);
    active_.push_back(Active{p, o, o.ahead_at_insert, now_ts + p.horizon_ns});
    ++next_;
  }
}

bool ShadowTracker::is_ahead(std::uint64_t order_id, std::int64_t insert_seq) {
  const auto* order = book_.find(order_id);
  if (order == nullptr) {
    ++unknown_order_assumed_ahead_;
    return true;
  }
  return static_cast<std::int64_t>(order->seq) < insert_seq;
}

void ShadowTracker::match(const Event& ev) {
  if (!consumes_visible_queue(ev.type)) return;

  for (Active& a : active_) {
    if (a.placement.side != ev.side || a.placement.price != ev.price) continue;

    if (ev.type != EventType::Execute) {
      if (is_ahead(ev.order_id, a.outcome.insert_seq)) {
        a.ahead = std::max<std::int64_t>(0, a.ahead - ev.size);
      }
      continue;
    }

    if (a.ahead == 0 && !is_ahead(ev.order_id, a.outcome.insert_seq)) {
      ++fifo_violations_;
    }
    const std::int64_t consumed = std::min(ev.size, a.ahead);
    a.ahead -= consumed;
    const std::int64_t residual = ev.size - consumed;
    if (residual <= 0) continue;

    const std::int64_t remaining = a.placement.size - a.outcome.filled_qty;
    const std::int64_t fill = std::min(residual, remaining);
    if (fill <= 0) continue;
    if (a.outcome.filled_qty == 0) a.outcome.first_fill_ts = ev.ts_ns;
    a.outcome.filled_qty += fill;
    if (a.outcome.filled_qty >= a.placement.size) {
      a.outcome.full_fill_ts = ev.ts_ns;
      a.outcome.status = Status::Filled;
      a.outcome.ahead_at_end = a.ahead;
    }
  }
}

void ShadowTracker::harvest_filled() {
  auto it = std::stable_partition(
      active_.begin(), active_.end(),
      [](const Active& a) { return a.outcome.status != Status::Filled; });
  for (auto e = it; e != active_.end(); ++e) settle(e->outcome);
  active_.erase(it, active_.end());
}

void ShadowTracker::on_event(const Event& ev) {
  expire(ev.ts_ns);
  activate(ev.ts_ns, ev.seq);
  match(ev);
  harvest_filled();
  book_.apply(ev);
}

void ShadowTracker::finalize() {
  for (Active& a : active_) {
    a.outcome.status = Status::Truncated;
    a.outcome.ahead_at_end = a.ahead;
    settle(a.outcome);
  }
  active_.clear();
  while (next_ < pending_.size()) {
    Outcome o;
    o.shadow_id = pending_[next_].shadow_id;
    o.status = Status::Truncated;
    settle(o);
    ++next_;
  }
  std::sort(outcomes_.begin(), outcomes_.end(),
            [](const Outcome& a, const Outcome& b) {
              return a.shadow_id < b.shadow_id;
            });
}

}  // namespace shadowfill
```

- [ ] **Step 4: Run test to verify it passes**

Run: `make test-cpp`
Expected: PASS, 14 test cases passing

- [ ] **Step 5: Commit**

```bash
git add src/shadowfill_core/include/shadowfill/shadow.hpp src/shadowfill_core/shadow.cpp tests/cpp/test_shadow.cpp
git commit -m "feat(cpp): never-cancel shadow-order tracker"
```

---

## Task 11: pybind11 bindings and Python↔C++ equivalence

**Files:**
- Create: `src/shadowfill_bindings/module.cpp`
- Test: `tests/python/test_cpp_equivalence.py`

**Context for the engineer:** The binding takes seven parallel 1-D NumPy arrays rather than a structured array, which avoids depending on NumPy struct padding matching the C++ layout. The whole replay happens inside one call — never call across the language boundary per event.

- [ ] **Step 1: Write the failing test**

`tests/python/test_cpp_equivalence.py`:
```python
import numpy as np
import pytest

from shadowfill.replay import Status, run_reference
from shadowfill.synthetic import generate_synthetic_messages

from .test_invariants import grid_placements

core = pytest.importorskip("shadowfill._core")


def run_core(events, placements):
    return core.replay(
        events["ts_ns"], events["seq"], events["order_id"], events["price"],
        events["size"], events["type"], events["side"],
        np.array([p.shadow_id for p in placements], dtype=np.uint64),
        np.array([p.ts_ns for p in placements], dtype=np.int64),
        np.array([p.latency_ns for p in placements], dtype=np.int64),
        np.array([p.side for p in placements], dtype=np.int8),
        np.array([p.price for p in placements], dtype=np.int64),
        np.array([p.size for p in placements], dtype=np.int64),
        np.array([p.horizon_ns for p in placements], dtype=np.int64),
    )


@pytest.mark.parametrize("seed", [1, 2, 3, 17, 99])
def test_cpp_matches_python_reference_field_by_field(seed):
    events = generate_synthetic_messages(n_events=40_000, seed=seed)
    placements = grid_placements(events, every=250)
    assert placements

    expected = run_reference(events, placements)
    actual = run_core(events, placements)

    assert len(actual["shadow_id"]) == len(expected)
    for field in (
        "status", "insert_ts", "insert_seq", "ahead_at_insert",
        "ahead_at_end", "first_fill_ts", "full_fill_ts", "filled_qty",
    ):
        np.testing.assert_array_equal(
            actual[field],
            np.array([getattr(o, field) for o in expected]),
            err_msg=f"mismatch in field {field} for seed {seed}",
        )


def test_cpp_reports_the_same_diagnostics():
    events = generate_synthetic_messages(n_events=40_000, seed=5)
    placements = grid_placements(events, every=250)
    actual = run_core(events, placements)
    assert actual["fifo_violations"] == 0
    assert actual["unknown_order_assumed_ahead"] >= 0


def test_cpp_produces_some_fills_so_the_comparison_is_meaningful():
    events = generate_synthetic_messages(n_events=40_000, seed=2)
    placements = grid_placements(events, every=250)
    actual = run_core(events, placements)
    assert (actual["status"] == int(Status.FILLED)).sum() > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pip install -e ".[dev]" && pytest tests/python/test_cpp_equivalence.py -v`
Expected: FAIL/SKIP with `Could not import 'shadowfill._core'`

- [ ] **Step 3: Write minimal implementation**

`src/shadowfill_bindings/module.cpp`:
```cpp
#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>

#include <stdexcept>
#include <vector>

#include "shadowfill/shadow.hpp"

namespace py = pybind11;
using namespace shadowfill;

namespace {

template <typename T>
using Arr = py::array_t<T, py::array::c_style | py::array::forcecast>;

py::dict replay(Arr<std::int64_t> ts_ns, Arr<std::uint64_t> seq,
                Arr<std::uint64_t> order_id, Arr<std::int64_t> price,
                Arr<std::int64_t> size, Arr<std::uint8_t> type,
                Arr<std::int8_t> side, Arr<std::uint64_t> p_shadow_id,
                Arr<std::int64_t> p_ts_ns, Arr<std::int64_t> p_latency_ns,
                Arr<std::int8_t> p_side, Arr<std::int64_t> p_price,
                Arr<std::int64_t> p_size, Arr<std::int64_t> p_horizon_ns) {
  const auto n = static_cast<std::size_t>(ts_ns.size());
  for (const auto& sz : {seq.size(), order_id.size(), price.size(),
                         size.size(), type.size(), side.size()}) {
    if (static_cast<std::size_t>(sz) != n) {
      throw std::invalid_argument("event arrays have mismatched lengths");
    }
  }
  const auto m = static_cast<std::size_t>(p_shadow_id.size());
  for (const auto& sz : {p_ts_ns.size(), p_latency_ns.size(), p_side.size(),
                         p_price.size(), p_size.size(), p_horizon_ns.size()}) {
    if (static_cast<std::size_t>(sz) != m) {
      throw std::invalid_argument("placement arrays have mismatched lengths");
    }
  }

  std::vector<Placement> placements;
  placements.reserve(m);
  for (std::size_t i = 0; i < m; ++i) {
    placements.push_back(Placement{
        p_shadow_id.at(i), p_ts_ns.at(i), p_latency_ns.at(i),
        static_cast<Side>(p_side.at(i)), p_price.at(i), p_size.at(i),
        p_horizon_ns.at(i)});
  }

  ShadowTracker tracker(std::move(placements));
  {
    py::gil_scoped_release release;
    auto t = ts_ns.unchecked<1>();
    auto s = seq.unchecked<1>();
    auto o = order_id.unchecked<1>();
    auto pr = price.unchecked<1>();
    auto sz = size.unchecked<1>();
    auto ty = type.unchecked<1>();
    auto sd = side.unchecked<1>();
    for (std::size_t i = 0; i < n; ++i) {
      tracker.on_event(Event{t(i), s(i), o(i), pr(i), sz(i),
                             static_cast<EventType>(ty(i)),
                             static_cast<Side>(sd(i))});
    }
    tracker.finalize();
  }

  const auto& outcomes = tracker.outcomes();
  const auto k = outcomes.size();
  auto make_i64 = [&](auto getter) {
    py::array_t<std::int64_t> out(static_cast<py::ssize_t>(k));
    auto view = out.mutable_unchecked<1>();
    for (std::size_t i = 0; i < k; ++i) view(i) = getter(outcomes[i]);
    return out;
  };

  py::array_t<std::uint64_t> ids(static_cast<py::ssize_t>(k));
  {
    auto view = ids.mutable_unchecked<1>();
    for (std::size_t i = 0; i < k; ++i) view(i) = outcomes[i].shadow_id;
  }

  py::dict result;
  result["shadow_id"] = ids;
  result["status"] = make_i64([](const Outcome& o) {
    return static_cast<std::int64_t>(o.status);
  });
  result["insert_ts"] = make_i64([](const Outcome& o) { return o.insert_ts; });
  result["insert_seq"] = make_i64([](const Outcome& o) { return o.insert_seq; });
  result["ahead_at_insert"] =
      make_i64([](const Outcome& o) { return o.ahead_at_insert; });
  result["ahead_at_end"] =
      make_i64([](const Outcome& o) { return o.ahead_at_end; });
  result["first_fill_ts"] =
      make_i64([](const Outcome& o) { return o.first_fill_ts; });
  result["full_fill_ts"] =
      make_i64([](const Outcome& o) { return o.full_fill_ts; });
  result["filled_qty"] = make_i64([](const Outcome& o) { return o.filled_qty; });
  result["unknown_order_assumed_ahead"] = tracker.unknown_order_assumed_ahead();
  result["fifo_violations"] = tracker.fifo_violations();
  result["unknown_order_events"] = tracker.book().unknown_order_events();
  return result;
}

}  // namespace

PYBIND11_MODULE(_core, m) {
  m.doc() = "ShadowFill C++ replay engine";
  m.def("replay", &replay, "Replay MBO events and evaluate shadow placements",
        py::arg("ts_ns"), py::arg("seq"), py::arg("order_id"), py::arg("price"),
        py::arg("size"), py::arg("type"), py::arg("side"),
        py::arg("p_shadow_id"), py::arg("p_ts_ns"), py::arg("p_latency_ns"),
        py::arg("p_side"), py::arg("p_price"), py::arg("p_size"),
        py::arg("p_horizon_ns"));
}
```

Also add `tests/python/__init__.py` (empty file) so `from .test_invariants import grid_placements` resolves:
```bash
touch tests/python/__init__.py
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pip install -e ".[dev]" --force-reinstall --no-deps && pytest tests/python/test_cpp_equivalence.py -v`
Expected: PASS, 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/shadowfill_bindings/module.cpp tests/python/test_cpp_equivalence.py tests/python/__init__.py
git commit -m "feat: pybind11 bindings with Python/C++ equivalence tests"
```

---

## Task 12: Placement grid, ground-truth output, benchmark, docs

**Files:**
- Create: `python/shadowfill/placements.py`, `python/shadowfill/ground_truth.py`, `benchmarks/bench_replay.py`, `configs/ground_truth_synthetic.yaml`, `README.md`
- Modify: `tests/python/test_invariants.py:grid_placements` → import from `shadowfill.placements`
- Test: `tests/python/test_ground_truth.py`

**Context for the engineer:** The run manifest is what makes results reproducible: it pins the git SHA, the SHA-256 of the input data, and the full config. Any figure in the paper must be traceable to one manifest.

- [ ] **Step 1: Write the failing test**

`tests/python/test_ground_truth.py`:
```python
import json

import pyarrow.parquet as pq
import pytest

from shadowfill.ground_truth import run_ground_truth
from shadowfill.placements import place_top_of_book_grid
from shadowfill.synthetic import generate_synthetic_messages, write_synthetic_csv

SEC = 1_000_000_000


def test_grid_places_one_shadow_per_side_per_grid_point():
    events = generate_synthetic_messages(n_events=20_000, seed=6)
    placements = place_top_of_book_grid(
        events, grid_ns=50 * 1_000_000, size=5, horizon_ns=2 * SEC, latency_ns=0
    )
    assert placements
    assert len({p.shadow_id for p in placements}) == len(placements)
    assert {p.side for p in placements} == {1, -1}
    assert all(p.horizon_ns == 2 * SEC for p in placements)


def test_grid_ids_are_contiguous_from_zero():
    events = generate_synthetic_messages(n_events=20_000, seed=6)
    placements = place_top_of_book_grid(
        events, grid_ns=50 * 1_000_000, size=5, horizon_ns=2 * SEC, latency_ns=0
    )
    assert sorted(p.shadow_id for p in placements) == list(range(len(placements)))


def test_run_writes_parquet_and_manifest(tmp_path):
    events = generate_synthetic_messages(n_events=30_000, seed=8)
    data_path = tmp_path / "synthetic_message_10.csv"
    write_synthetic_csv(events, data_path)
    out_dir = tmp_path / "out"

    manifest = run_ground_truth(
        message_path=data_path,
        out_dir=out_dir,
        grid_ns=50 * 1_000_000,
        size=5,
        horizon_ns=2 * SEC,
        latency_ns=0,
        engine="python",
    )

    table = pq.read_table(out_dir / "outcomes.parquet")
    assert table.num_rows == manifest["n_placements"]
    assert set(table.column_names) >= {
        "shadow_id", "status", "insert_ts", "insert_seq",
        "ahead_at_insert", "ahead_at_end", "first_fill_ts",
        "full_fill_ts", "filled_qty",
    }

    on_disk = json.loads((out_dir / "manifest.json").read_text())
    assert on_disk["input_sha256"] == manifest["input_sha256"]
    assert len(on_disk["input_sha256"]) == 64
    assert on_disk["config"]["grid_ns"] == 50 * 1_000_000


def test_python_and_cpp_engines_write_identical_outcomes(tmp_path):
    pytest.importorskip("shadowfill._core")
    events = generate_synthetic_messages(n_events=30_000, seed=12)
    data_path = tmp_path / "synthetic_message_10.csv"
    write_synthetic_csv(events, data_path)

    kwargs = dict(
        message_path=data_path, grid_ns=50 * 1_000_000, size=5,
        horizon_ns=2 * SEC, latency_ns=0,
    )
    run_ground_truth(out_dir=tmp_path / "py", engine="python", **kwargs)
    run_ground_truth(out_dir=tmp_path / "cpp", engine="cpp", **kwargs)

    py_table = pq.read_table(tmp_path / "py" / "outcomes.parquet")
    cpp_table = pq.read_table(tmp_path / "cpp" / "outcomes.parquet")
    assert py_table.equals(cpp_table)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/python/test_ground_truth.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'shadowfill.placements'`

- [ ] **Step 3: Write minimal implementation**

`python/shadowfill/placements.py`:
```python
"""Placement schedules for shadow orders."""

from __future__ import annotations

import numpy as np

from .events import Side
from .replay import Placement, RefBook


def place_top_of_book_grid(
    events: np.ndarray,
    *,
    grid_ns: int,
    size: int,
    horizon_ns: int,
    latency_ns: int,
) -> list[Placement]:
    """One shadow per side at each grid time, at the prevailing best price.

    Top-of-book only: deeper levels can leave the recorded level band, which
    would silently truncate the event stream feeding the queue arithmetic.
    """
    book = RefBook()
    placements: list[Placement] = []
    shadow_id = 0
    next_grid: int | None = None

    for e in events:
        ts = int(e["ts_ns"])
        if next_grid is None:
            next_grid = ts
        while next_grid is not None and ts >= next_grid:
            for side, price in (
                (Side.BID, book.best_bid()),
                (Side.ASK, book.best_ask()),
            ):
                if price is None:
                    continue
                placements.append(
                    Placement(
                        shadow_id=shadow_id,
                        ts_ns=next_grid,
                        latency_ns=latency_ns,
                        side=int(side),
                        price=price,
                        size=size,
                        horizon_ns=horizon_ns,
                    )
                )
                shadow_id += 1
            next_grid += grid_ns

        book.apply(
            ts, int(e["seq"]), int(e["order_id"]), int(e["price"]),
            int(e["size"]), int(e["type"]), int(e["side"]),
        )

    return placements
```

`python/shadowfill/ground_truth.py`:
```python
"""Run the ground-truth engine end to end and persist reproducible output."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from .lobster import load_lobster_messages
from .placements import place_top_of_book_grid
from .replay import Placement, run_reference

OUTCOME_FIELDS = (
    "shadow_id", "status", "insert_ts", "insert_seq", "ahead_at_insert",
    "ahead_at_end", "first_fill_ts", "full_fill_ts", "filled_qty",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def _run_cpp(events: np.ndarray, placements: list[Placement]) -> dict[str, Any]:
    from shadowfill import _core

    return _core.replay(
        events["ts_ns"], events["seq"], events["order_id"], events["price"],
        events["size"], events["type"], events["side"],
        np.array([p.shadow_id for p in placements], dtype=np.uint64),
        np.array([p.ts_ns for p in placements], dtype=np.int64),
        np.array([p.latency_ns for p in placements], dtype=np.int64),
        np.array([p.side for p in placements], dtype=np.int8),
        np.array([p.price for p in placements], dtype=np.int64),
        np.array([p.size for p in placements], dtype=np.int64),
        np.array([p.horizon_ns for p in placements], dtype=np.int64),
    )


def run_ground_truth(
    *,
    message_path: str | Path,
    out_dir: str | Path,
    grid_ns: int,
    size: int,
    horizon_ns: int,
    latency_ns: int,
    engine: str = "cpp",
) -> dict[str, Any]:
    """Compute never-cancel ground truth and write outcomes.parquet + manifest.json."""
    message_path = Path(message_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    events = load_lobster_messages(message_path)
    placements = place_top_of_book_grid(
        events, grid_ns=grid_ns, size=size,
        horizon_ns=horizon_ns, latency_ns=latency_ns,
    )

    if engine == "cpp":
        result = _run_cpp(events, placements)
        columns = {f: np.asarray(result[f], dtype="int64") for f in OUTCOME_FIELDS}
        diagnostics = {
            "unknown_order_assumed_ahead": int(result["unknown_order_assumed_ahead"]),
            "fifo_violations": int(result["fifo_violations"]),
            "unknown_order_events": int(result["unknown_order_events"]),
        }
    elif engine == "python":
        outcomes = run_reference(events, placements)
        columns = {
            f: np.array([getattr(o, f) for o in outcomes], dtype="int64")
            for f in OUTCOME_FIELDS
        }
        diagnostics = {}
    else:
        raise ValueError(f"unknown engine: {engine!r}")

    pq.write_table(pa.table(columns), out_dir / "outcomes.parquet")

    manifest = {
        "git_sha": _git_sha(),
        "input_path": str(message_path),
        "input_sha256": _sha256(message_path),
        "n_events": int(len(events)),
        "n_placements": len(placements),
        "engine": engine,
        "diagnostics": diagnostics,
        "config": {
            "grid_ns": grid_ns,
            "size": size,
            "horizon_ns": horizon_ns,
            "latency_ns": latency_ns,
        },
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Compute shadow-order ground truth")
    parser.add_argument("--message-path", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--grid-ns", type=int, default=100_000_000)
    parser.add_argument("--size", type=int, default=100)
    parser.add_argument("--horizon-ns", type=int, default=60_000_000_000)
    parser.add_argument("--latency-ns", type=int, default=0)
    parser.add_argument("--engine", choices=["cpp", "python"], default="cpp")
    args = parser.parse_args()

    manifest = run_ground_truth(
        message_path=args.message_path,
        out_dir=args.out_dir,
        grid_ns=args.grid_ns,
        size=args.size,
        horizon_ns=args.horizon_ns,
        latency_ns=args.latency_ns,
        engine=args.engine,
    )
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
```

`benchmarks/bench_replay.py`:
```python
"""Throughput benchmark. Fails the build if the C++ engine regresses."""

from __future__ import annotations

import sys
import time

from shadowfill import _core
from shadowfill.ground_truth import _run_cpp
from shadowfill.placements import place_top_of_book_grid
from shadowfill.synthetic import generate_synthetic_messages

MIN_EVENTS_PER_SECOND = 2_000_000


def main() -> int:
    assert _core is not None
    events = generate_synthetic_messages(n_events=2_000_000, seed=20260919)
    placements = place_top_of_book_grid(
        events, grid_ns=10_000_000, size=100,
        horizon_ns=10_000_000_000, latency_ns=0,
    )
    print(f"events={len(events):,} placements={len(placements):,}")

    start = time.perf_counter()
    _run_cpp(events, placements)
    elapsed = time.perf_counter() - start
    rate = len(events) / elapsed
    print(f"replay: {elapsed:.3f}s -> {rate:,.0f} events/s")

    if rate < MIN_EVENTS_PER_SECOND:
        print(f"FAIL: below gate of {MIN_EVENTS_PER_SECOND:,} events/s")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

`configs/ground_truth_synthetic.yaml`:
```yaml
message_path: tests/fixtures/synthetic_mbo_v1.csv
out_dir: results/synthetic
grid_ns: 100000000       # 100 ms
size: 100
horizon_ns: 60000000000  # 60 s
latency_ns: 0
engine: cpp
```

Update `tests/python/test_invariants.py` to drop its local helper and import the real one:
```python
from shadowfill.placements import place_top_of_book_grid


def grid_placements(events, *, every=500, size=5, horizon=2 * SEC):
    """Event-count grid, expressed via the shared time-grid placer."""
    span = int(events["ts_ns"][-1]) - int(events["ts_ns"][0])
    grid_ns = max(1, span * every // max(1, len(events)))
    return place_top_of_book_grid(
        events, grid_ns=grid_ns, size=size, horizon_ns=horizon, latency_ns=0
    )
```

`README.md` (Plan 1 section only; the full paper-style README lands in Plan 4):
```markdown
# ShadowFill

Counterfactual fill estimation from market-by-order data.

## What this repository currently contains

A validated ground-truth engine: given an MBO event stream and a schedule of
hypothetical passive orders, it computes — by exact queue arithmetic, not by a
model — whether each order would have filled, when, and how much queue sat ahead
of it throughout its life.

## Quick start

    make install
    make test          # Python suite, runs with no external data
    make test-cpp      # C++ suite
    make bench         # throughput gate
    python -m shadowfill.ground_truth \
        --message-path tests/fixtures/synthetic_mbo_v1.csv \
        --out-dir results/synthetic

## Using real data

    ./scripts/fetch_lobster_sample.sh data/lobster
    SHADOWFILL_LOBSTER_DIR=data/lobster pytest -m needs_lobster -v

LOBSTER sample files are not redistributed here.

## Design notes

- Two implementations, one behaviour. `shadowfill.replay` is the readable
  oracle; `shadowfill._core` is the fast path. `tests/python/test_cpp_equivalence.py`
  holds them to identical output field by field.
- Hidden executions never consume the visible queue.
- Cancellations are resolved as ahead-or-behind by arrival sequence, which is
  only possible because MBO data carries order ids.
- Shadow accounting runs before the book applies each event, so cancelled
  orders can still be looked up.
- Every run writes `manifest.json` pinning the git SHA, input SHA-256 and config.

## Assumptions, stated plainly

The shadow order is assumed not to change other participants' behaviour. That
is defensible for small sizes and is stress-tested in a later stage of the
project; it is not free. Orders referencing ids added before the recording
window are assumed to be ahead of the shadow, and the count is reported in
every manifest.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/python -v && make bench`
Expected: full suite PASS; benchmark prints a rate and `OK`

- [ ] **Step 5: Commit**

```bash
git add python/shadowfill/placements.py python/shadowfill/ground_truth.py benchmarks/bench_replay.py configs/ground_truth_synthetic.yaml README.md tests/python/test_ground_truth.py tests/python/test_invariants.py
git commit -m "feat: placement grid, reproducible ground-truth output, benchmark gate"
```

---

## Definition of done for Plan 1

- [ ] `make test`, `make test-cpp`, `make lint`, `make bench` all pass on a clean clone with no external data
- [ ] `pytest -m needs_lobster` passes on a machine with the LOBSTER sample present
- [ ] Top-of-book reconstruction matches LOBSTER's own snapshot file on >95% of in-band rows
- [ ] C++ and Python outcomes are identical on five independent synthetic seeds
- [ ] `results/synthetic/manifest.json` exists and pins git SHA + input hash
- [ ] `fifo_violations` is 0 on synthetic data and reported (not silently dropped) on real data

---

## Plans 2–4 (not in scope here)

- **Plan 2 — Multi-venue data layer.** Coinbase L3 websocket recorder with gap detection and sequence-number validation, canonical-event adapter, Databento MBO adapter, Parquet partitioning, dataset manifests. Start the recorder first: the tape has to accumulate in wall-clock time.
- **Plan 3 — Estimators and the L2 ablation.** Kaplan–Meier, cause-specific Cox, Aalen–Johansen, Fine–Gray, IPCW policy re-targeting, copula sensitivity bounds, and a discrete-time-hazard neural baseline; plus the L2 ablation harness (cancel-from-front/back/uniform heuristics) that quantifies what order ids are worth.
- **Plan 4 — Experiments and the paper.** Markout/adverse-selection layer, bias measurement against Plan 1's ground truth, policy-ranking inversion study, impact-assumption stress tests, block-bootstrap confidence intervals, figures, and the paper-style README.

---

## Plan amendments (agreed 2026-09-19, during Task 1)

The plan as written above is superseded on the following points. Where this
section and the task bodies disagree, this section wins.

### A. Build gating (Task 1, done) and the cpp CI job (Task 9)

The C++ targets in `CMakeLists.txt` are gated behind
`option(SHADOWFILL_BUILD_CORE "Build the C++ engine" OFF)`, pinned OFF from
`[tool.scikit-build.cmake.define]`. Task 9 flips it to `"ON"` in that task's
commit. Rationale: without a gate, `pip install -e .` fails at CMake configure
from Task 1 until Task 10, because `book.cpp` and `shadow.cpp` do not yet
exist. An explicit option is preferred over `if(EXISTS ...)` because a mistyped
source path must be a loud CMake error — with filesystem probing it would yield
a wheel silently missing `shadowfill._core`, `test_cpp_equivalence.py` would
skip itself via `importorskip`, and CI would go green over an unvalidated C++
engine. `find_package(pybind11)` is `REQUIRED`, not `QUIET`, for the same
reason. The `cpp` CI job moves from Task 1 to Task 9.

### B. Anti-skip guard (Task 11)

From Task 11 onward, `conftest.py` must **fail** rather than skip when the
environment variable `SHADOWFILL_REQUIRE_CORE=1` is set, and the CI cpp job
sets it. A skipped equivalence test must never be able to pass for the wrong
reason.

### C. Event ordering inside `on_event` (Tasks 6 and 10)

Replaces the four-step ordering in Task 6. Per event `ev`, in exactly this
order:

1. **activate** every pending placement with `effective_ts < ev.ts_ns`:
   `insert_ts = placement.effective_ts` (*not* `ev.ts_ns`),
   `insert_seq = ev.seq`, `ahead = book.level_size(side, price)` read from the
   book state *before* `ev`, `expiry_ts = insert_ts + horizon_ns`
2. **expire** every active with `expiry_ts < ev.ts_ns` → `EXPIRED`
3. **match** `ev` against the remaining actives
4. **harvest** filled
5. `book.apply(ev)`

`finalize()`: remaining actives → `TRUNCATED`; never-activated pending →
`NOT_ACTIVATED` (see D).

Two things the original ordering got wrong. Activating before `book.apply` on a
timestamp tie made a placement at `ts=0` see an empty book, giving
`ahead_at_insert == 0` where the Task 6 tests assert `30`. And expiring before
activating meant a placement whose entire life falls in a quiet gap between two
events came out `TRUNCATED` instead of `EXPIRED`.

**Tie convention** (state this in the `RefShadowTracker` docstring): an order
arriving at exactly the placement's effective timestamp is **ahead** of the
shadow. Sequence numbers decide priority and the shadow has none, so we take
the pessimistic side. Never flatter the hypothetical order.

Two test corrections follow:

- `test_hidden_execution_never_consumes_the_queue` asserts `EXPIRED` but places
  a 10 s horizon against 2 s of data, which is `TRUNCATED`. Change the
  placement to `horizon=1 * SEC`; keep `ahead_at_end == 10`. Mirror in the C++
  case.
- Add a test pinning the tie convention:

```python
def test_order_added_at_exactly_the_placement_timestamp_is_ahead():
    events = make_events([
        (0, 1, 100, 30, EventType.ADD, Side.BID),
        (1 * SEC, 2, 100, 40, EventType.EXECUTE, Side.BID),
    ])
    out = run_reference(events, [place(ts=0, size=10)])[0]
    assert out.ahead_at_insert == 30
```

All remaining Task 6 and Task 10 expectations were worked through this rule by
hand and are unchanged.

### D. Fifth status: `NOT_ACTIVATED` (Tasks 6, 10, 12)

```
RESTING = 0, FILLED = 1, EXPIRED = 2, TRUNCATED = 3, NOT_ACTIVATED = 4
```

`TRUNCATED` now means only "activated, still resting when the stream ended".
`NOT_ACTIVATED` means "`effective_ts` fell past the last event". These are
different events in the world and must not share a code: a never-activated
placement is not a censored observation, it is a placement that never happened,
and it must not enter any fill-rate denominator in Plan 3. Mirror in C++.

### E. Nulls, not sentinels, in Parquet (Task 12)

`-1` stays in the in-memory `Outcome` struct, so the C++/Python equivalence
test keeps comparing raw integers. The Parquet writer converts every numeric
field to an Arrow null on rows where `status == NOT_ACTIVATED`. Nulls propagate
or raise downstream; `-1` silently regresses as a covariate.

`first_fill_ts` and `full_fill_ts` stay `-1` on activated-but-unfilled rows —
that is a real observation, not a missing one. Document the convention in the
`Outcome` docstring. Two tests to add in Task 12:
`test_not_activated_rows_are_null_not_sentinel` and
`test_no_negative_values_survive_to_parquet` (the latter over `insert_ts`,
`insert_seq`, `ahead_at_insert`, `ahead_at_end`, `filled_qty`, excluding the
two fill-timestamp columns).

### F. Per-shadow assumption accounting (Tasks 6, 10, 12)

`_is_ahead` must not increment `unknown_order_assumed_ahead` when called for
the `fifo_violations` diagnostic; split out a non-counting lookup. Note this
narrows the tracker-level counter: on the `EXECUTE` path the queue arithmetic
uses `ahead` directly and only the diagnostic looks the order up, so after the
split that counter increments on cancel/delete only. Keep it, keep reporting it
in the manifest, and name it accordingly.

Add a per-shadow column to `Outcome`:

```
assumed_ahead_events: int64  # times this shadow's `ahead` was decremented
                             # on an order id absent from the book
```

Increment only when the arithmetic actually depended on the assumption: a
cancel or delete at this shadow's price and side, unknown id, `ahead`
decremented. Never from the `fifo_violations` lookup. Counted per
(shadow, event), so one unknown order touching three actives increments three
rows. This makes "drop every shadow whose outcome depended on an unverifiable
assumption, rerun, does the result hold" a one-line filter in Plan 4 rather
than a rebuild; that robustness check is going in the paper, so the column
exists from the start.

### G. Dependency pinning (Task 3)

`pip` resolves the plan's floors to numpy 2.4.x / pandas 3.0.x / mypy 2.x.
pandas 3.0 changed string and NA handling, which is exactly what
`parse_seconds_to_ns` leans on. Add `pandas-stubs` to the dev extra, keep loose
ranges in `pyproject.toml`, and commit exact pins in `requirements-dev.lock`
from `pip freeze`. CI installs from the lock; `make install` uses
`pyproject.toml`. Do **not** cap pandas to make the stubs happy. mypy stays
`strict`; if one function still fights, scope the override to that module with
a comment explaining why — never a blanket relaxation.

### H. Environment provenance in the manifest (Task 12)

The run manifest records the resolved environment: Python version, installed
versions of numpy / pandas / pyarrow, and the compiler id and version used for
the C++ engine. A result that cannot be traced to an environment is not
reproducible, and that claim is load-bearing in this repository.

import importlib
import os
from pathlib import Path

import pytest

LOBSTER_DIR = Path(os.environ.get("SHADOWFILL_LOBSTER_DIR", "data/lobster"))
ITCH_DIR = Path(os.environ.get("SHADOWFILL_ITCH_DIR", "data/itch"))

# test_cpp_equivalence.py guards itself with importorskip, which is right for a
# local pure-Python checkout but wrong for CI: a wheel built without the
# extension module would skip the only test that validates the C++ engine and
# go green over it. Where the engine is meant to exist, its absence is an error.
if os.environ.get("SHADOWFILL_REQUIRE_CORE") == "1":
    importlib.import_module("shadowfill._core")


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


@pytest.fixture(scope="session")
def itch_sample():
    """Path to a Nasdaq ITCH sample, or skip."""
    samples = list(ITCH_DIR.glob("*.gz")) + list(ITCH_DIR.glob("*.itch"))
    if not samples:
        pytest.skip(f"no ITCH sample in {ITCH_DIR}; run scripts/fetch_itch_sample.sh")
    # Largest by bytes, not by name: a 2 MB and a 20 MB prefix of the same day
    # sort the wrong way round, and the short one covers too little to validate.
    return max(samples, key=lambda p: p.stat().st_size)

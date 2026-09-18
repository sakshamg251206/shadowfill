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

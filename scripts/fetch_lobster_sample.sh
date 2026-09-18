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

#!/usr/bin/env bash
# Downloads a small prefix of a public Nasdaq TotalView-ITCH 5.0 daily file.
#
# Nasdaq publishes whole trading days at emi.nasdaq.com with no account, no key
# and no charge. A full day is ~3.5 GB compressed, which is far more than the
# adapter needs to be validated, so this fetches only the first N megabytes with
# an HTTP Range request -- the server advertises `accept-ranges: bytes`.
#
# The sample is NOT committed: it is third-party data, and CI runs entirely on
# the synthetic fixture (invariant 6).
#
# Usage:  ./scripts/fetch_itch_sample.sh [MEGABYTES] [DEST]
#
# A prefix necessarily ends mid-message and, being gzip, mid-member. That is
# expected: parse it with `allow_truncated=True`, which reports the truncation
# in the diagnostics rather than hiding it.
#
# Note the coverage a prefix buys you. In 12302019, 20 MB compressed (51 MB raw,
# 1.76 M messages) reaches 05:56 ET -- pre-market only. Regular-hours data needs
# the whole file, because a gzip member cannot be seeked into.
set -euo pipefail

MB="${1:-20}"
DEST="${2:-data/itch}"
FILE="12302019.NASDAQ_ITCH50.gz"
URL="https://emi.nasdaq.com/ITCH/Nasdaq%20ITCH/${FILE}"
OUT="${DEST}/12302019.prefix${MB}mb.gz"

mkdir -p "$DEST"
BYTES=$(( MB * 1024 * 1024 - 1 ))
echo "Fetching first ${MB} MB of ${FILE} ..."
curl -fsSL -r "0-${BYTES}" -o "$OUT" "$URL"

ls -lh "$OUT"
echo
echo "Then run:  pytest -m needs_itch -v"
echo "The busiest symbol in this prefix is UN; override with SHADOWFILL_ITCH_SYMBOL."

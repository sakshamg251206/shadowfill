#!/usr/bin/env bash
# Fetch a Nasdaq ITCH daily file in short fixed-size byte ranges.
#
# A single long-lived connection to emi.nasdaq.com does not survive a 3.5 GB
# transfer: measured 2026-09-20, curl retried 100 times over four hours and
# finished at exactly the byte it started from, because each retry resumed from
# the offset it was launched with rather than from what was already on disk.
#
# Short ranges fix that. Each chunk is its own request, appended on success, so
# a reset costs one chunk instead of the whole transfer, and progress is always
# whatever is already on disk.
#
# Chunk size is small on purpose. The server throttles to ~120 KB/s at times,
# and a 32 MB chunk then needs ~286 s against a 300 s limit -- so chunks were
# timing out just short of completion and their work was discarded. 8 MB
# finishes in ~70 s at that rate, leaving room for the server to be slower
# still. A connection delivering almost nothing is abandoned in 60 s rather
# than held until the timeout.
#
# Usage:  ./scripts/fetch_itch_chunked.sh [FILE] [DEST] [CHUNK_MB]
set -euo pipefail

FILE="${1:-12302019.NASDAQ_ITCH50.gz}"
DEST="${2:-data/itch}"
CHUNK_MB="${3:-8}"
URL="https://emi.nasdaq.com/ITCH/Nasdaq%20ITCH/${FILE}"
OUT="${DEST}/${FILE}"

mkdir -p "$DEST"
TOTAL=$(curl -fsSI --http1.1 "$URL" | awk 'tolower($1)=="content-length:"{print $2+0}')
[ "${TOTAL:-0}" -gt 0 ] || { echo "could not read content-length" >&2; exit 1; }

CHUNK=$(( CHUNK_MB * 1024 * 1024 ))
while :; do
    HAVE=$(stat -f%z "$OUT" 2>/dev/null || echo 0)
    [ "$HAVE" -ge "$TOTAL" ] && break
    END=$(( HAVE + CHUNK - 1 ))
    [ "$END" -ge "$TOTAL" ] && END=$(( TOTAL - 1 ))
    # Append only on success: a partial chunk written straight to $OUT would
    # corrupt the offset the next iteration computes from the file size.
    if curl -fsS --http1.1 --max-time 600 --speed-limit 5000 --speed-time 60 \
        -r "${HAVE}-${END}" "$URL" > /tmp/itch_chunk.$$; then
        cat /tmp/itch_chunk.$$ >> "$OUT"
        printf '\r%d / %d MB' $(( (HAVE + CHUNK) / 1048576 )) $(( TOTAL / 1048576 ))
    else
        echo " chunk at ${HAVE} failed, retrying" >&2
        sleep 5
    fi
    rm -f /tmp/itch_chunk.$$
done
echo
ls -lh "$OUT"

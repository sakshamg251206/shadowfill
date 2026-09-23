#!/usr/bin/env bash
# Fetch a Nasdaq ITCH daily file as parallel fixed-size byte ranges.
#
# fetch_itch_chunked.sh is sequential and appends each chunk to the output as it
# lands. Measured 2026-09-24, it averaged ~67 KB/s on 03272019 -- chunks were
# being abandoned to the low-speed timeout and redone -- while a single fresh
# range request got ~512 KB/s and four concurrent ones ~910 KB/s combined. At
# the sequential rate the remaining 4.9 GB was ~20 hours; in parallel, ~1.5.
#
# Each chunk is written to its own part file and size-checked, so a failure
# costs one chunk and a rerun resumes exactly where it stopped. Only when every
# part is present and complete are they appended, in order, to the existing
# prefix -- the output file is never left holding anything but a valid prefix.
# Concurrency is kept modest on purpose: this is a free public server.
#
# Offsets go through `seq -f '%.0f'`: macOS seq prints anything past ~1e6 in
# scientific notation, which bash arithmetic then rejects.
#
# Usage:  ./scripts/fetch_itch_parallel.sh FILE [DEST] [JOBS] [CHUNK_MB]
set -euo pipefail

FILE="$1"
DEST="${2:-data/itch}"
JOBS="${3:-4}"
CHUNK_MB="${4:-8}"
URL="https://emi.nasdaq.com/ITCH/Nasdaq%20ITCH/${FILE}"
OUT="${DEST}/${FILE}"
PARTS="${DEST}/.parts-${FILE}"

mkdir -p "$DEST" "$PARTS"
TOTAL=$(curl -fsSI --http1.1 "$URL" | awk 'tolower($1)=="content-length:"{print $2+0}')
[ "${TOTAL:-0}" -gt 0 ] || { echo "could not read content-length" >&2; exit 1; }
CHUNK=$(( CHUNK_MB * 1024 * 1024 ))
HAVE=$(stat -f%z "$OUT" 2>/dev/null || echo 0)
echo "total ${TOTAL} bytes; prefix on disk ${HAVE}; fetching the rest with ${JOBS} connections"

fetch_part() {
    off="$1"; total="$2"; chunk="$3"; url="$4"; parts="$5"
    end=$(( off + chunk - 1 )); [ "$end" -ge "$total" ] && end=$(( total - 1 ))
    want=$(( end - off + 1 ))
    part="${parts}/$(printf '%012d' "$off")"
    [ -f "$part" ] && [ "$(stat -f%z "$part")" -eq "$want" ] && return 0
    for attempt in 1 2 3 4 5 6 7 8; do
        if curl -fs --http1.1 --max-time 240 --speed-limit 2048 --speed-time 60 \
                -r "${off}-${end}" -o "${part}.tmp" "$url" \
           && [ "$(stat -f%z "${part}.tmp")" -eq "$want" ]; then
            mv "${part}.tmp" "$part"; return 0
        fi
        rm -f "${part}.tmp"; sleep $(( attempt * 5 ))
    done
    echo "part at ${off} failed after 8 attempts" >&2; return 1
}
export -f fetch_part

seq -f "%.0f" "$HAVE" "$CHUNK" $(( TOTAL - 1 )) \
    | xargs -P "$JOBS" -I{} bash -c 'fetch_part "$@"' _ {} "$TOTAL" "$CHUNK" "$URL" "$PARTS"

# Every part must be present and complete before a single byte is appended.
for off in $(seq -f "%.0f" "$HAVE" "$CHUNK" $(( TOTAL - 1 ))); do
    end=$(( off + CHUNK - 1 )); [ "$end" -ge "$TOTAL" ] && end=$(( TOTAL - 1 ))
    part="${PARTS}/$(printf '%012d' "$off")"
    [ "$(stat -f%z "$part" 2>/dev/null || echo 0)" -eq $(( end - off + 1 )) ] \
        || { echo "incomplete part at ${off}; rerun to resume" >&2; exit 1; }
done
[ "$(stat -f%z "$OUT" 2>/dev/null || echo 0)" -eq "$HAVE" ] \
    || { echo "prefix changed under us; refusing to append" >&2; exit 1; }
# Exactly the offsets verified above, in order -- never a directory listing,
# which would sweep in a stale .tmp left by an interrupted earlier run.
for off in $(seq -f "%.0f" "$HAVE" "$CHUNK" $(( TOTAL - 1 ))); do
    cat "${PARTS}/$(printf '%012d' "$off")" >> "$OUT"
done

FINAL=$(stat -f%z "$OUT")
[ "$FINAL" -eq "$TOTAL" ] || { echo "size ${FINAL} != ${TOTAL}" >&2; exit 1; }
rm -rf "$PARTS"
echo "complete: ${FINAL} bytes. Run 'gzip -t ${OUT}' before using it."

# Coinbase L3 Recorder — Design

Status: approved 2026-09-19. Implementation plan not yet written.

This is **Plan 2a**. Plan 2 in `docs/RESEARCH-SPEC.md` bundles a live recorder
with offline adapters, Parquet partitioning and dataset manifests. Those are
independent subsystems, and only one of them is time-critical: tape accumulates
in wall-clock time and cannot be backfilled. So the recorder is specified and
built alone, and the canonical data layer becomes **Plan 2b**, designed against
real recorded messages rather than against API documentation.

---

## 1. What this is for

`RESEARCH-SPEC.md` §5 names self-recorded Coinbase L3 as the source for "scale +
out-of-venue generalisation", because Coinbase is one of the few venues exposing
per-order IDs and full order lifecycle events. Per-order IDs are the entire
basis of the project: without them there is no arrival sequence, and without an
arrival sequence the queue arithmetic in Plan 1 has nothing to stand on.

H2 additionally needs contrasting tick regimes. In crypto the relevant quantity
is the **relative** tick, `tick / price`, not the tick itself.

## 2. Scope

**In scope.** Connect to the Coinbase Exchange `level3` WebSocket channel for a
configured set of products, write every received message verbatim and durably to
disk, detect and record every discontinuity, survive disconnection, and emit a
manifest per session that makes the resulting tape auditable.

**Out of scope, deliberately.** Conversion to the canonical MBO schema, Parquet
output, Databento, order book reconstruction, and any analysis. All Plan 2b or
later. The recorder does not know what an order book is.

## 3. Venue facts this design rests on

Verified against Coinbase's documentation on 2026-09-19
(<https://docs.cdp.coinbase.com/exchange/websocket-feed/channels>):

- The `level3` channel **requires authentication**. Read-only market-data API
  credentials are sufficient. This was not true of the historical public
  Coinbase Pro `full` feed, and any tutorial predating the change is wrong.
- `level3` emits `open`, `change`, `match`, `done` and `noop`, each carrying
  `product_id`, `order_id` and a per-product `sequence`.
- Sequence numbers increase by exactly one per product. A jump means messages
  were dropped. A value at or below the last seen can be a duplicate or an
  out-of-order arrival.
- Recovery is: subscribe, queue messages, fetch a REST `level=3` snapshot,
  discard queued messages at or below the snapshot sequence, then apply the
  rest.
- `noop` carries a sequence number and no book change. It exists to keep the
  sequence contiguous, so a reader that discards it will manufacture false
  gaps. The recorder stores it like anything else.
- **Not every `done` or `change` message changes the book.** Some refer to
  orders that never rested. This is exactly the kind of semantic subtlety that
  argues for recording raw and interpreting offline.

Credentials are read from the environment. They are never committed, never
logged, and never written to a manifest.

## 4. Configuration

| key | default | meaning |
|---|---|---|
| `products` | `BTC-USD, ETH-USD, ADA-USD` | products to subscribe to |
| `out_dir` | `data/raw/coinbase` | tape root |
| `rotate_seconds` | `3600` | tape rotation period |
| `queue_maxsize` | `100_000` | bounded queue depth; see §11 |
| `stall_warn_seconds` | `1.0` | log a stall if the writer blocks this long |
| `disk_min_free_gb` | `5` | warn loudly below this |

The product default spans the tick regimes H2 needs: BTC-USD has a very small
relative tick (many price levels, thin queue at the touch), ADA-USD a large one
(few levels, deep queue), ETH-USD sits between. Holding the venue constant
isolates tick regime from venue microstructure.

## 5. Architecture

```
websocket ──► reader ──► bounded queue ──► writer ──► tape (.jsonl.zst)
                                              │
                                              └─► validator ──► gaps.jsonl
```

The reader timestamps and enqueues; it does nothing else, because anything it
does is time the socket is not being drained.

**The writer appends bytes to the tape before parsing them.** Tape integrity
must not be contingent on the parser being correct. An unparseable frame is
already durable by the time we discover we cannot read it, and the failure is
recorded rather than swallowed.

### Components

Each is separately testable, and none needs a network to test.

| component | responsibility | depends on |
|---|---|---|
| `Transport` | protocol: `connect`, `send`, `recv`, `close` | — |
| `CoinbaseTransport` | real WebSocket implementation | `websockets` |
| `FakeTransport` | scripted frames for tests | — |
| `CoinbaseFeed` | auth headers, subscribe payload, snapshot fetch | `Transport` |
| `SequenceTracker` | pure; per-product expected-next, classifies each message | — |
| `TapeWriter` | framing, compression, rotation, fsync policy | — |
| `GapLedger` | append-only JSONL of discontinuities | — |
| `SessionManifest` | hashes and summarises a finished tape | — |
| `Recorder` | wires the above, owns the tasks | all |

## 6. On-disk format

One tape per session, all products interleaved in arrival order. Splitting per
product requires parsing, and parsing belongs offline.

```
data/raw/coinbase/2026-09-19/
  session-0001-20260919T041500Z.jsonl.zst
  session-0001-20260919T041500Z.gaps.jsonl
  session-0001-20260919T041500Z.manifest.json
```

### Tape records

Each line splices the raw payload rather than re-encoding it:

```
{"t":<recv_unix_ns>,"m":<raw bytes exactly as received>}
```

A REST snapshot is not a WebSocket frame, so it carries an explicit kind and
the product it belongs to. Absence of `k` means "a frame from the socket":

```
{"t":<recv_unix_ns>,"k":"snapshot","product_id":"BTC-USD","m":<raw REST body>}
```

Every tape reader must handle both shapes. Keeping them on one tape, in arrival
order, means a session's recovery is legible from the tape alone rather than by
correlating two files by timestamp.

`t` is `CLOCK_REALTIME` at the moment of receipt, which is the only clock that
can be compared against the exchange's own `time` field. It is a host clock and
therefore NTP-dependent; the manifest records that, and no analysis should treat
it as exchange-accurate. Splicing avoids a decode/encode round trip and
guarantees the stored bytes are the received bytes.

Compressed with zstd. Rotated every `rotate_seconds` and on every reconnect, so
a tape never spans a discontinuity it cannot describe.

### Gap ledger

One JSON object per line, each with `kind`, `ts_ns` and `product_id`:

| kind | meaning |
|---|---|
| `exchange_gap` | sequence jumped; carries `expected` and `got` |
| `stale` | sequence at or below the last seen; carries both |
| `stall` | writer blocked; carries `duration_ns` |
| `unparseable` | frame stored but not readable; carries the byte offset |
| `session_start` / `session_end` | boundaries, with the reason |
| `snapshot` | a REST snapshot was written into the tape |

### Session manifest

Written when a tape closes: products, per-product first and last sequence,
counts by ledger kind, message and byte counts, SHA-256 of the compressed tape,
wall-clock span, recorder git SHA, and resolved environment. This mirrors Plan
1's run manifests: a dataset that cannot be traced to the code and machine that
produced it is not reproducible, and that claim is load-bearing here.

## 7. Failure handling

| failure | response |
|---|---|
| sequence jump | `exchange_gap`; keep recording |
| sequence at or below last | `stale`; keep recording |
| queue full | **block; never drop.** Log `stall` past the threshold |
| disconnect | exponential backoff with jitter, resubscribe, snapshot, new session |
| unparseable frame | `unparseable`; bytes already on tape |
| disk below threshold | loud warning; recording continues until it genuinely fails |
| credentials rejected | fail fast and loudly at startup, not silently at 3am |

**Blocking rather than dropping under backpressure is a deliberate choice.** If
the consumer stalls, the socket buffer fills and Coinbase eventually disconnects
us as a slow consumer. That is loud, detectable and recoverable through the
snapshot path. Dropping is quiet, and a quietly incomplete tape would corrupt
every queue statistic derived from it without ever announcing itself. The
recorder should fail in ways that are obvious.

Gaps are recorded, never repaired. A gap means the queue arithmetic cannot be
trusted across it, and Plan 2b decides how to segment around them. Interpolating
would be inventing data.

## 8. Testing

`Transport` is injectable, so CI drives the real `Recorder` against a scripted
`FakeTransport`. No network in CI, mirroring the existing rule that no
third-party data enters the test suite.

**Unit.** `SequenceTracker` across in-order, gapped, duplicated and reset
streams. Tape framing round-trips, including payloads containing braces and
newlines. Rotation boundary arithmetic. Manifest hashing.

**Integration, against `FakeTransport`.** A clean run produces a readable tape
and an empty ledger. An injected gap appears in the ledger with correct
`expected`/`got` and does not interrupt recording. A duplicate is classified
`stale`. A mid-stream disconnect produces `session_end`, a reconnect, a
snapshot record and `session_start`. A slow writer produces a `stall` entry and
loses nothing. A malformed frame is on the tape and in the ledger.

**The test that matters most:** every byte handed to the transport appears
exactly once, in order, in the decompressed tape, across every scenario above.
That is the recorder's whole contract.

**Live smoke test** behind a `needs_network` marker, mirroring `needs_lobster`.
It connects, records briefly, and asserts the tape parses and sequences are
contiguous. Never run in CI.

## 9. Dependencies

Two additions: `websockets` (protocol client) and `zstandard` (compression).
Hand-rolling either would be worse code than the dependency. Both go into
`pyproject.toml` with loose ranges and into `requirements-dev.lock` pinned, per
amendment G.

## 10. Operational shape

Runs under systemd on an always-on host with `Restart=always`. The research
evaluation design (`RESEARCH-SPEC.md` §6) block-bootstraps over sessions and
days, so continuity matters: a tape fragmented by a laptop's sleep schedule
makes "a session" an artefact of when the lid was open, which correlates with
time of day and therefore with volatility.

Volume is deliberately not estimated here. The recorder reports bytes and
messages per day from its first session, and the disk is sized from measurement.
A guess written into a spec tends to get quoted later as though it were a
finding.

## 11. Open question deferred to implementation

`queue_maxsize` cannot be chosen sensibly before observing real message rates.
The plan should set a conservative default, have the recorder log queue
high-water marks, and tune from the first days of recording.

## 12. Retention

Not specified. The recorder does not delete anything. Deleting research data on
a timer is a policy decision, and an unattended process that silently discards
tape is the wrong default for a project whose central claim is reproducibility.
If disk pressure becomes real, retention gets its own design with a prune ledger
so a dataset manifest can still account for what existed and what was removed.

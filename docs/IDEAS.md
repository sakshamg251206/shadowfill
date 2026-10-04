# Ideas and open engineering items

Things noticed while working that are outside the current plan. Each entry says
what it is, why it matters, and what doing it would cost. Nothing here is built.

## Credit fills from orders arriving at a lone shadow's price

**What.** Trade-throughs are credited since 2026-10-04: an aggressor executing
beyond a lone shadow's price fills it (amendments AH, AI). A second route is
not. While a shadow bid sits alone above the visible best bid, a new *sell
order* that arrives at or below its price would have hit it on arrival, and in
the data it rests instead. Neither engine credits that fill.

**Direction.** It can only miss fills, so the computed truth is still a lower
bound on what the stated assumptions imply, and measured understatements are
conservative on this account.

**Why it is not simply added.** Crediting it means treating a real order as
executing instead of resting, so the real book diverges from what it would have
been. That leans harder on the no-impact assumption than a trade-through does,
where the aggressor executed anyway.

**Cost.** Measure first, as the trade-through gap was: extend
`shadowfill.tradethrough` with the first opposite-side `ADD` priced at or
through each lone shadow's price, and report how far F* would move. Decide from
the size. A fix touches both engines and needs a full rerun again.

## Guard `fetch_itch_parallel.sh` against concurrent runs

Two copies of the script running against one file share part-file names, and the
size check cannot tell a complete part from one two writers have interleaved.
This corrupted the 2019-03-27 download once (amendment AG). A `mkdir` lock on
the parts directory, released on exit, would make a second run refuse to start.
Not built: it is infrastructure outside the current plan. Until then, run one
fetcher at a time and confirm no `xargs` or `curl` survives an interruption.

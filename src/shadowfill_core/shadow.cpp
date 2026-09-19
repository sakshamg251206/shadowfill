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

void ShadowTracker::settle(const Outcome& outcome) {
  outcomes_.push_back(outcome);
}

/// Strictly `effective_ts < now_ts`, and the outcome records the placement's
/// own effective timestamp rather than the current event's.
///
/// The strict comparison is the tie convention: an order arriving at exactly
/// the shadow's effective timestamp counts as *ahead* of it. Sequence numbers
/// decide priority and the shadow has none, so the pessimistic side is the
/// honest one -- never flatter the hypothetical order. Holding activation back
/// one event lets that event reach the book first, so its volume lands in
/// ahead_at_insert.
void ShadowTracker::activate(std::int64_t now_ts, std::uint64_t now_seq) {
  while (next_ < pending_.size() && pending_[next_].effective_ts() < now_ts) {
    const Placement& p = pending_[next_];
    Outcome o;
    o.shadow_id = p.shadow_id;
    o.status = Status::Resting;
    o.insert_ts = p.effective_ts();
    o.insert_seq = static_cast<std::int64_t>(now_seq);
    o.ahead_at_insert = book_.level_size(p.side, p.price);
    active_.push_back(
        Active{p, o, o.ahead_at_insert, p.effective_ts() + p.horizon_ns});
    ++next_;
  }
}

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

/// Non-counting priority lookup: an id absent from the book is assumed ahead.
/// The assumption is *counted* at its one call site that acts on it, not here,
/// so that a lookup which changes no queue never inflates the diagnostic.
bool ShadowTracker::is_ahead(std::uint64_t order_id,
                             std::int64_t insert_seq) const {
  const auto* order = book_.find(order_id);
  if (order == nullptr) return true;
  return static_cast<std::int64_t>(order->seq) < insert_seq;
}

void ShadowTracker::match(const Event& ev) {
  // ADD is behind us; hidden/cross/halt touch no visible queue.
  if (!consumes_visible_queue(ev.type)) return;

  for (Active& a : active_) {
    if (a.placement.side != ev.side || a.placement.price != ev.price) continue;

    if (ev.type != EventType::Execute) {
      if (!is_ahead(ev.order_id, a.outcome.insert_seq)) continue;
      if (a.ahead > 0 && book_.find(ev.order_id) == nullptr) {
        // The decrement rests on the unverifiable assumption that an id absent
        // from the book was resting ahead of the shadow.
        ++unknown_order_assumed_ahead_;
        ++a.outcome.assumed_ahead_events;
      }
      a.ahead = std::max<std::int64_t>(0, a.ahead - ev.size);
      continue;
    }

    // EXECUTE: price-time priority means the front of the queue is hit first,
    // so whatever this execution does not consume of `ahead` reaches the
    // shadow.
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

/// Per event, in exactly this order. Activation precedes expiry so a placement
/// whose entire life falls inside a quiet gap between two events activates and
/// then expires, rather than surviving to the end of the stream. Matching
/// precedes the book update because deciding whether a cancelled order was
/// ahead requires its arrival sequence, which is gone once the book deletes it.
void ShadowTracker::on_event(const Event& ev) {
  activate(ev.ts_ns, ev.seq);
  expire(ev.ts_ns);
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
  // Never activated: the effective timestamp fell past the last event, so the
  // order never existed. Distinct from Truncated, which is a censored
  // observation of an order that did.
  while (next_ < pending_.size()) {
    Outcome o;
    o.shadow_id = pending_[next_].shadow_id;
    o.status = Status::NotActivated;
    settle(o);
    ++next_;
  }
  std::sort(outcomes_.begin(), outcomes_.end(),
            [](const Outcome& a, const Outcome& b) {
              return a.shadow_id < b.shadow_id;
            });
}

}  // namespace shadowfill

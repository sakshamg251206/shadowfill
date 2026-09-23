#include "shadowfill/shadow.hpp"

#include <algorithm>

namespace shadowfill {

std::int64_t anonymous_removal_from_ahead(CancelModel model, std::int64_t qty,
                                          std::int64_t ahead,
                                          std::int64_t level) noexcept {
  std::int64_t taken = qty;
  if (model == CancelModel::Back) {
    const std::int64_t behind = std::max<std::int64_t>(0, level - ahead);
    taken = std::max<std::int64_t>(0, qty - behind);
  } else if (model == CancelModel::Proportional) {
    // Non-negative operands, so integer division floors exactly like Python //.
    taken = level > 0 ? qty * ahead / level : 0;
  }
  // A windowed session can leave a level holding less than a cancel removes.
  return std::min(ahead, taken);
}

ShadowTracker::ShadowTracker(std::vector<Placement> placements,
                             CancelModel cancel_model)
    : cancel_model_(cancel_model), pending_(std::move(placements)) {
  std::sort(pending_.begin(), pending_.end(),
            [](const Placement& a, const Placement& b) {
              if (a.effective_ts() != b.effective_ts())
                return a.effective_ts() < b.effective_ts();
              return a.shadow_id < b.shadow_id;
            });
  outcomes_.reserve(pending_.size());
}

void ShadowTracker::settle(std::size_t index) {
  Active& a = actives_[index];
  a.settled = true;
  outcomes_.push_back(a.outcome);
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

    const std::size_t index = actives_.size();
    const std::int64_t expiry = p.effective_ts() + p.horizon_ns;
    record_crossings(o, o.ahead_at_insert, p.effective_ts());
    actives_.push_back(Active{p, o, o.ahead_at_insert, expiry, false});
    by_level_[level_key(p.side, p.price)].push_back(index);
    expiries_.emplace(expiry, index);
    ++next_;
  }
}

/// Only the shadows actually due are touched, so a quiet event costs one heap
/// comparison rather than a scan of everything still live.
void ShadowTracker::expire(std::int64_t now_ts) {
  while (!expiries_.empty() && expiries_.top().first < now_ts) {
    const std::size_t index = expiries_.top().second;
    expiries_.pop();
    Active& a = actives_[index];
    if (a.settled) continue;
    a.outcome.status = Status::Expired;
    a.outcome.ahead_at_end = a.ahead;
    settle(index);
  }
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

  auto bucket = by_level_.find(level_key(ev.side, ev.price));
  if (bucket == by_level_.end()) return;
  std::vector<std::size_t>& indices = bucket->second;

  // Settled shadows are dropped from the bucket as they are encountered, by
  // swapping in the last element. Each one is removed at most once, so the
  // clean-up is amortised O(1) and the bucket stays the size of what is live.
  std::size_t i = 0;
  while (i < indices.size()) {
    Active& a = actives_[indices[i]];
    if (a.settled) {
      indices[i] = indices.back();
      indices.pop_back();
      continue;
    }

    if (ev.type != EventType::Execute) {
      if (ev.order_id == 0 && cancel_model_ != CancelModel::Front) {
        // An L2 heuristic, not the unknown-id assumption, so it does not
        // count towards assumed_ahead_events. Front stays on the original
        // path below, which keeps every committed result bit-identical.
        a.ahead -= anonymous_removal_from_ahead(
            cancel_model_, ev.size, a.ahead,
            book_.level_size(ev.side, ev.price));
        record_crossings(a.outcome, a.ahead, ev.ts_ns);
        ++i;
        continue;
      }
      if (is_ahead(ev.order_id, a.outcome.insert_seq)) {
        if (a.ahead > 0 && book_.find(ev.order_id) == nullptr) {
          // The decrement rests on the unverifiable assumption that an id
          // absent from the book was resting ahead of the shadow.
          ++unknown_order_assumed_ahead_;
          ++a.outcome.assumed_ahead_events;
        }
        a.ahead = std::max<std::int64_t>(0, a.ahead - ev.size);
        record_crossings(a.outcome, a.ahead, ev.ts_ns);
      }
      ++i;
      continue;
    }

    // EXECUTE: price-time priority means the front of the queue is hit first,
    // so whatever this execution does not consume of `ahead` reaches the
    // shadow.
    const std::int64_t consumed = std::min(ev.size, a.ahead);
    a.ahead -= consumed;
    record_crossings(a.outcome, a.ahead, ev.ts_ns);
    const std::int64_t residual = ev.size - consumed;
    if (residual <= 0) {
      ++i;
      continue;
    }

    const std::int64_t remaining = a.placement.size - a.outcome.filled_qty;
    const std::int64_t fill = std::min(residual, remaining);
    if (fill <= 0) {
      ++i;
      continue;
    }
    if (a.outcome.filled_qty == 0) a.outcome.first_fill_ts = ev.ts_ns;
    a.outcome.filled_qty += fill;
    if (a.outcome.filled_qty >= a.placement.size) {
      a.outcome.full_fill_ts = ev.ts_ns;
      a.outcome.status = Status::Filled;
      a.outcome.ahead_at_end = a.ahead;
      // A filled outcome is final, so it is settled here rather than in a
      // separate harvest pass over everything still live.
      settle(indices[i]);
      indices[i] = indices.back();
      indices.pop_back();
      continue;
    }
    ++i;
  }
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
  book_.apply(ev);
}

void ShadowTracker::finalize() {
  for (std::size_t i = 0; i < actives_.size(); ++i) {
    if (actives_[i].settled) continue;
    actives_[i].outcome.status = Status::Truncated;
    actives_[i].outcome.ahead_at_end = actives_[i].ahead;
    settle(i);
  }
  // Never activated: the effective timestamp fell past the last event, so the
  // order never existed. Distinct from Truncated, which is a censored
  // observation of an order that did.
  while (next_ < pending_.size()) {
    Outcome o;
    o.shadow_id = pending_[next_].shadow_id;
    o.status = Status::NotActivated;
    outcomes_.push_back(o);
    ++next_;
  }
  std::sort(outcomes_.begin(), outcomes_.end(),
            [](const Outcome& a, const Outcome& b) {
              return a.shadow_id < b.shadow_id;
            });
}

}  // namespace shadowfill

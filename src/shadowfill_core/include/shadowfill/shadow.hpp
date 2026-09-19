#pragma once
#include <cstdint>
#include <deque>
#include <queue>
#include <unordered_map>
#include <utility>
#include <vector>

#include "shadowfill/book.hpp"
#include "shadowfill/event.hpp"

namespace shadowfill {

/// Terminal state of a shadow order. Values match
/// python/shadowfill/replay.py::Status exactly -- Task 11 compares the raw
/// integers field by field.
///
/// Truncated and NotActivated are deliberately distinct. Truncated means the
/// order was live and still resting when the stream ended: a censored
/// observation. NotActivated means its effective timestamp fell past the last
/// event, so it never existed, and it must not enter any fill-rate denominator.
enum class Status : std::uint8_t {
  Resting = 0,
  Filled = 1,
  Expired = 2,
  Truncated = 3,
  NotActivated = 4,
};

struct Placement {
  std::uint64_t shadow_id;
  std::int64_t ts_ns;
  std::int64_t latency_ns;
  Side side;
  std::int64_t price;
  std::int64_t size;
  std::int64_t horizon_ns;

  [[nodiscard]] std::int64_t effective_ts() const noexcept {
    return ts_ns + latency_ns;
  }
};

struct Outcome {
  std::uint64_t shadow_id = 0;
  Status status = Status::Resting;
  std::int64_t insert_ts = -1;
  std::int64_t insert_seq = -1;
  std::int64_t ahead_at_insert = -1;
  std::int64_t ahead_at_end = -1;
  std::int64_t first_fill_ts = -1;
  std::int64_t full_fill_ts = -1;
  std::int64_t filled_qty = 0;
  std::int64_t assumed_ahead_events = 0;
};

class ShadowTracker {
 public:
  explicit ShadowTracker(std::vector<Placement> placements);

  void on_event(const Event& ev);
  void finalize();

  /// Outcomes ordered by shadow_id. Valid only after finalize().
  [[nodiscard]] const std::vector<Outcome>& outcomes() const noexcept {
    return outcomes_;
  }
  [[nodiscard]] std::uint64_t unknown_order_assumed_ahead() const noexcept {
    return unknown_order_assumed_ahead_;
  }
  /// Delegates to the book: this counts breaches in the data, not anything
  /// about a shadow. The earlier shadow-relative version fired whenever a
  /// shadow reached the front of its queue and filled there -- the normal
  /// path -- so it could never be zero on a correct FIFO stream.
  [[nodiscard]] std::uint64_t fifo_violations() const noexcept {
    return book_.fifo_violations();
  }
  [[nodiscard]] const OrderBook& book() const noexcept { return book_; }

 private:
  struct Active {
    Placement placement;
    Outcome outcome;
    std::int64_t ahead;
    std::int64_t expiry_ts;
    bool settled = false;
  };

  /// One bucket per (side, price). An event can only touch shadows resting at
  /// its own price on its own side, so matching never looks at the rest.
  [[nodiscard]] static std::uint64_t level_key(Side side,
                                               std::int64_t price) noexcept {
    return static_cast<std::uint64_t>(price) * 2U +
           (side == Side::Bid ? 1U : 0U);
  }

  void activate(std::int64_t now_ts, std::uint64_t now_seq);
  void expire(std::int64_t now_ts);
  void match(const Event& ev);
  [[nodiscard]] bool is_ahead(std::uint64_t order_id,
                              std::int64_t insert_seq) const;
  void settle(std::size_t index);

  OrderBook book_;
  std::vector<Placement> pending_;
  std::size_t next_ = 0;

  // Stable storage: indices into this are held by the level buckets and the
  // expiry heap, so entries are marked settled rather than erased. Memory is
  // therefore O(total placements) rather than O(live ones).
  // ponytail: fine while a run's placements fit in memory (400k ~= 45 MB);
  // recycle settled slots via a free list if a run ever outgrows that.
  std::deque<Active> actives_;
  std::unordered_map<std::uint64_t, std::vector<std::size_t>> by_level_;

  using Expiry = std::pair<std::int64_t, std::size_t>;
  std::priority_queue<Expiry, std::vector<Expiry>, std::greater<>> expiries_;

  std::vector<Outcome> outcomes_;
  std::uint64_t unknown_order_assumed_ahead_ = 0;
};

}  // namespace shadowfill

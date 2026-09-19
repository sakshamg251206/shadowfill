#pragma once
#include <cstdint>
#include <deque>
#include <functional>
#include <map>
#include <optional>
#include <unordered_map>

#include "shadowfill/event.hpp"

namespace shadowfill {

/// Mirrors python/shadowfill/replay.py::RefBook. Price levels live in std::map
/// so best_bid()/best_ask() are O(log n) rather than the reference's linear
/// scan; bids use std::greater so begin() is the best price on both sides.
class OrderBook {
 public:
  struct Resting {
    std::int64_t price;
    std::int64_t size;
    std::uint64_t seq;
    Side side;
  };

  void apply(const Event& ev);

  [[nodiscard]] const Resting* find(std::uint64_t order_id) const;
  [[nodiscard]] std::int64_t level_size(Side side, std::int64_t price) const;
  [[nodiscard]] std::optional<std::int64_t> best_bid() const;
  [[nodiscard]] std::optional<std::int64_t> best_ask() const;
  [[nodiscard]] std::uint64_t unknown_order_events() const noexcept {
    return unknown_order_events_;
  }
  /// Price-time priority breaches seen in the data: an execution that hit an
  /// order while one that arrived earlier was still resting at the same price.
  /// Zero on a correct FIFO stream; on real data it counts hidden liquidity,
  /// order types this reconstruction does not model, or gaps in it. It says
  /// nothing about any shadow -- it is a property of the event stream alone.
  [[nodiscard]] std::uint64_t fifo_violations() const noexcept {
    return fifo_violations_;
  }

 private:
  void reduce_level(Side side, std::int64_t price, std::int64_t qty);
  /// Earliest-arrived order still resting at this price, purging ids that
  /// cancels removed from the middle of the queue. O(1) amortised: each id is
  /// discarded at most once.
  const Resting* oldest_resting(Side side, std::int64_t price);

  std::unordered_map<std::uint64_t, Resting> orders_;
  std::map<std::int64_t, std::int64_t, std::greater<>> bids_;
  std::map<std::int64_t, std::int64_t> asks_;
  std::unordered_map<std::int64_t, std::deque<std::uint64_t>> bid_q_;
  std::unordered_map<std::int64_t, std::deque<std::uint64_t>> ask_q_;
  std::uint64_t unknown_order_events_ = 0;
  std::uint64_t fifo_violations_ = 0;
};

}  // namespace shadowfill

#pragma once
#include <cstdint>
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

 private:
  void reduce_level(Side side, std::int64_t price, std::int64_t qty);

  std::unordered_map<std::uint64_t, Resting> orders_;
  std::map<std::int64_t, std::int64_t, std::greater<>> bids_;
  std::map<std::int64_t, std::int64_t> asks_;
  std::uint64_t unknown_order_events_ = 0;
};

}  // namespace shadowfill

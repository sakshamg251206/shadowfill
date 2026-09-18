#include "shadowfill/book.hpp"

namespace shadowfill {

void OrderBook::reduce_level(Side side, std::int64_t price, std::int64_t qty) {
  if (side == Side::Bid) {
    auto it = bids_.find(price);
    if (it == bids_.end()) return;
    it->second -= qty;
    if (it->second <= 0) bids_.erase(it);
  } else {
    auto it = asks_.find(price);
    if (it == asks_.end()) return;
    it->second -= qty;
    if (it->second <= 0) asks_.erase(it);
  }
}

void OrderBook::apply(const Event& ev) {
  if (ev.type == EventType::Add) {
    orders_[ev.order_id] = Resting{ev.price, ev.size, ev.seq, ev.side};
    if (ev.side == Side::Bid) {
      bids_[ev.price] += ev.size;
    } else {
      asks_[ev.price] += ev.size;
    }
    return;
  }

  if (!consumes_visible_queue(ev.type)) return;

  auto it = orders_.find(ev.order_id);
  if (it == orders_.end()) {
    // Added before the recording window or outside the level band.
    ++unknown_order_events_;
    reduce_level(ev.side, ev.price, ev.size);
    return;
  }

  it->second.size -= ev.size;
  reduce_level(it->second.side, it->second.price, ev.size);
  if (it->second.size <= 0) orders_.erase(it);
}

const OrderBook::Resting* OrderBook::find(std::uint64_t order_id) const {
  auto it = orders_.find(order_id);
  return it == orders_.end() ? nullptr : &it->second;
}

std::int64_t OrderBook::level_size(Side side, std::int64_t price) const {
  if (side == Side::Bid) {
    auto it = bids_.find(price);
    return it == bids_.end() ? 0 : it->second;
  }
  auto it = asks_.find(price);
  return it == asks_.end() ? 0 : it->second;
}

std::optional<std::int64_t> OrderBook::best_bid() const {
  if (bids_.empty()) return std::nullopt;
  return bids_.begin()->first;
}

std::optional<std::int64_t> OrderBook::best_ask() const {
  if (asks_.empty()) return std::nullopt;
  return asks_.begin()->first;
}

}  // namespace shadowfill

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

const OrderBook::Resting* OrderBook::oldest_resting(Side side,
                                                   std::int64_t price) {
  auto& per_price = (side == Side::Bid) ? bid_q_ : ask_q_;
  auto lvl = per_price.find(price);
  if (lvl == per_price.end()) return nullptr;
  auto& q = lvl->second;
  while (!q.empty() && orders_.find(q.front()) == orders_.end()) q.pop_front();
  if (q.empty()) return nullptr;
  return &orders_.at(q.front());
}

void OrderBook::apply(const Event& ev) {
  if (ev.type == EventType::Add) {
    orders_[ev.order_id] = Resting{ev.price, ev.size, ev.seq, ev.side};
    if (ev.side == Side::Bid) {
      bids_[ev.price] += ev.size;
      bid_q_[ev.price].push_back(ev.order_id);
    } else {
      asks_[ev.price] += ev.size;
      ask_q_[ev.price].push_back(ev.order_id);
    }
    return;
  }

  if (!consumes_visible_queue(ev.type)) return;

  auto it = orders_.find(ev.order_id);
  if (ev.type == EventType::Execute && it != orders_.end()) {
    const Resting* oldest = oldest_resting(it->second.side, it->second.price);
    if (oldest != nullptr && oldest->seq < it->second.seq) ++fifo_violations_;
    it = orders_.find(ev.order_id);  // oldest_resting may rehash nothing, but
                                     // re-find keeps the iterator obviously
                                     // valid rather than subtly so.
  }
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

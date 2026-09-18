#include <catch2/catch_test_macros.hpp>

#include "shadowfill/book.hpp"

using namespace shadowfill;

namespace {
Event add(std::uint64_t seq, std::uint64_t oid, std::int64_t px,
          std::int64_t sz, Side side) {
  return Event{static_cast<std::int64_t>(seq) * 1000, seq, oid, px, sz,
               EventType::Add, side};
}
}  // namespace

TEST_CASE("add accumulates level size") {
  OrderBook b;
  b.apply(add(0, 1, 100, 10, Side::Bid));
  b.apply(add(1, 2, 100, 5, Side::Bid));
  REQUIRE(b.level_size(Side::Bid, 100) == 15);
  REQUIRE(b.best_bid() == 100);
}

TEST_CASE("delete removes order and empties the level") {
  OrderBook b;
  b.apply(add(0, 1, 100, 10, Side::Bid));
  b.apply(Event{1000, 1, 1, 100, 10, EventType::Delete, Side::Bid});
  REQUIRE(b.level_size(Side::Bid, 100) == 0);
  REQUIRE(b.find(1) == nullptr);
  REQUIRE_FALSE(b.best_bid().has_value());
}

TEST_CASE("partial cancel keeps the order alive") {
  OrderBook b;
  b.apply(add(0, 1, 100, 10, Side::Bid));
  b.apply(Event{1000, 1, 1, 100, 4, EventType::CancelPartial, Side::Bid});
  REQUIRE(b.level_size(Side::Bid, 100) == 6);
  REQUIRE(b.find(1)->size == 6);
}

TEST_CASE("hidden execution leaves the visible book untouched") {
  OrderBook b;
  b.apply(add(0, 1, 100, 10, Side::Ask));
  b.apply(Event{1000, 1, 0, 100, 7, EventType::ExecuteHidden, Side::Ask});
  REQUIRE(b.level_size(Side::Ask, 100) == 10);
}

TEST_CASE("unknown order ids still reduce the level and are counted") {
  OrderBook b;
  b.apply(add(0, 1, 100, 10, Side::Bid));
  b.apply(Event{1000, 1, 999, 100, 4, EventType::Execute, Side::Bid});
  REQUIRE(b.level_size(Side::Bid, 100) == 6);
  REQUIRE(b.unknown_order_events() == 1);
}

TEST_CASE("best prices pick the correct extremes") {
  OrderBook b;
  b.apply(add(0, 1, 100, 10, Side::Bid));
  b.apply(add(1, 2, 101, 10, Side::Bid));
  b.apply(add(2, 3, 105, 10, Side::Ask));
  b.apply(add(3, 4, 104, 10, Side::Ask));
  REQUIRE(b.best_bid() == 101);
  REQUIRE(b.best_ask() == 104);
}

TEST_CASE("arrival sequence is retained for priority comparisons") {
  OrderBook b;
  b.apply(add(0, 1, 100, 10, Side::Bid));
  b.apply(add(7, 2, 100, 10, Side::Bid));
  REQUIRE(b.find(1)->seq == 0);
  REQUIRE(b.find(2)->seq == 7);
}

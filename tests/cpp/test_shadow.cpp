#include <catch2/catch_test_macros.hpp>
#include <vector>

#include "shadowfill/shadow.hpp"

using namespace shadowfill;

namespace {
constexpr std::int64_t kSec = 1'000'000'000;

Placement place(std::int64_t ts, std::int64_t size = 10,
                std::int64_t latency = 0, std::int64_t horizon = 10 * kSec) {
  return Placement{0, ts, latency, Side::Bid, 100, size, horizon};
}

std::vector<Outcome> run(const std::vector<Event>& events,
                         const std::vector<Placement>& placements) {
  ShadowTracker tracker(placements);
  for (const auto& ev : events) tracker.on_event(ev);
  tracker.finalize();
  return tracker.outcomes();
}
}  // namespace

TEST_CASE("ahead at insert equals resting volume at the price") {
  std::vector<Event> events{
      {0, 0, 1, 100, 30, EventType::Add, Side::Bid},
      {kSec, 1, 2, 100, 20, EventType::Add, Side::Bid},
      {2 * kSec, 2, 3, 100, 5, EventType::Add, Side::Bid},
  };
  auto out = run(events, {place(kSec + 1)});
  REQUIRE(out[0].ahead_at_insert == 50);
  REQUIRE(out[0].insert_seq == 2);
}

TEST_CASE("orders arriving after insertion are behind the shadow") {
  std::vector<Event> events{
      {0, 0, 1, 100, 30, EventType::Add, Side::Bid},
      {kSec, 1, 2, 100, 999, EventType::Add, Side::Bid},
      {2 * kSec, 2, 1, 100, 30, EventType::Execute, Side::Bid},
      {3 * kSec, 3, 3, 100, 10, EventType::Execute, Side::Bid},
  };
  auto out = run(events, {place(0)});
  REQUIRE(out[0].status == Status::Filled);
  REQUIRE(out[0].first_fill_ts == 3 * kSec);
}

TEST_CASE("cancellation ahead shrinks the queue, behind does not") {
  std::vector<Event> events{
      {0, 0, 1, 100, 30, EventType::Add, Side::Bid},
      {kSec, 1, 2, 100, 40, EventType::Add, Side::Bid},
      {2 * kSec, 2, 2, 100, 40, EventType::Delete, Side::Bid},
      {3 * kSec, 3, 1, 100, 25, EventType::CancelPartial, Side::Bid},
  };
  auto out = run(events, {place(500'000'000)});
  REQUIRE(out[0].ahead_at_insert == 30);
  REQUIRE(out[0].ahead_at_end == 5);
}

TEST_CASE("hidden executions never consume the queue") {
  std::vector<Event> events{
      {0, 0, 1, 100, 10, EventType::Add, Side::Bid},
      {kSec, 1, 0, 100, 10, EventType::ExecuteHidden, Side::Bid},
      {2 * kSec, 2, 0, 100, 10, EventType::ExecuteHidden, Side::Bid},
  };
  // Horizon deliberately shorter than the stream, so the terminal state is a
  // real expiry: the two hidden executions must leave the queue untouched all
  // the way to it. Mirrors test_hidden_execution_never_consumes_the_queue.
  auto out = run(events, {place(0, 10, 0, 1 * kSec)});
  REQUIRE(out[0].ahead_at_end == 10);
  REQUIRE(out[0].status == Status::Expired);
}

TEST_CASE("partial then full fill records both timestamps") {
  std::vector<Event> events{
      {0, 0, 1, 100, 5, EventType::Add, Side::Bid},
      {kSec, 1, 1, 100, 5, EventType::Execute, Side::Bid},
      {2 * kSec, 2, 2, 100, 3, EventType::Execute, Side::Bid},
      {3 * kSec, 3, 3, 100, 7, EventType::Execute, Side::Bid},
  };
  auto out = run(events, {place(0, 10)});
  REQUIRE(out[0].first_fill_ts == 2 * kSec);
  REQUIRE(out[0].full_fill_ts == 3 * kSec);
  REQUIRE(out[0].filled_qty == 10);
}

TEST_CASE("latency puts intervening orders ahead") {
  std::vector<Event> events{
      {0, 0, 1, 100, 10, EventType::Add, Side::Bid},
      {kSec, 1, 2, 100, 40, EventType::Add, Side::Bid},
      {3 * kSec, 2, 1, 100, 10, EventType::Execute, Side::Bid},
  };
  REQUIRE(run(events, {place(0, 10, 0)})[0].ahead_at_insert == 10);
  REQUIRE(run(events, {place(0, 10, 2 * kSec)})[0].ahead_at_insert == 50);
}

TEST_CASE("horizon expiry beats a later fill") {
  std::vector<Event> events{
      {0, 0, 1, 100, 10, EventType::Add, Side::Bid},
      {5 * kSec, 1, 1, 100, 10, EventType::Execute, Side::Bid},
  };
  auto out = run(events, {place(0, 10, 0, 2 * kSec)});
  REQUIRE(out[0].status == Status::Expired);
  REQUIRE(out[0].first_fill_ts == -1);
}

TEST_CASE("still resting at end of data is not activated") {
  // Activation is strictly `effective_ts < now_ts`, so a placement whose
  // effective timestamp is 0 never activates against a lone event at ts=0:
  // it never existed, which is not the same as being censored.
  // Mirrors test_still_resting_at_end_of_data_is_not_activated.
  std::vector<Event> events{{0, 0, 1, 100, 10, EventType::Add, Side::Bid}};
  REQUIRE(run(events, {place(0)})[0].status == Status::NotActivated);
}

TEST_CASE("activated and still resting at end of data is truncated") {
  // The second event activates the shadow; the stream then ends under it while
  // it is still inside its horizon -- the genuine censored observation.
  // Mirrors test_activated_and_still_resting_at_end_of_data_is_truncated.
  std::vector<Event> events{
      {0, 0, 1, 100, 10, EventType::Add, Side::Bid},
      {kSec, 1, 2, 100, 10, EventType::Add, Side::Bid},
  };
  auto out = run(events, {place(0)});
  REQUIRE(out[0].status == Status::Truncated);
  REQUIRE(out[0].ahead_at_insert == 10);
}

// --- L2 queue models for anonymous cancels. Mirrors
// tests/python/test_cancel_models.py; the Python oracle is authoritative.
namespace {
std::int64_t ahead_after_anonymous_cancel(CancelModel model, std::int64_t qty) {
  // 30 shares ahead of the shadow, 70 behind, then an id-0 cancel of `qty`.
  std::vector<Event> events{
      {0, 0, 1, 100, 30, EventType::Add, Side::Bid},
      {kSec, 1, 2, 100, 70, EventType::Add, Side::Bid},
      {2 * kSec, 2, 0, 100, qty, EventType::Delete, Side::Bid},
  };
  ShadowTracker tracker({place(1, 10, 0, 100 * kSec)}, model);
  for (const auto& ev : events) tracker.on_event(ev);
  tracker.finalize();
  return tracker.outcomes()[0].ahead_at_end;
}
}  // namespace

TEST_CASE("front takes an anonymous cancel from ahead") {
  REQUIRE(ahead_after_anonymous_cancel(CancelModel::Front, 40) == 0);
}

TEST_CASE("back takes an anonymous cancel from behind first") {
  REQUIRE(ahead_after_anonymous_cancel(CancelModel::Back, 40) == 30);
  REQUIRE(ahead_after_anonymous_cancel(CancelModel::Back, 80) == 20);
}

TEST_CASE("proportional takes the ahead share of the level, floored") {
  REQUIRE(ahead_after_anonymous_cancel(CancelModel::Proportional, 40) == 18);
}

TEST_CASE("no model drives ahead negative") {
  for (auto m : {CancelModel::Front, CancelModel::Back, CancelModel::Proportional}) {
    REQUIRE(ahead_after_anonymous_cancel(m, 500) >= 0);
  }
}

// --- First-passage times of queue-ahead. Mirrors the Python oracle's
// test_crossing_times_* cases.
TEST_CASE("crossing times record when ahead first drops below each threshold") {
  std::vector<Event> events{
      {0, 0, 1, 100, 1500, EventType::Add, Side::Bid},
      {kSec, 1, 1, 100, 600, EventType::CancelPartial, Side::Bid},
      {2 * kSec, 2, 1, 100, 850, EventType::CancelPartial, Side::Bid},
      {3 * kSec, 3, 1, 100, 45, EventType::Execute, Side::Bid},
      {4 * kSec, 4, 1, 100, 10, EventType::Execute, Side::Bid},
  };
  auto out = run(events, {place(1, 10)});
  REQUIRE(out[0].ahead_at_insert == 1500);
  REQUIRE(out[0].ahead_lt_1000_ts == kSec);
  REQUIRE(out[0].ahead_lt_100_ts == 2 * kSec);
  REQUIRE(out[0].ahead_lt_10_ts == 3 * kSec);
  REQUIRE(out[0].ahead_lt_1_ts == 4 * kSec);
}

TEST_CASE("thresholds already below at insert take the insert time") {
  std::vector<Event> events{
      {0, 0, 1, 100, 5, EventType::Add, Side::Bid},
      {kSec, 1, 2, 100, 5, EventType::Add, Side::Bid},
  };
  auto out = run(events, {place(1)});
  REQUIRE(out[0].ahead_lt_1000_ts == 1);
  REQUIRE(out[0].ahead_lt_10_ts == 1);
  REQUIRE(out[0].ahead_lt_1_ts == -1);
}

#pragma once
#include <cstdint>

namespace shadowfill {

enum class EventType : std::uint8_t {
  Add = 1,
  CancelPartial = 2,
  Delete = 3,
  Execute = 4,
  ExecuteHidden = 5,
  Cross = 6,
  Halt = 7,
};

enum class Side : std::int8_t { Bid = 1, Ask = -1 };

struct Event {
  std::int64_t ts_ns;
  std::uint64_t seq;
  std::uint64_t order_id;
  std::int64_t price;
  std::int64_t size;
  EventType type;
  Side side;
};

/// ExecuteHidden is excluded deliberately: a hidden order never sat in the
/// visible book, so it must never consume visible queue.
constexpr bool consumes_visible_queue(EventType t) noexcept {
  return t == EventType::CancelPartial || t == EventType::Delete ||
         t == EventType::Execute;
}

}  // namespace shadowfill

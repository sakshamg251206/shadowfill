#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>

#include <stdexcept>
#include <string>
#include <vector>

#include "shadowfill/shadow.hpp"

namespace py = pybind11;
using namespace shadowfill;

namespace {

template <typename T>
using Arr = py::array_t<T, py::array::c_style | py::array::forcecast>;

py::dict replay(Arr<std::int64_t> ts_ns, Arr<std::uint64_t> seq,
                Arr<std::uint64_t> order_id, Arr<std::int64_t> price,
                Arr<std::int64_t> size, Arr<std::uint8_t> type,
                Arr<std::int8_t> side, Arr<std::uint64_t> p_shadow_id,
                Arr<std::int64_t> p_ts_ns, Arr<std::int64_t> p_latency_ns,
                Arr<std::int8_t> p_side, Arr<std::int64_t> p_price,
                Arr<std::int64_t> p_size, Arr<std::int64_t> p_horizon_ns) {
  const auto n = static_cast<std::size_t>(ts_ns.size());
  for (const auto& sz : {seq.size(), order_id.size(), price.size(),
                         size.size(), type.size(), side.size()}) {
    if (static_cast<std::size_t>(sz) != n) {
      throw std::invalid_argument("event arrays have mismatched lengths");
    }
  }
  const auto m = static_cast<std::size_t>(p_shadow_id.size());
  for (const auto& sz : {p_ts_ns.size(), p_latency_ns.size(), p_side.size(),
                         p_price.size(), p_size.size(), p_horizon_ns.size()}) {
    if (static_cast<std::size_t>(sz) != m) {
      throw std::invalid_argument("placement arrays have mismatched lengths");
    }
  }

  std::vector<Placement> placements;
  placements.reserve(m);
  for (std::size_t i = 0; i < m; ++i) {
    placements.push_back(Placement{
        p_shadow_id.at(i), p_ts_ns.at(i), p_latency_ns.at(i),
        static_cast<Side>(p_side.at(i)), p_price.at(i), p_size.at(i),
        p_horizon_ns.at(i)});
  }

  ShadowTracker tracker(std::move(placements));
  {
    py::gil_scoped_release release;
    auto t = ts_ns.unchecked<1>();
    auto s = seq.unchecked<1>();
    auto o = order_id.unchecked<1>();
    auto pr = price.unchecked<1>();
    auto sz = size.unchecked<1>();
    auto ty = type.unchecked<1>();
    auto sd = side.unchecked<1>();
    for (std::size_t i = 0; i < n; ++i) {
      tracker.on_event(Event{t(i), s(i), o(i), pr(i), sz(i),
                             static_cast<EventType>(ty(i)),
                             static_cast<Side>(sd(i))});
    }
    tracker.finalize();
  }

  const auto& outcomes = tracker.outcomes();
  const auto k = outcomes.size();
  auto make_i64 = [&](auto getter) {
    py::array_t<std::int64_t> out(static_cast<py::ssize_t>(k));
    auto view = out.mutable_unchecked<1>();
    for (std::size_t i = 0; i < k; ++i) view(i) = getter(outcomes[i]);
    return out;
  };

  py::array_t<std::uint64_t> ids(static_cast<py::ssize_t>(k));
  {
    auto view = ids.mutable_unchecked<1>();
    for (std::size_t i = 0; i < k; ++i) view(i) = outcomes[i].shadow_id;
  }

  py::dict result;
  result["shadow_id"] = ids;
  result["status"] = make_i64([](const Outcome& o) {
    return static_cast<std::int64_t>(o.status);
  });
  result["insert_ts"] = make_i64([](const Outcome& o) { return o.insert_ts; });
  result["insert_seq"] = make_i64([](const Outcome& o) { return o.insert_seq; });
  result["ahead_at_insert"] =
      make_i64([](const Outcome& o) { return o.ahead_at_insert; });
  result["ahead_at_end"] =
      make_i64([](const Outcome& o) { return o.ahead_at_end; });
  result["first_fill_ts"] =
      make_i64([](const Outcome& o) { return o.first_fill_ts; });
  result["full_fill_ts"] =
      make_i64([](const Outcome& o) { return o.full_fill_ts; });
  result["filled_qty"] = make_i64([](const Outcome& o) { return o.filled_qty; });
  result["assumed_ahead_events"] =
      make_i64([](const Outcome& o) { return o.assumed_ahead_events; });
  result["unknown_order_assumed_ahead"] = tracker.unknown_order_assumed_ahead();
  result["fifo_violations"] = tracker.fifo_violations();
  result["unknown_order_events"] = tracker.book().unknown_order_events();
  return result;
}

}  // namespace

PYBIND11_MODULE(_core, m) {
  m.doc() = "ShadowFill C++ replay engine";

  // Amendment H: a result that cannot be traced to an environment is not
  // reproducible, and this repository leans on that claim. The manifest records
  // which compiler built the engine that produced the numbers.
#if defined(__clang__)
  m.attr("compiler") = "clang " __clang_version__;
#elif defined(__GNUC__)
  m.attr("compiler") = "gcc " __VERSION__;
#elif defined(_MSC_VER)
  m.attr("compiler") = "msvc " + std::to_string(_MSC_VER);
#else
  m.attr("compiler") = "unknown";
#endif

  m.def("replay", &replay, "Replay MBO events and evaluate shadow placements",
        py::arg("ts_ns"), py::arg("seq"), py::arg("order_id"), py::arg("price"),
        py::arg("size"), py::arg("type"), py::arg("side"),
        py::arg("p_shadow_id"), py::arg("p_ts_ns"), py::arg("p_latency_ns"),
        py::arg("p_side"), py::arg("p_price"), py::arg("p_size"),
        py::arg("p_horizon_ns"));
}

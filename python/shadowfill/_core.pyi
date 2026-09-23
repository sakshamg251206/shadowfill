"""Type stub for the compiled engine in ``src/shadowfill_bindings/module.cpp``.

A stub rather than an ``ignore_missing_imports`` override: the call site passes
fourteen parallel arrays whose dtypes have to line up with the C++ signature,
which is exactly the mistake worth having checked.
"""

from typing import Any

import numpy as np
import numpy.typing as npt

compiler: str

def replay(
    ts_ns: npt.NDArray[np.int64],
    seq: npt.NDArray[np.uint64],
    order_id: npt.NDArray[np.uint64],
    price: npt.NDArray[np.int64],
    size: npt.NDArray[np.int64],
    type: npt.NDArray[np.uint8],
    side: npt.NDArray[np.int8],
    p_shadow_id: npt.NDArray[np.uint64],
    p_ts_ns: npt.NDArray[np.int64],
    p_latency_ns: npt.NDArray[np.int64],
    p_side: npt.NDArray[np.int8],
    p_price: npt.NDArray[np.int64],
    p_size: npt.NDArray[np.int64],
    p_horizon_ns: npt.NDArray[np.int64],
    cancel_model: int = 0,
) -> dict[str, Any]: ...

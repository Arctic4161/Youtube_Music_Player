"""Density-independent responsive scaling helpers."""

from __future__ import annotations


def bounded_ui_scale(
    width: float,
    height: float,
    *,
    reference_width: float = 411.0,
    minimum: float = 0.85,
    maximum: float = 1.35,
) -> float:
    """Scale from logical window units without applying screen density twice."""

    if reference_width <= 0:
        return 1.0
    raw = min(float(width), float(height)) / reference_width
    return max(minimum, min(maximum, raw))

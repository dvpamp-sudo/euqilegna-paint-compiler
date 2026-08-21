from __future__ import annotations

from typing import Tuple
import numpy as np
from scipy import ndimage as ndi


def label_point(mask: np.ndarray) -> Tuple[float, float, float]:
    if mask is None or not np.any(mask):
        return 0.0, 0.0, 7.0
    mask = np.asarray(mask, dtype=bool)
    distance = ndi.distance_transform_edt(mask)
    y, x = np.unravel_index(np.argmax(distance), distance.shape)
    radius = float(distance[y, x])
    return float(x), float(y), max(radius, 7.0)


def label_is_visible(record: dict) -> bool:
    label = record.get("label") or {}
    try:
        return (
            bool(label.get("visible", True))
            and float(label.get("fontSize", 0)) >= 7.0
            and float(label.get("radius", 0)) >= 1.0
        )
    except (TypeError, ValueError):
        return False

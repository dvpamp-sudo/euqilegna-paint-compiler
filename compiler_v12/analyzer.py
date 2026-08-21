from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image


def analyze_source(input_path: str | Path) -> dict[str, Any]:
    rgb = np.array(Image.open(input_path).convert("RGB"))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    edges = cv2.Canny(gray, 45, 120)

    h, w = gray.shape
    dark_ratio = float((gray < 50).mean())
    light_ratio = float((gray > 215).mean())
    saturation = float(hsv[..., 1].mean() / 255.0)
    edge_density = float((edges > 0).mean())
    tonal_std = float(gray.std() / 255.0)
    near_monochrome = saturation < 0.16

    if near_monochrome and dark_ratio > 0.22 and edge_density > 0.04:
        family = "graphic_monochrome"
    elif edge_density > 0.12 and light_ratio > 0.45 and saturation < 0.25:
        family = "line_art"
    elif w / max(h, 1) > 1.28:
        family = "landscape_or_scene"
    elif saturation > 0.38 and edge_density > 0.07:
        family = "decorative_or_illustration"
    else:
        family = "general_artwork"

    return {
        "family": family,
        "width": int(w),
        "height": int(h),
        "darkRatio": round(dark_ratio, 5),
        "lightRatio": round(light_ratio, 5),
        "saturation": round(saturation, 5),
        "edgeDensity": round(edge_density, 5),
        "tonalStd": round(tonal_std, 5),
    }

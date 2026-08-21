from __future__ import annotations

from pathlib import Path
from PIL import Image
import numpy as np


class ImageLoadError(ValueError):
    pass


def load_rgb(path: Path) -> np.ndarray:
    if not path.is_file():
        raise ImageLoadError(f"Image file does not exist: {path}")

    try:
        image = Image.open(path).convert("RGB")
    except Exception as exc:
        raise ImageLoadError(f"Unable to read image: {exc}") from exc

    array = np.asarray(image, dtype=np.uint8)
    if array.ndim != 3 or array.shape[2] != 3:
        raise ImageLoadError("Image did not decode as RGB.")
    if array.shape[0] < 32 or array.shape[1] < 32:
        raise ImageLoadError("Image is too small to compile reliably.")
    return array


from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Tuple
import json
import time
import xml.etree.ElementTree as ET

import cv2
import numpy as np
from PIL import Image
from scipy import ndimage as ndi
from skimage import color, measure, morphology, segmentation
from shapely.geometry import Polygon
from shapely.ops import unary_union
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors as pdfcolors
from reportlab.pdfgen import canvas
from interaction_engine_v9 import build_pointer_engine_script
from artwork_profiles import infer_profile, apply_profile_settings
from numbering_engine import label_point
from validation_engine import validate_compiled_records, write_validation_report


@dataclass(frozen=True)
class Preset:
    max_side: int
    colors: int
    min_region_area: int
    seed_min_area: int
    simplify_tolerance: float
    line_low: int
    line_high: int
    line_dark_threshold: int
    line_dilate: int


PRESETS: Dict[str, Preset] = {
    "kids": Preset(900, 16, 420, 110, 2.4, 70, 165, 65, 2),
    "beginner": Preset(1100, 20, 260, 70, 1.7, 58, 145, 72, 2),
    "balanced": Preset(1350, 24, 150, 42, 1.05, 48, 125, 78, 1),
    "advanced": Preset(1600, 32, 80, 24, 0.65, 38, 105, 84, 1),
    "illustration": Preset(1800, 40, 38, 10, 0.35, 28, 82, 92, 0),
}


def rgb_to_hex(rgb: Iterable[int]) -> str:
    r, g, b = [int(v) for v in rgb]
    return f"#{r:02X}{g:02X}{b:02X}"


def resize_keep_aspect(rgb: np.ndarray, max_side: int) -> np.ndarray:
    h, w = rgb.shape[:2]
    scale = min(max_side / max(h, w), 1.0)
    if scale >= 1:
        return rgb
    return cv2.resize(
        rgb,
        (max(1, round(w * scale)), max(1, round(h * scale))),
        interpolation=cv2.INTER_AREA,
    )


def border_pixels(rgb: np.ndarray) -> np.ndarray:
    return np.concatenate(
        [rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]],
        axis=0,
    ).astype(np.float32)


def estimate_border_color(rgb: np.ndarray) -> np.ndarray:
    return np.median(border_pixels(rgb), axis=0)


def foreground_mask(rgb: np.ndarray) -> np.ndarray:
    h, w = rgb.shape[:2]
    border_color = estimate_border_color(rgb)
    lab = color.rgb2lab(rgb)
    border_lab = color.rgb2lab(np.uint8([[border_color]])).reshape(3)
    distance = np.linalg.norm(lab - border_lab, axis=2)

    border_lab_values = color.rgb2lab(
        np.uint8(border_pixels(rgb).reshape(-1, 1, 3))
    ).reshape(-1, 3)
    border_variation = np.linalg.norm(border_lab_values - border_lab, axis=1)
    threshold = max(8.0, float(np.percentile(border_variation, 92)) + 4.5)

    possible_bg = distance <= threshold
    possible_bg = morphology.closing(possible_bg, morphology.disk(3))
    possible_bg = morphology.remove_small_holes(
        possible_bg,
        area_threshold=max(100, h * w // 1200),
    )

    seeds = np.zeros((h, w), dtype=bool)
    seeds[0] = possible_bg[0]
    seeds[-1] = possible_bg[-1]
    seeds[:, 0] = possible_bg[:, 0]
    seeds[:, -1] = possible_bg[:, -1]
    connected_bg = ndi.binary_propagation(seeds, mask=possible_bg)

    fg = ~connected_bg
    fg = morphology.remove_small_objects(
        fg,
        min_size=max(48, h * w // 4500),
    )
    fg = morphology.closing(fg, morphology.disk(2))
    return fg



def safe_foreground_mask(
    rgb: np.ndarray,
    *,
    use_full_canvas: bool = False,
    minimum_coverage: float = 0.12,
) -> Tuple[np.ndarray, dict]:
    """
    Return a usable foreground mask without allowing foreground detection to
    terminate compilation.

    Full-frame artwork always receives an all-true mask. For portrait-style
    artwork, failed or implausibly small masks fall back to the full canvas.
    """
    h, w = rgb.shape[:2]
    full = np.ones((h, w), dtype=bool)

    if use_full_canvas:
        return full, {
            "mode": "full_canvas",
            "fallbackUsed": False,
            "coverage": 1.0,
        }

    try:
        detected = foreground_mask(rgb)
        detected = np.asarray(detected, dtype=bool)
    except Exception as exc:
        return full, {
            "mode": "full_canvas_fallback",
            "fallbackUsed": True,
            "reason": f"foreground_detection_error: {exc}",
            "coverage": 1.0,
        }

    coverage = float(detected.mean()) if detected.size else 0.0
    if not detected.any() or coverage < minimum_coverage:
        return full, {
            "mode": "full_canvas_fallback",
            "fallbackUsed": True,
            "reason": "foreground_missing_or_too_small",
            "detectedCoverage": round(coverage, 6),
            "coverage": 1.0,
        }

    return detected, {
        "mode": "detected_foreground",
        "fallbackUsed": False,
        "coverage": round(coverage, 6),
    }


def crop_subject(
    rgb: np.ndarray,
    fg: np.ndarray,
    padding_ratio: float = 0.09,
) -> Tuple[np.ndarray, np.ndarray, dict]:
    h, w = rgb.shape[:2]
    coords = np.argwhere(fg)
    if coords.size == 0:
        return rgb, fg, {"cropped": False, "box": [0, 0, w, h]}

    y0, x0 = coords.min(axis=0)
    y1, x1 = coords.max(axis=0) + 1
    px = max(20, int((x1 - x0) * padding_ratio))
    py = max(20, int((y1 - y0) * padding_ratio))
    x0, y0 = max(0, x0 - px), max(0, y0 - py)
    x1, y1 = min(w, x1 + px), min(h, y1 + py)

    if (x1 - x0) * (y1 - y0) >= h * w * 0.98:
        return rgb, fg, {"cropped": False, "box": [0, 0, w, h]}

    return (
        rgb[y0:y1, x0:x1],
        fg[y0:y1, x0:x1],
        {"cropped": True, "box": [int(x0), int(y0), int(x1), int(y1)]},
    )


def preprocess(rgb: np.ndarray) -> np.ndarray:
    # Preserve cel-shaded edges and small highlight shapes.
    return cv2.bilateralFilter(rgb, 5, 30, 30)

def preprocess_graphic_monochrome(rgb: np.ndarray) -> np.ndarray:
    """Preserve broad black/gray fills and fine vinyl-style grooves."""
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    gray = cv2.bilateralFilter(gray, 3, 18, 18)
    levels = np.array([0, 28, 62, 102, 150, 205, 242, 255], dtype=np.uint8)
    indices = np.abs(
        gray[..., None].astype(np.int16) - levels[None, None, :].astype(np.int16)
    ).argmin(axis=2)
    mapped = levels[indices]
    blended = cv2.addWeighted(gray, 0.42, mapped, 0.58, 0)
    return cv2.cvtColor(blended, cv2.COLOR_GRAY2RGB)



def detect_ink_barriers(
    rgb: np.ndarray,
    fg: np.ndarray,
    cfg: Preset,
) -> Tuple[np.ndarray, np.ndarray]:
    """Detect thin illustration lines without swallowing broad dark fills."""
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    canny = cv2.Canny(gray, cfg.line_low, cfg.line_high)

    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    gradient = cv2.magnitude(gx, gy)
    if gradient.max() > 0:
        gradient = gradient / gradient.max()

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    blackhat = cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, kernel)
    thin_dark_line = (blackhat >= 15) & (gradient >= 0.08)

    # Never classify every dark pixel as ink. Solid black clothing, hair,
    # shadows, gloves, and tires must remain paintable color regions.
    barrier = (canny > 0) | thin_dark_line | (gradient > 0.44)
    barrier &= fg
    barrier = morphology.remove_small_objects(barrier, min_size=5)
    barrier = morphology.closing(barrier, morphology.disk(1))

    if cfg.line_dilate > 0:
        barrier = morphology.dilation(
            barrier,
            morphology.disk(cfg.line_dilate),
        )

    strength = np.maximum.reduce([
        canny.astype(np.float32) / 255.0,
        np.clip(blackhat.astype(np.float32) / 48.0, 0.0, 1.0),
        gradient.astype(np.float32),
    ])
    strength[~fg] = 1.0
    return barrier, strength

def quantize_pixels(
    rgb: np.ndarray,
    fg: np.ndarray,
    color_count: int,
) -> Tuple[np.ndarray, np.ndarray]:
    pixels = rgb[fg].astype(np.float32)
    if pixels.size == 0:
        # Last-resort recovery: compile the complete canvas rather than
        # terminating a customer's full-frame artwork.
        fg = np.ones(rgb.shape[:2], dtype=bool)
        pixels = rgb.reshape(-1, 3).astype(np.float32)

    k = min(color_count, max(2, len(pixels) // 100))
    criteria = (
        cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER,
        100,
        0.25,
    )
    _, labels, centers = cv2.kmeans(
        pixels,
        k,
        None,
        criteria,
        8,
        cv2.KMEANS_PP_CENTERS,
    )
    centers = np.clip(centers, 0, 255).astype(np.uint8)

    color_map = np.full(fg.shape, -1, dtype=np.int32)
    color_map[fg] = labels.reshape(-1)
    return color_map, centers




def preprocess_basic_scenic(rgb: np.ndarray) -> np.ndarray:
    """
    Simplify photographic/scenic artwork into broader paintable shapes.
    Removes tiny bright sparkles, softens bokeh/background noise, and preserves
    stronger subject edges such as butterflies, flowers, and major leaves.
    """
    h, w = rgb.shape[:2]

    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    bright = (hsv[:, :, 2] > 215) & (hsv[:, :, 1] < 95)

    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        bright.astype(np.uint8),
        connectivity=8,
    )
    small_specks = np.zeros((h, w), dtype=np.uint8)
    max_speck = max(10, int((h * w) / 90000))
    for index in range(1, count):
        area = int(stats[index, cv2.CC_STAT_AREA])
        if area <= max_speck:
            small_specks[labels == index] = 255

    median = cv2.medianBlur(rgb, 7)
    cleaned = rgb.copy()
    cleaned[small_specks > 0] = median[small_specks > 0]

    smoothed = cv2.bilateralFilter(cleaned, 11, 55, 55)
    smoothed = cv2.pyrMeanShiftFiltering(smoothed, sp=12, sr=24, maxLevel=1)

    gray = cv2.cvtColor(cleaned, cv2.COLOR_RGB2GRAY)
    major_edges = cv2.Canny(gray, 70, 145)
    major_edges = cv2.morphologyEx(
        major_edges,
        cv2.MORPH_CLOSE,
        np.ones((3, 3), np.uint8),
    )
    major_edges = cv2.dilate(major_edges, np.ones((2, 2), np.uint8), iterations=1)

    result = smoothed.copy()
    contour_mask = cv2.GaussianBlur(major_edges, (0, 0), 1.1) > 12
    result[contour_mask] = cleaned[contour_mask]
    return result


def classify_image_pipeline(rgb: np.ndarray) -> dict:
    """
    Heuristically choose between illustration and photo pipelines.
    This runs locally and does not upload the artwork or require an AI model.
    """
    small = resize_keep_aspect(rgb, 640)
    gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
    hsv = cv2.cvtColor(small, cv2.COLOR_RGB2HSV)

    edges = cv2.Canny(gray, 60, 140)
    edge_density = float((edges > 0).mean())

    lap = cv2.Laplacian(gray, cv2.CV_32F)
    texture = float(np.mean(np.abs(lap))) / 255.0

    # Photographic gradients have many unique colors even after downsampling.
    quant = (small // 16).reshape(-1, 3)
    unique_ratio = min(1.0, len(np.unique(quant, axis=0)) / 1800.0)

    saturation_std = float(hsv[:, :, 1].std()) / 255.0
    value_std = float(hsv[:, :, 2].std()) / 255.0

    photo_score = (
        unique_ratio * 0.42
        + texture * 0.20
        + saturation_std * 0.17
        + value_std * 0.16
        + max(0.0, 0.075 - edge_density) * 2.2
    )
    illustration_score = (
        min(1.0, edge_density * 5.5) * 0.52
        + max(0.0, 0.48 - unique_ratio) * 0.55
        + max(0.0, 0.16 - texture) * 0.55
    )

    # Scenic AI art can contain strong linework while still behaving like a photo:
    # many gradients, bokeh, soft lighting, and thousands of colors. Treat those
    # images as photo-like even when edge density is moderately high.
    scenic_photo_like = (
        unique_ratio >= 0.34
        and texture <= 0.14
        and value_std >= 0.16
    )

    pipeline = (
        "photo"
        if photo_score > illustration_score or scenic_photo_like
        else "illustration"
    )
    confidence = abs(photo_score - illustration_score) / max(
        0.001, photo_score + illustration_score
    )
    return {
        "pipeline": pipeline,
        "confidence": round(min(0.99, max(0.51, 0.55 + confidence * 0.75)), 2),
        "edgeDensity": round(edge_density, 4),
        "uniqueColorScore": round(unique_ratio, 4),
        "textureScore": round(texture, 4),
    }


def merge_slic_regions(
    rgb: np.ndarray,
    labels: np.ndarray,
    target_regions: int,
    color_threshold: float = 13.5,
) -> np.ndarray:
    """Merge adjacent SLIC superpixels by Lab color distance."""
    result = relabel(labels.astype(np.int32) + 1)
    lab = color.rgb2lab(rgb / 255.0).astype(np.float32)

    for _ in range(12):
        ids, counts = np.unique(result[result > 0], return_counts=True)
        if len(ids) <= target_regions:
            break

        means = {
            int(rid): lab[result == rid].mean(axis=0)
            for rid in ids
        }
        areas = {int(rid): int(count) for rid, count in zip(ids, counts)}
        graph = adjacency(result)
        candidates = []

        for rid in ids:
            rid = int(rid)
            for neighbor in graph.get(rid, []):
                if rid >= neighbor:
                    continue
                distance = float(np.linalg.norm(means[rid] - means[neighbor]))
                # Prefer merging smaller neighboring regions first.
                size_bias = min(6.0, (areas[rid] + areas[neighbor]) ** 0.5 / 28.0)
                candidates.append((distance + size_bias, distance, rid, neighbor))

        if not candidates:
            break

        candidates.sort()
        changed = False
        merge_limit = color_threshold + max(
            0.0, (len(ids) - target_regions) / max(1, target_regions)
        ) * 7.0

        consumed = set()
        for _, distance, rid, neighbor in candidates:
            if rid in consumed or neighbor in consumed:
                continue
            if distance > merge_limit and len(ids) <= target_regions * 1.35:
                continue

            # Merge smaller into larger to retain stable shapes.
            source, destination = (
                (rid, neighbor)
                if areas[rid] <= areas[neighbor]
                else (neighbor, rid)
            )
            result[result == source] = destination
            consumed.add(source)
            changed = True

            if len(ids) - len(consumed) <= target_regions:
                break

        result = relabel(result)
        if not changed:
            break

    return result


def photo_slic_regions(
    rgb: np.ndarray,
    target_regions: int,
    min_region_area: int,
) -> np.ndarray:
    """
    Build complete paint regions for photos and soft scenic art.
    Unlike the illustration pipeline, this covers every pixel and does not
    depend on closed ink lines.
    """
    h, w = rgb.shape[:2]
    pixels = h * w

    # Start with enough superpixels to preserve the subject, then merge down.
    initial_segments = max(
        target_regions * 3,
        min(1800, max(260, pixels // 1800)),
    )

    source = cv2.bilateralFilter(rgb, 9, 42, 42)
    source = cv2.pyrMeanShiftFiltering(source, sp=8, sr=20, maxLevel=1)

    labels = segmentation.slic(
        source,
        n_segments=int(initial_segments),
        compactness=13.0,
        sigma=1.1,
        start_label=0,
        convert2lab=True,
        enforce_connectivity=True,
        min_size_factor=0.45,
        max_size_factor=3.5,
        channel_axis=-1,
    )

    labels = merge_slic_regions(
        source,
        labels,
        target_regions=max(80, target_regions),
        color_threshold=12.5,
    )

    # Merge remaining undersized regions without requiring artificial ink edges.
    zero_barrier = np.zeros(labels.shape, dtype=np.float32)
    low_detail = np.full(labels.shape, 0.25, dtype=np.float32)
    labels = adaptive_merge_regions(
        source,
        labels,
        zero_barrier,
        low_detail,
        max(18, min_region_area),
        "relaxed",
        max(80, target_regions),
    )

    # Fill any accidental zero gaps using nearest labeled pixels.
    missing = labels == 0
    if missing.any():
        nearest = ndi.distance_transform_edt(
            missing,
            return_distances=False,
            return_indices=True,
        )
        labels[missing] = labels[tuple(nearest[:, missing])]

    return relabel(labels)


PREMIUM_ADULT_PALETTE = np.array(
    [
        [42, 49, 71],    # deep slate
        [92, 71, 93],    # plum
        [154, 89, 104],  # dusty rose
        [214, 132, 124], # terracotta blush
        [237, 185, 140], # warm peach
        [220, 194, 112], # muted gold
        [108, 135, 92],  # sage
        [62, 97, 79],    # forest
        [116, 151, 166], # blue gray
        [210, 217, 213], # mist
        [129, 98, 76],   # umber
        [241, 230, 213], # cream
    ],
    dtype=np.uint8,
)


def premium_lineart_regions(
    rgb: np.ndarray,
    min_region_area: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Convert clean coloring-book or paint-by-number line art into smooth enclosed
    regions. Decorative ink stays as ink and does not become a paint cell.
    """
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)

    # Preserve intentional dark line work while ignoring light antialiasing.
    ink = gray < 155
    ink = morphology.remove_small_objects(ink, min_size=8)
    ink = morphology.binary_closing(ink, morphology.disk(1))
    ink = cv2.dilate(ink.astype(np.uint8), np.ones((2, 2), np.uint8), iterations=1).astype(bool)

    # Close tiny line gaps so petals/leaves become proper enclosed regions.
    closed = morphology.binary_closing(ink, morphology.disk(2))
    open_space = ~closed

    labels = measure.label(open_space, connectivity=2).astype(np.int32)
    h, w = labels.shape

    # Remove the exterior page/background component touching image edges.
    edge_ids = np.unique(
        np.concatenate(
            [labels[0, :], labels[-1, :], labels[:, 0], labels[:, -1]]
        )
    )
    for rid in edge_ids:
        labels[labels == rid] = 0

    labels = relabel(labels)

    # Remove only truly tiny enclosed artifacts; retain flower centers and accents.
    for rid in np.unique(labels):
        if rid <= 0:
            continue
        area = int((labels == rid).sum())
        if area < max(18, min_region_area // 5):
            labels[labels == rid] = 0

    labels = relabel(labels)

    # Smooth region edges without changing the hand-designed structure.
    cleaned = np.zeros_like(labels)
    next_id = 1
    for rid in np.unique(labels):
        if rid <= 0:
            continue
        mask = labels == rid
        mask = morphology.binary_opening(mask, morphology.disk(1))
        mask = morphology.binary_closing(mask, morphology.disk(1))
        if int(mask.sum()) < max(18, min_region_area // 5):
            continue
        cleaned[mask] = next_id
        next_id += 1

    barrier_strength = cv2.GaussianBlur(
        closed.astype(np.float32),
        (0, 0),
        0.8,
    )
    return relabel(cleaned), closed, barrier_strength


def assign_premium_lineart_palette(
    labels: np.ndarray,
    rgb: np.ndarray,
) -> tuple[dict, np.ndarray, dict]:
    """
    Use source colors when the artwork is already colored. For monochrome line
    art, assign a curated adult palette while avoiding identical neighboring colors.
    """
    ids = [int(i) for i in np.unique(labels) if i > 0]
    saturation = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)[:, :, 1]
    colored_source = float((saturation > 35).mean()) > 0.10

    original_means = {
        rid: rgb[labels == rid].mean(axis=0)
        for rid in ids
    }

    if colored_source:
        samples = np.array([original_means[rid] for rid in ids], dtype=np.float32)
        n_colors = min(12, max(6, int(round(len(ids) ** 0.5))))
        criteria = (
            cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER,
            80,
            0.35,
        )
        _compactness, assignments, centers = cv2.kmeans(
            samples.astype(np.float32),
            n_colors,
            None,
            criteria,
            8,
            cv2.KMEANS_PP_CENTERS,
        )
        assignments = assignments.reshape(-1)
        region_to_color = {
            rid: int(assignments[index])
            for index, rid in enumerate(ids)
        }
        return region_to_color, np.clip(centers, 0, 255).astype(np.uint8), original_means

    centers = PREMIUM_ADULT_PALETTE.copy()
    graph = adjacency(labels)
    region_to_color: dict[int, int] = {}

    # Largest shapes first. Pick the first palette color not used by neighbors.
    areas = {rid: int((labels == rid).sum()) for rid in ids}
    for index, rid in enumerate(sorted(ids, key=areas.get, reverse=True)):
        used = {
            region_to_color[n]
            for n in graph.get(rid, [])
            if n in region_to_color
        }
        preferred = (index * 5 + rid * 3) % len(centers)
        choices = list(range(len(centers)))
        choices.sort(key=lambda c: (c in used, (c - preferred) % len(centers)))
        region_to_color[rid] = choices[0]

    # For monochrome art, original/reference colors should match the curated preview.
    original_means = {
        rid: centers[region_to_color[rid]].astype(np.float32)
        for rid in ids
    }
    return region_to_color, centers, original_means

def adaptive_detail_map(
    rgb: np.ndarray,
    fg: np.ndarray,
    barrier_strength: np.ndarray,
) -> np.ndarray:
    """
    Estimate where detail should be protected. High values favor smaller regions;
    low values favor simpler, easier-to-paint regions.
    """
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV).astype(np.float32)
    saturation = hsv[:, :, 1] / 255.0
    value = hsv[:, :, 2] / 255.0

    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    local_variance = cv2.GaussianBlur(gray.astype(np.float32) ** 2, (0, 0), 2.0)
    local_mean = cv2.GaussianBlur(gray.astype(np.float32), (0, 0), 2.0)
    local_variance = np.maximum(0.0, local_variance - local_mean ** 2)
    if local_variance.max() > 0:
        local_variance /= local_variance.max()

    # Protect likely face/skin areas and expressive bright accents.
    r, g, b = [rgb[:, :, i].astype(np.int16) for i in range(3)]
    skin_like = (
        (r > 75)
        & (g > 35)
        & (b > 20)
        & (r > g)
        & ((r - b) > 12)
        & ((r - g) < 95)
    ).astype(np.float32)

    accent = ((saturation > 0.48) & (value > 0.36)).astype(np.float32)

    h, w = fg.shape
    yy, xx = np.mgrid[0:h, 0:w]
    cx, cy = w / 2.0, h / 2.0
    radial = 1.0 - np.sqrt(
        ((xx - cx) / max(1.0, w / 2.0)) ** 2
        + ((yy - cy) / max(1.0, h / 2.0)) ** 2
    )
    radial = np.clip(radial, 0.0, 1.0).astype(np.float32)

    detail = (
        barrier_strength * 0.38
        + local_variance.astype(np.float32) * 0.24
        + saturation.astype(np.float32) * 0.12
        + skin_like * 0.14
        + accent * 0.07
        + radial * 0.05
    )
    detail = cv2.GaussianBlur(detail, (0, 0), 1.2)
    detail = np.clip(detail, 0.0, 1.0)
    detail[~fg] = 0.0
    return detail


def adaptive_seed_threshold(
    detail_mean: float,
    base_seed_area: int,
    experience_mode: str,
) -> int:
    mode_factor = {
        "relaxed": 1.55,
        "balanced": 1.0,
        "detailed": 0.70,
    }.get(experience_mode, 1.0)

    # High-detail areas receive smaller seeds; quiet areas receive larger seeds.
    adaptive = base_seed_area * mode_factor * (1.55 - 1.05 * detail_mean)
    return max(5, int(round(adaptive)))


def build_markers(
    color_map: np.ndarray,
    fg: np.ndarray,
    barrier: np.ndarray,
    detail_map: np.ndarray,
    seed_min_area: int,
    experience_mode: str,
) -> np.ndarray:
    markers = np.zeros(fg.shape, dtype=np.int32)
    marker_id = 1
    interior = fg & ~barrier

    for color_id in [v for v in np.unique(color_map) if v >= 0]:
        mask = interior & (color_map == color_id)
        components = measure.label(mask, connectivity=2)

        for comp in range(1, int(components.max()) + 1):
            component_mask = components == comp
            area = int(component_mask.sum())
            detail_mean = float(detail_map[component_mask].mean()) if area else 0.0
            required_area = adaptive_seed_threshold(
                detail_mean,
                seed_min_area,
                experience_mode,
            )
            if area < required_area:
                continue

            # Erode slightly so markers sit inside visual regions, not on line edges.
            eroded = morphology.erosion(component_mask, morphology.disk(1))
            if eroded.sum() >= max(4, seed_min_area // 4):
                component_mask = eroded

            markers[component_mask] = marker_id
            marker_id += 1

    if marker_id == 1:
        # Conservative fallback.
        components = measure.label(interior, connectivity=2)
        for comp in range(1, int(components.max()) + 1):
            mask = components == comp
            area = int(mask.sum())
            detail_mean = float(detail_map[mask].mean()) if area else 0.0
            required_area = adaptive_seed_threshold(
                detail_mean,
                seed_min_area,
                experience_mode,
            )
            if area >= required_area:
                markers[mask] = marker_id
                marker_id += 1

    return markers


def illustration_watershed(
    rgb: np.ndarray,
    fg: np.ndarray,
    barrier: np.ndarray,
    barrier_strength: np.ndarray,
    markers: np.ndarray,
) -> np.ndarray:
    lab = color.rgb2lab(rgb)
    color_gradient = np.zeros(fg.shape, dtype=np.float32)
    for channel in range(3):
        gx = cv2.Sobel(
            lab[:, :, channel].astype(np.float32),
            cv2.CV_32F,
            1,
            0,
            ksize=3,
        )
        gy = cv2.Sobel(
            lab[:, :, channel].astype(np.float32),
            cv2.CV_32F,
            0,
            1,
            ksize=3,
        )
        color_gradient += cv2.magnitude(gx, gy)

    if color_gradient.max() > 0:
        color_gradient /= color_gradient.max()

    elevation = (
        color_gradient * 0.45
        + barrier_strength * 1.55
        + barrier.astype(np.float32) * 2.0
    )

    labels = segmentation.watershed(
        elevation,
        markers=markers,
        mask=fg,
        connectivity=2,
        watershed_line=True,
        compactness=0.0001,
    ).astype(np.int32)

    return labels


def adjacency(labels: np.ndarray) -> Dict[int, set]:
    graph = {int(v): set() for v in np.unique(labels) if v > 0}
    for a, b in ((labels[:, :-1], labels[:, 1:]), (labels[:-1], labels[1:, :])):
        changed = (a != b) & (a > 0) & (b > 0)
        for left, right in zip(a[changed], b[changed]):
            li, ri = int(left), int(right)
            graph.setdefault(li, set()).add(ri)
            graph.setdefault(ri, set()).add(li)
    return graph


def shared_boundary(labels: np.ndarray, a: int, b: int) -> np.ndarray:
    boundary = np.zeros(labels.shape, dtype=bool)
    boundary[:, :-1] |= (
        ((labels[:, :-1] == a) & (labels[:, 1:] == b))
        | ((labels[:, :-1] == b) & (labels[:, 1:] == a))
    )
    boundary[:-1, :] |= (
        ((labels[:-1] == a) & (labels[1:] == b))
        | ((labels[:-1] == b) & (labels[1:] == a))
    )
    return boundary


def relabel(labels: np.ndarray) -> np.ndarray:
    values = [v for v in np.unique(labels) if v > 0]
    mapping = {old: new + 1 for new, old in enumerate(values)}
    result = np.zeros_like(labels, dtype=np.int32)
    for old, new in mapping.items():
        result[labels == old] = new
    return result


def merge_only_micro_regions(
    rgb: np.ndarray,
    labels: np.ndarray,
    barrier_strength: np.ndarray,
    min_region_area: int,
) -> np.ndarray:
    result = labels.copy()

    for _ in range(5):
        ids, counts = np.unique(result[result > 0], return_counts=True)
        areas = {int(i): int(c) for i, c in zip(ids, counts)}
        if not areas:
            return result

        means = {
            rid: rgb[result == rid].mean(axis=0)
            for rid in areas
        }
        graph = adjacency(result)
        changed = False

        for rid in sorted(areas, key=areas.get):
            area = areas[rid]
            if area >= min_region_area:
                continue

            neighbors = list(graph.get(rid, []))
            if not neighbors:
                continue

            candidates = []
            for neighbor in neighbors:
                boundary = shared_boundary(result, rid, neighbor)
                if not boundary.any():
                    continue
                barrier_mean = float(barrier_strength[boundary].mean())
                barrier_high = float(
                    (barrier_strength[boundary] >= 0.55).mean()
                )
                color_distance = float(
                    np.linalg.norm(means[rid] - means[neighbor])
                )
                score = (
                    color_distance
                    + barrier_mean * 100.0
                    + barrier_high * 180.0
                )
                candidates.append(
                    (score, barrier_mean, barrier_high, color_distance, neighbor)
                )

            if not candidates:
                continue

            _, barrier_mean, barrier_high, color_distance, best = min(candidates)

            # Critical V6 rule: never cross meaningful ink.
            safe = barrier_high < 0.035 and barrier_mean < 0.20
            microscopic = area < max(8, min_region_area // 4)
            very_similar = color_distance < 12

            if safe and (microscopic or very_similar):
                result[result == rid] = best
                changed = True

        result = relabel(result)
        if not changed:
            break

    return result



def adaptive_merge_regions(
    rgb: np.ndarray,
    labels: np.ndarray,
    barrier_strength: np.ndarray,
    detail_map: np.ndarray,
    min_region_area: int,
    experience_mode: str,
    target_regions: int,
) -> np.ndarray:
    """
    Simplify low-detail regions while protecting expressive/detail-rich areas.
    The function never merges across a strong ink boundary.
    """
    result = labels.copy()
    mode_area_factor = {
        "relaxed": 2.2,
        "balanced": 1.25,
        "detailed": 0.80,
    }.get(experience_mode, 1.25)

    max_passes = 9 if experience_mode == "relaxed" else 7
    for _ in range(max_passes):
        active = result[result > 0]
        ids, counts = np.unique(active, return_counts=True)
        areas = {int(i): int(c) for i, c in zip(ids, counts)}
        if not areas:
            return result

        means = {rid: rgb[result == rid].mean(axis=0) for rid in areas}
        details = {
            rid: float(detail_map[result == rid].mean())
            for rid in areas
        }
        graph = adjacency(result)
        changed = False

        region_pressure = max(0.0, (len(areas) - target_regions) / max(1, target_regions))

        for rid in sorted(areas, key=areas.get):
            area = areas[rid]
            detail = details[rid]
            adaptive_limit = int(
                min_region_area
                * mode_area_factor
                * (1.75 - 1.30 * detail)
                * (1.0 + min(1.25, region_pressure))
            )
            if area >= max(8, adaptive_limit):
                continue

            neighbors = list(graph.get(rid, []))
            if not neighbors:
                continue

            candidates = []
            for neighbor in neighbors:
                boundary = shared_boundary(result, rid, neighbor)
                if not boundary.any():
                    continue

                barrier_mean = float(barrier_strength[boundary].mean())
                barrier_high = float((barrier_strength[boundary] >= 0.55).mean())
                color_distance = float(np.linalg.norm(means[rid] - means[neighbor]))
                neighbor_detail = details.get(neighbor, 0.0)

                # Prefer a similarly colored, larger, lower-detail neighbor.
                score = (
                    color_distance
                    + barrier_mean * 120.0
                    + barrier_high * 220.0
                    + max(0.0, neighbor_detail - detail) * 22.0
                    - min(18.0, areas.get(neighbor, 0) ** 0.5 / 6.0)
                )
                candidates.append(
                    (score, barrier_mean, barrier_high, color_distance, neighbor)
                )

            if not candidates:
                continue

            _, barrier_mean, barrier_high, color_distance, best = min(candidates)
            safe_boundary = barrier_high < 0.045 and barrier_mean < 0.24
            high_detail = detail > 0.62
            color_limit = 11.0 if high_detail else (20.0 if experience_mode == "relaxed" else 16.0)

            if safe_boundary and color_distance <= color_limit:
                result[result == rid] = best
                changed = True

        result = relabel(result)
        if not changed:
            break

    return result


def region_means(
    rgb: np.ndarray,
    labels: np.ndarray,
    region_ids: List[int],
) -> Dict[int, np.ndarray]:
    return {
        rid: rgb[labels == rid].mean(axis=0)
        for rid in region_ids
    }


def assign_region_palette(
    rgb: np.ndarray,
    labels: np.ndarray,
    region_ids: List[int],
    color_count: int,
) -> Tuple[Dict[int, int], np.ndarray, Dict[int, np.ndarray]]:
    """
    Build a weighted palette in Lab space and preserve each region's original mean color.
    Large regions influence the palette more than tiny regions, while dark shades are
    protected from collapsing into a single black swatch.
    """
    means = region_means(rgb, labels, region_ids)
    areas = {rid: int((labels == rid).sum()) for rid in region_ids}

    region_rgb = np.array([means[rid] for rid in region_ids], dtype=np.float32)
    region_lab = color.rgb2lab(
        np.clip(region_rgb.reshape(-1, 1, 3) / 255.0, 0, 1)
    ).reshape(-1, 3).astype(np.float32)

    # Weight large regions, but cap duplication so they do not dominate completely.
    weighted_samples = []
    for index, rid in enumerate(region_ids):
        repeats = max(1, min(18, int(round((areas[rid] ** 0.5) / 10.0))))
        weighted_samples.extend([region_lab[index]] * repeats)

    samples = np.array(weighted_samples, dtype=np.float32)
    k = min(max(1, color_count), len(region_ids), len(samples))
    criteria = (
        cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER,
        120,
        0.10,
    )
    _, _, centers_lab = cv2.kmeans(
        samples,
        k,
        None,
        criteria,
        12,
        cv2.KMEANS_PP_CENTERS,
    )

    # Add representative dark shades when the artwork contains them, preventing
    # charcoal, purple-black, warm-black, and blue-black from all becoming pure black.
    dark_indices = np.where(region_lab[:, 0] < 34)[0]
    if len(dark_indices) >= 3:
        dark_samples = region_lab[dark_indices]
        dark_k = min(5, max(2, color_count // 10), len(dark_samples))
        _, _, dark_centers = cv2.kmeans(
            dark_samples.astype(np.float32),
            dark_k,
            None,
            criteria,
            8,
            cv2.KMEANS_PP_CENTERS,
        )
        centers_lab = np.vstack([centers_lab, dark_centers])

    # Remove near-duplicate Lab centers.
    unique_centers = []
    for center in centers_lab:
        if not unique_centers or min(
            float(np.linalg.norm(center - existing))
            for existing in unique_centers
        ) >= 3.0:
            unique_centers.append(center)
    centers_lab = np.array(unique_centers[:color_count], dtype=np.float32)

    centers_rgb = color.lab2rgb(
        centers_lab.reshape(-1, 1, 3)
    ).reshape(-1, 3)
    centers_rgb = np.clip(np.round(centers_rgb * 255), 0, 255).astype(np.uint8)

    mapping: Dict[int, int] = {}
    for index, rid in enumerate(region_ids):
        distances = np.linalg.norm(centers_lab - region_lab[index], axis=1)
        mapping[rid] = int(np.argmin(distances))

    return mapping, centers_rgb, means


def repair_region_coverage(
    labels: np.ndarray,
    min_component_area: int = 8,
) -> np.ndarray:
    """
    Guarantee that every canvas pixel belongs to a usable connected region.

    1. Fill all zero/orphan pixels from the nearest labeled pixel.
    2. Split disconnected islands into independent labels.
    3. Merge microscopic components into the nearest neighboring component.
    """
    repaired = labels.astype(np.int32).copy()
    h, w = repaired.shape

    positive = repaired > 0
    if not positive.any():
        return repaired

    # Fill every orphan pixel using the nearest already-labeled pixel.
    missing = ~positive
    if missing.any():
        _dist, indices = ndi.distance_transform_edt(
            missing,
            return_distances=True,
            return_indices=True,
        )
        repaired[missing] = repaired[
            indices[0][missing],
            indices[1][missing],
        ]

    # Split disconnected islands so every visible shape becomes independently
    # selectable and progress totals match the rendered SVG.
    split = np.zeros_like(repaired, dtype=np.int32)
    next_id = 1
    for rid in [int(v) for v in np.unique(repaired) if v > 0]:
        components = measure.label(repaired == rid, connectivity=2)
        for cid in [int(v) for v in np.unique(components) if v > 0]:
            split[components == cid] = next_id
            next_id += 1

    # Merge only microscopic fragments. Never discard them.
    kernel = np.ones((3, 3), np.uint8)
    for _ in range(4):
        ids, counts = np.unique(split[split > 0], return_counts=True)
        tiny_ids = [
            int(rid)
            for rid, count in zip(ids, counts)
            if int(count) < max(2, min_component_area)
        ]
        if not tiny_ids:
            break

        changed = False
        for rid in tiny_ids:
            mask = split == rid
            if not mask.any():
                continue
            dilated = cv2.dilate(mask.astype(np.uint8), kernel, iterations=1).astype(bool)
            neighbors = split[dilated & ~mask]
            neighbors = neighbors[neighbors > 0]
            if neighbors.size:
                neighbor_ids, neighbor_counts = np.unique(neighbors, return_counts=True)
                target = int(neighbor_ids[np.argmax(neighbor_counts)])
                split[mask] = target
                changed = True
        if not changed:
            break

    # Relabel connected components one last time to keep IDs compact and valid.
    final = np.zeros_like(split, dtype=np.int32)
    next_id = 1
    for rid in [int(v) for v in np.unique(split) if v > 0]:
        components = measure.label(split == rid, connectivity=2)
        for cid in [int(v) for v in np.unique(components) if v > 0]:
            final[components == cid] = next_id
            next_id += 1

    # Final safety fill: no zero pixels are permitted.
    missing = final == 0
    if missing.any():
        _dist, indices = ndi.distance_transform_edt(
            missing,
            return_distances=True,
            return_indices=True,
        )
        final[missing] = final[
            indices[0][missing],
            indices[1][missing],
        ]

    return final



def _compact_connected_labels(labels: np.ndarray) -> np.ndarray:
    """Return compact labels where every ID represents exactly one connected island."""
    compact = np.zeros_like(labels, dtype=np.int32)
    next_id = 1
    for rid in [int(v) for v in np.unique(labels) if v > 0]:
        components = measure.label(labels == rid, connectivity=2)
        for cid in [int(v) for v in np.unique(components) if v > 0]:
            compact[components == cid] = next_id
            next_id += 1
    return compact


def _best_neighbor_for_merge(labels: np.ndarray, rid: int) -> int | None:
    """Choose the neighboring region sharing the greatest border with rid."""
    mask = labels == rid
    if not mask.any():
        return None

    kernel = np.ones((3, 3), np.uint8)
    border = (
        cv2.dilate(mask.astype(np.uint8), kernel, iterations=1).astype(bool)
        & ~mask
    )
    neighbors = labels[border]
    neighbors = neighbors[neighbors > 0]
    neighbors = neighbors[neighbors != rid]
    if neighbors.size == 0:
        return None

    neighbor_ids, border_counts = np.unique(neighbors, return_counts=True)
    region_sizes = {
        int(nid): int((labels == int(nid)).sum())
        for nid in neighbor_ids
    }

    # Shared border is primary; larger stable neighbor breaks ties.
    ranked = sorted(
        (
            (int(border_count), region_sizes[int(nid)], int(nid))
            for nid, border_count in zip(neighbor_ids, border_counts)
        ),
        reverse=True,
    )
    return ranked[0][2] if ranked else None


def region_mask_validation(
    mask: np.ndarray,
    tolerance: float,
    min_area: int,
    min_label_radius: float,
) -> dict:
    """Validate one candidate region before any SVG or palette count is exported."""
    area = int(mask.sum())
    result = {
        "area": area,
        "connected": False,
        "hasInteriorPoint": False,
        "labelRadius": 0.0,
        "vectorizable": False,
        "pathLength": 0,
        "valid": False,
        "reasons": [],
    }

    if area <= 0:
        result["reasons"].append("empty")
        return result

    components = measure.label(mask, connectivity=2)
    component_count = int(components.max())
    result["connected"] = component_count == 1
    if component_count != 1:
        result["reasons"].append("disconnected")

    x, y, radius = label_point(mask)
    result["labelRadius"] = float(radius)
    iy = int(round(y))
    ix = int(round(x))
    interior_ok = (
        0 <= iy < mask.shape[0]
        and 0 <= ix < mask.shape[1]
        and bool(mask[iy, ix])
        and radius > 0
    )
    result["hasInteriorPoint"] = interior_ok
    if not interior_ok:
        result["reasons"].append("no_interior_point")

    if area < min_area:
        result["reasons"].append("area_too_small")
    if radius < min_label_radius:
        result["reasons"].append("label_does_not_fit")

    poly = mask_polygon(mask, tolerance)
    path = polygon_path(poly) if poly is not None else ""
    result["vectorizable"] = bool(path)
    result["pathLength"] = len(path)
    if not path:
        result["reasons"].append("not_vectorizable")

    result["valid"] = (
        result["connected"]
        and result["hasInteriorPoint"]
        and area >= min_area
        and radius >= min_label_radius
        and result["vectorizable"]
    )
    return result


def _cropped_region_mask(labels: np.ndarray, rid: int) -> tuple[np.ndarray, tuple[slice, slice]] | tuple[None, None]:
    """Return a tight crop for one region so validation never scans the full canvas."""
    ys, xs = np.where(labels == rid)
    if ys.size == 0:
        return None, None
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    # One-pixel padding keeps contour/vectorization logic stable at crop edges.
    y0 = max(0, y0 - 1)
    x0 = max(0, x0 - 1)
    y1 = min(labels.shape[0], y1 + 1)
    x1 = min(labels.shape[1], x1 + 1)
    slc = (slice(y0, y1), slice(x0, x1))
    return labels[slc] == rid, slc


def _best_neighbor_for_merge_fast(labels: np.ndarray, rid: int) -> int | None:
    """Find the best adjacent region using only a tight local crop."""
    mask, slc = _cropped_region_mask(labels, rid)
    if mask is None or slc is None or not mask.any():
        return None

    y0 = max(0, slc[0].start - 1)
    y1 = min(labels.shape[0], slc[0].stop + 1)
    x0 = max(0, slc[1].start - 1)
    x1 = min(labels.shape[1], slc[1].stop + 1)
    local = labels[y0:y1, x0:x1]
    local_mask = local == rid

    kernel = np.ones((3, 3), np.uint8)
    border = cv2.dilate(local_mask.astype(np.uint8), kernel, iterations=1).astype(bool) & ~local_mask
    neighbors = local[border]
    neighbors = neighbors[(neighbors > 0) & (neighbors != rid)]
    if neighbors.size == 0:
        return None

    neighbor_ids, border_counts = np.unique(neighbors, return_counts=True)
    # Shared boundary is the only ranking criterion here. Avoid repeatedly
    # scanning the entire label image to calculate every neighbor's size.
    return int(neighbor_ids[int(np.argmax(border_counts))])


def _fast_region_screen(mask: np.ndarray, min_area: int, min_label_radius: float) -> dict:
    """Cheap repair-pass validation. Expensive polygon validation happens once at the end."""
    area = int(mask.sum())
    if area <= 0:
        return {"valid": False, "area": 0, "labelRadius": 0.0, "reasons": ["empty"]}

    _x, _y, radius = label_point(mask)
    reasons: list[str] = []
    if area < min_area:
        reasons.append("area_too_small")
    if radius < min_label_radius:
        reasons.append("label_does_not_fit")

    return {
        "valid": not reasons,
        "area": area,
        "labelRadius": float(radius),
        "reasons": reasons,
    }


def guarantee_paintable_regions(
    labels: np.ndarray,
    tolerance: float,
    min_area: int,
    min_label_radius: float = 2.25,
    max_passes: int = 8,
) -> tuple[np.ndarray, dict]:
    """
    Fast validation/repair pass.

    Version 12.2 avoids full-canvas distance transforms, contour conversion, and
    neighbor-size scans for every region on every pass. Repair decisions use
    tight crops; full vectorizability validation runs only once after repair.
    """
    repaired = _compact_connected_labels(labels.astype(np.int32))
    history: list[dict] = []
    merged_total = 0

    for pass_index in range(1, max_passes + 1):
        region_ids = [int(v) for v in np.unique(repaired) if v > 0]
        invalid: list[tuple[int, dict]] = []

        for rid in region_ids:
            mask, _slc = _cropped_region_mask(repaired, rid)
            if mask is None:
                continue
            validation = _fast_region_screen(
                mask,
                min_area=min_area,
                min_label_radius=min_label_radius,
            )
            if not validation["valid"]:
                invalid.append((rid, validation))

        history.append(
            {
                "pass": pass_index,
                "regions": len(region_ids),
                "invalid": len(invalid),
            }
        )
        if not invalid:
            break

        invalid.sort(key=lambda item: (item[1]["area"], item[1]["labelRadius"]))
        changed = False

        for rid, _validation in invalid:
            if not np.any(repaired == rid):
                continue
            target = _best_neighbor_for_merge_fast(repaired, rid)
            if target is None:
                continue
            repaired[repaired == rid] = target
            merged_total += 1
            changed = True

        if not changed:
            break

        # Merging an adjacent connected region into its neighbor preserves
        # connectivity; defer expensive relabeling until all repair passes end.

    repaired = _compact_connected_labels(repaired)

    final_validations: dict[int, dict] = {}
    remaining_invalid: list[dict] = []
    final_region_ids = [int(v) for v in np.unique(repaired) if v > 0]

    for rid in final_region_ids:
        mask, _slc = _cropped_region_mask(repaired, rid)
        if mask is None:
            continue
        validation = region_mask_validation(
            mask,
            tolerance=tolerance,
            min_area=min_area,
            min_label_radius=min_label_radius,
        )
        final_validations[rid] = validation
        if not validation["valid"]:
            remaining_invalid.append({"region": rid, **validation})

    report = {
        "engine": "Guaranteed Paintability Engine 12.2 Fast Crop Validator",
        "initialRegions": int(len(np.unique(labels[labels > 0]))),
        "finalRegions": len(final_region_ids),
        "mergedRegions": merged_total,
        "minimumArea": int(min_area),
        "minimumLabelRadius": float(min_label_radius),
        "passes": history,
        "remainingInvalid": remaining_invalid,
        "passed": not remaining_invalid,
        "optimization": "tight-crop validation; deferred polygon/vector validation",
    }
    return repaired, report


def simulate_complete_painting(
    records: list[dict],
    palette: list[dict],
    blank_svg: str,
    regions: np.ndarray,
    region_labels: dict[str, int],
) -> dict:
    """
    Run a deterministic pre-export customer simulation.

    This verifies that every counted region has one SVG path, one matching
    number, a valid interior coordinate, a palette entry, and can advance
    progress exactly once. Export is blocked unless the result is 100%.
    """
    failures: list[dict] = []
    region_ids = [str(record.get("regionId")) for record in records]
    unique_ids = set(region_ids)

    if len(unique_ids) != len(region_ids):
        failures.append({"type": "duplicate_region_ids"})

    color_ids = {str(item.get("colorId")) for item in palette}
    record_color_counts: dict[str, int] = {}
    for record in records:
        color_id = str(record.get("colorId"))
        record_color_counts[color_id] = record_color_counts.get(color_id, 0) + 1
        if color_id not in color_ids:
            failures.append(
                {
                    "type": "missing_palette_color",
                    "regionId": record.get("regionId"),
                    "colorId": color_id,
                }
            )

    try:
        root = ET.fromstring(blank_svg)
    except ET.ParseError as exc:
        return {
            "passed": False,
            "simulated": 0,
            "expected": len(records),
            "failures": [{"type": "invalid_svg", "message": str(exc)}],
        }

    namespace = "{http://www.w3.org/2000/svg}"
    paths = {
        element.get("id"): element
        for element in root.findall(f".//{namespace}path")
        if element.get("class") == "paint-region"
    }
    labels = {
        element.get("data-region-id"): element
        for element in root.findall(f".//{namespace}text")
        if element.get("class") == "region-number"
    }

    simulated = 0
    simulated_ids: set[str] = set()

    for record in records:
        region_id = str(record.get("regionId"))
        color_id = str(record.get("colorId"))
        path = paths.get(region_id)
        label = labels.get(region_id)
        label_id = region_labels.get(region_id)

        reasons: list[str] = []
        if path is None or not (path.get("d") or "").strip():
            reasons.append("missing_svg_path")
        if label is None:
            reasons.append("missing_number")
        elif (label.text or "").strip() != color_id:
            reasons.append("wrong_number")
        if label_id is None:
             reasons.append("missing_region_label")
        else:
           label_data = record.get("label", {})
           x = int(round(float(label_data.get("x", -1))))
           y = int(round(float(label_data.get("y", -1))))
           if not (
                0 <= y < regions.shape[0]
                and 0 <= x < regions.shape[1]
                and int(regions[y, x]) == label_id
       ):
                reasons.append("number_not_inside_region")

        if region_id in simulated_ids:
            reasons.append("duplicate_simulation")
        if reasons:
            failures.append(
                {
                    "type": "region_failure",
                    "regionId": region_id,
                    "colorId": color_id,
                    "reasons": reasons,
                }
            )
            continue

        simulated_ids.add(region_id)
        simulated += 1

    palette_counts = {
        color_id: sum(
            1
            for record in records
            if str(record.get("colorId")) == color_id
        )
        for color_id in color_ids
    }

    passed = (
        not failures
        and simulated == len(records)
        and len(paths) >= len(records)
        and len(labels) >= len(records)
        and sum(palette_counts.values()) == len(records)
    )
    return {
        "engine": "Pre-Export Customer Simulation 9.0",
        "passed": passed,
        "expected": len(records),
        "simulated": simulated,
        "svgPaintPaths": len(paths),
        "svgNumberLabels": len(labels),
        "paletteCounts": palette_counts,
        "failures": failures,
    }


def region_coverage_stats(labels: np.ndarray) -> dict:
    total = int(labels.size)
    assigned = int((labels > 0).sum())
    orphan = total - assigned
    return {
        "totalPixels": total,
        "assignedPixels": assigned,
        "orphanPixels": orphan,
        "coveragePercent": round((assigned / max(1, total)) * 100.0, 6),
    }


def mask_polygon(mask: np.ndarray, tolerance: float) -> Polygon | None:
    polygons: List[Polygon] = []

    for contour in measure.find_contours(mask.astype(np.uint8), 0.5):
        if len(contour) < 8:
            continue

        coords = [(float(c), float(r)) for r, c in contour]
        poly = Polygon(coords)
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.is_empty or poly.area < 4:
            continue

        poly = poly.simplify(tolerance, preserve_topology=True)
        if not poly.is_empty and poly.area >= 4:
            polygons.append(poly)

    if not polygons:
        return None

    merged = unary_union(polygons)
    return merged


def polygon_path(poly) -> str:
    parts: List[str] = []

    def add_polygon(item) -> None:
        def add_ring(coords) -> None:
            coords = list(coords)
            if len(coords) < 4:
                return
            parts.append(f"M {coords[0][0]:.2f} {coords[0][1]:.2f}")
            parts.extend(f"L {x:.2f} {y:.2f}" for x, y in coords[1:])
            parts.append("Z")

        add_ring(item.exterior.coords)
        for ring in item.interiors:
            add_ring(ring.coords)

    if poly.geom_type == "Polygon":
        add_polygon(poly)
    elif poly.geom_type == "MultiPolygon":
        for item in poly.geoms:
            add_polygon(item)
    elif poly.geom_type == "GeometryCollection":
        for item in poly.geoms:
            if item.geom_type == "Polygon":
                add_polygon(item)
            elif item.geom_type == "MultiPolygon":
                for sub_item in item.geoms:
                    add_polygon(sub_item)

    return " ".join(parts)



def trace_ink_paths(
    barrier: np.ndarray,
    fg: np.ndarray,
    min_length: int = 10,
) -> List[str]:
    ink = barrier & fg
    ink = morphology.skeletonize(ink)
    contours, _ = cv2.findContours(
        ink.astype(np.uint8) * 255,
        cv2.RETR_LIST,
        cv2.CHAIN_APPROX_NONE,
    )

    paths: List[str] = []
    for contour in contours:
        if len(contour) < min_length:
            continue
        epsilon = max(0.25, cv2.arcLength(contour, False) * 0.0018)
        approx = cv2.approxPolyDP(contour, epsilon, False)
        points = approx.reshape(-1, 2)
        if len(points) < 2:
            continue
        path = [f"M {points[0][0]:.2f} {points[0][1]:.2f}"]
        path.extend(f"L {x:.2f} {y:.2f}" for x, y in points[1:])
        paths.append(" ".join(path))
    return paths



def build_interactive_player(
    output_dir: Path,
    records: list,
    palette: list,
    blank_svg: str,
    reference_svg: str,
) -> None:
    """Create a self-contained customer player plus a private review lab."""
    payload_regions = json.dumps(records)
    payload_palette = json.dumps(palette)

    import base64
    import io

    reveal_path = output_dir / "completion_reveal.png"
    if not reveal_path.exists():
        reveal_path = output_dir / "completion_original.png"
    reveal_data = Image.open(reveal_path)
    reveal_buffer = io.BytesIO()
    reveal_data.save(reveal_buffer, format="PNG")
    reveal_uri = "data:image/png;base64," + base64.b64encode(
        reveal_buffer.getvalue()
    ).decode("ascii")

    masterpiece_path = output_dir / "finished_masterpiece.png"
    if not masterpiece_path.exists():
        masterpiece_path = output_dir / "completion_original.png"
    completion_data = Image.open(masterpiece_path)
    completion_buffer = io.BytesIO()
    completion_data.save(completion_buffer, format="PNG")
    completion_uri = "data:image/png;base64," + base64.b64encode(
        completion_buffer.getvalue()
    ).decode("ascii")

    reference_uri = (
        "data:image/svg+xml;base64,"
        + base64.b64encode(reference_svg.encode("utf-8")).decode("ascii")
    )

    html = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Euqilegna Interactive Paint Event Review Lab</title>
<style>
:root{--accent:#9c4a22;--accent2:#d47b48;--bg:#fff8f1;--panel:#fff;--line:#ead6c5;--text:#2b190f;--ok:#216e39;--warn:#9a5b00}
*{box-sizing:border-box}
body{margin:0;font-family:Arial,sans-serif;background:var(--bg);color:var(--text)}
header{padding:16px 20px;background:#fff;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap}
header h1{margin:0;font-family:Georgia,serif;font-size:23px}
.mode-switch{display:flex;gap:8px}
.app{display:grid;grid-template-columns:minmax(0,1fr) 320px;min-height:calc(100vh - 68px)}
.canvas-wrap{position:relative;overflow:hidden;background:#f3ede8;display:flex;align-items:center;justify-content:center;padding:18px}
#artboard{position:relative;width:min(100%,1000px);aspect-ratio:__ASPECT_RATIO__;background:#fff;box-shadow:0 10px 30px #0002;overflow:hidden;touch-action:none;cursor:crosshair;user-select:none;-webkit-user-select:none}#artboard.dragging{cursor:grabbing}#canvasContent{position:absolute;inset:0;width:100%;height:100%;transform-origin:center center;will-change:transform;backface-visibility:hidden;-webkit-font-smoothing:antialiased;text-rendering:geometricPrecision;shape-rendering:geometricPrecision}
#svgHost,#completion,#referenceOverlay,#masterReveal{position:absolute;inset:0;width:100%;height:100%}#svgHost{z-index:2;transition:opacity .35s ease,filter .35s ease}#masterReveal{z-index:4;pointer-events:none;overflow:hidden;display:block}#masterReveal image{image-rendering:auto}
#svgHost svg{width:100%;height:100%;display:block;shape-rendering:geometricPrecision;text-rendering:geometricPrecision}
#completion,#referenceOverlay{object-fit:contain;pointer-events:none;transition:opacity .5s ease}
#completion{opacity:0;z-index:20;background:#fff;clip-path:inset(0 100% 0 0);transition:clip-path 1.8s cubic-bezier(.22,.61,.36,1),opacity .35s ease}
#referenceOverlay{opacity:0;z-index:3;transition:opacity .5s ease}
.sidebar{background:#fff;border-left:1px solid var(--line);padding:18px;overflow:auto}
.progress{height:12px;border-radius:999px;background:#eee;overflow:hidden;margin:8px 0 14px}
#bar{height:100%;width:0;background:var(--accent);transition:width .25s}
.palette{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.swatch{aspect-ratio:1;border-radius:8px;border:2px solid transparent;cursor:pointer;font-weight:700;font-size:12px}
.swatch.active{border-color:#111;transform:scale(1.05);box-shadow:0 0 0 3px #d9d9d9}.swatch.complete{opacity:.48;filter:grayscale(.35)}.swatch.locked{opacity:.55;cursor:not-allowed;filter:saturate(.55)}.swatch.complete::after{content:'✓';position:absolute;right:5px;top:3px;background:#216e39;color:#fff;width:18px;height:18px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:12px}.swatch{position:relative;overflow:hidden}.swatch .swatch-number{position:relative;z-index:2}.swatch .swatch-progress{position:absolute;left:0;bottom:0;height:6px;background:#ffffffaa;z-index:1}.swatch .swatch-count{position:absolute;left:4px;bottom:7px;font-size:10px;line-height:1;background:#0008;color:#fff;padding:2px 4px;border-radius:4px;z-index:3}
.controls{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
button{border:0;border-radius:8px;padding:10px 12px;font-weight:700;cursor:pointer}
.primary{background:var(--accent);color:#fff}.secondary{background:#eee4dc}.danger{background:#f8e3e3;color:#8d1717}.success{background:var(--ok);color:#fff}
#message{font-weight:700;min-height:24px}
.hint{font-size:13px;color:#765d4c;line-height:1.4}
.review-card{border:1px solid var(--line);border-radius:10px;padding:12px;margin-top:14px;background:#fffaf6}
.review-card h3{margin:0 0 10px}
.checklist label{display:flex;gap:9px;align-items:flex-start;margin:8px 0;font-weight:400}
textarea{width:100%;min-height:90px;border:1px solid #ccb7a7;border-radius:8px;padding:9px}
.badge{display:inline-block;padding:5px 8px;border-radius:999px;font-weight:700;font-size:12px;background:#eee4dc}
.hidden{display:none!important}
.customer-only.review-mode{display:none!important}
.review-only.customer-mode{display:none!important}
.zoom-readout{min-width:62px;text-align:center;padding:9px 4px}
.region-info{font-family:Consolas,monospace;font-size:12px;background:#241f1c;color:#f7ede5;padding:9px;border-radius:8px;white-space:pre-wrap}.region-number{transition:opacity .2s ease,font-size .2s ease;text-rendering:geometricPrecision;paint-order:stroke;stroke-linejoin:round}.region-number.selected-number{opacity:1!important;font-weight:900;paint-order:stroke;stroke:#fff;stroke-width:5px;stroke-linejoin:round;filter:drop-shadow(0 0 2px #fff)}.region-number.unrelated-number{opacity:0!important}.region-number.painted-number{opacity:0!important}
.hint-marker-ring{fill:#fff7;stroke:#ff2f92;stroke-width:3.2px;vector-effect:non-scaling-stroke;pointer-events:none;animation:hintPulse 1s ease-in-out infinite alternate}
.hint-marker-core{fill:#ff2f92;stroke:#fff;stroke-width:2px;vector-effect:non-scaling-stroke;pointer-events:none}
.hint-marker-text{fill:#fff;stroke:#561033;stroke-width:1.5px;paint-order:stroke;font-family:Arial,sans-serif;font-weight:900;text-anchor:middle;dominant-baseline:middle;pointer-events:none}
@keyframes hintPulse{from{opacity:.5}to{opacity:1}}
@media(max-width:820px){.app{grid-template-columns:1fr}.sidebar{border-left:0;border-top:1px solid var(--line)}}
</style>
</head>
<body class="review-mode">
<header>
  <h1>Euqilegna Interactive Paint Event</h1>
  <div class="mode-switch">
    <button id="reviewModeBtn" class="primary">Private Review Lab</button>
    <button id="customerModeBtn" class="secondary">Preview as Customer</button>
  </div>
</header>

<div class="app">
  <main class="canvas-wrap">
    <div id="artboard">
      <div id="canvasContent">
      <div id="svgHost">__BLANK_SVG__</div>
      <svg id="masterReveal" aria-label="Original artwork revealed through painted regions"></svg>
      <img id="revealSource" src="__REVEAL_URI__" alt="Original artwork reveal source" style="display:none">
      <img id="referenceOverlay" src="__REFERENCE_URI__" alt="Reference overlay">
      <img id="completion" src="__COMPLETION_URI__" alt="Completed artwork"><div id="completionMessage" style="position:absolute;inset:auto 20px 20px 20px;z-index:8;background:#ffffffee;border:1px solid #ead6c5;border-radius:14px;padding:14px 16px;text-align:center;font-weight:800;opacity:0;transform:translateY(12px);transition:opacity .5s ease,transform .5s ease;pointer-events:none">Artwork complete — enjoy the full-color reveal.</div>
      <div id="showcasePanel" style="position:absolute;z-index:30;left:50%;bottom:22px;transform:translate(-50%,18px);display:flex;gap:8px;flex-wrap:wrap;justify-content:center;opacity:0;pointer-events:none;transition:opacity .45s ease,transform .45s ease">
        <button id="paintAgainBtn" class="primary">Paint Again</button>
        <button id="downloadArtworkBtn" class="secondary">Download Finished Artwork</button>
        <button id="downloadShowcaseBtn" class="secondary">Download Showcase Card</button>
        <button id="shareArtworkBtn" class="secondary">Share Artwork</button>
      </div>
      </div>
    </div>
  </main>

  <aside class="sidebar">
    <div id="message">Choose a play style, then begin testing.</div>

    <div class="review-only">
      <span id="difficultyBadge" class="badge">Analyzing difficulty…</span>
      <div class="review-card">
        <h3>Customer play style</h3>
        <label><input type="radio" name="playMode" value="relaxed" checked> <strong>Relaxed:</strong>&nbsp;tap any region and it fills with the correct color.</label>
        <label><input type="radio" name="playMode" value="guided"> <strong>Guided:</strong>&nbsp;choose a color, then paint matching regions.</label>
        <label><input type="radio" name="playMode" value="challenge"> <strong>Challenge:</strong>&nbsp;wrong colors give feedback and do not fill.</label>
      </div>
    </div>

    <div><strong id="count">0 / 0 regions painted</strong></div><div id="colorStatus" class="hint" style="margin:6px 0 4px"></div><div id="selectedColorStatus" class="hint" style="font-weight:700;margin-bottom:10px"></div>
    <div class="progress"><div id="bar"></div></div>

    <h3>Paint palette</h3>
    <p class="hint">All unpainted regions display their number references. Use Focus Selected Number to temporarily show only one number.</p>
    <div id="palette" class="palette"></div>

    <div class="review-card">
  <h3>Need a hint?</h3>
  <p class="hint">Hints stay hidden unless requested. Reveal one remaining section for the selected number.</p>
  <button id="hintOneBtn" class="secondary">Show One Hint</button>
  <button id="paintHintBtn" class="secondary">Complete Highlighted Section</button>
  <button id="clearHintBtn" class="secondary">Clear Hint</button>
  <button id="resolveHiddenBtn" class="secondary">Resolve Hidden Sections</button>
  <button id="skipColorBtn" class="secondary">Skip This Color for Now</button>
</div>
<div class="controls">
      <button id="saveBtn" class="secondary">Save Progress</button>
      <button id="exportProgressBtn" class="secondary">Download Progress</button>
      <button id="importProgressBtn" class="secondary">Load Progress</button>
      <input id="progressImportFile" type="file" accept="application/json" style="display:none">
      <button id="resetBtn" class="secondary">Reset</button>
    </div>

    <div class="controls">
      <button id="zoomOut" class="secondary">−</button>
      <span id="zoomReadout" class="zoom-readout">100%</span>
      <button id="zoomIn" class="secondary">+</button>
      <button id="zoomReset" class="secondary">Reset View</button>
      <button id="centerSelected" class="secondary">Center Selected Color</button><button id="toggleNumberFocus" class="secondary">Show All Numbers</button>
    </div>
    <p class="hint">Looking for a number? Zoom in with the +/− buttons or by scrolling over the picture, then drag the picture to move around. A quick tap still paints as usual.</p>

    <div class="review-only review-card">
      <h3>Review tools</h3>
      <div class="controls">
        <button id="toggleReference" class="secondary">Show Reference Overlay</button>
        <button id="inspectMode" class="secondary">Inspect Regions</button>
      </div>
      <div id="regionInfo" class="region-info">Click “Inspect Regions,” then select a region.</div>
    </div>

    <div class="review-only review-card">
      <h3>Customer Experience Checklist</h3>
      <div class="checklist" id="checklist">
        <label><input type="checkbox"> Numbers are readable at normal zoom.</label>
        <label><input type="checkbox"> Important facial and object features are preserved.</label>
        <label><input type="checkbox"> No region feels unreasonably tiny or frustrating.</label>
        <label><input type="checkbox"> Relaxed mode feels easy and enjoyable.</label>
        <label><input type="checkbox"> Guided mode clearly highlights matching regions.</label>
        <label><input type="checkbox"> Progress saves and restores correctly.</label>
        <label><input type="checkbox"> The experience works comfortably on a phone-size window.</label>
        <label><input type="checkbox"> The completed reveal is correctly fitted and not cropped.</label>
      </div>
      <label for="reviewNotes"><strong>Review notes</strong></label>
      <textarea id="reviewNotes" placeholder="Record anything that needs improvement before publishing."></textarea>
      <div class="controls">
        <button id="saveReviewBtn" class="secondary">Save Review Notes</button>
        <button id="downloadReport" class="secondary">Download QA Report</button>
        <button id="approveBtn" class="success">Approve for Website</button>
      </div>
      <p id="approvalMessage" class="hint">Approval remains private and local until you choose to upload the package.</p>
    </div>

    <p class="hint customer-only">Your progress is saved automatically. You can leave and come back later without losing your work.</p>
  </aside>
</div>

<script>
const REGIONS=__REGIONS__;
const PALETTE=__PALETTE__;
const STORAGE_KEY='euqilegna-paint-event-' + location.pathname;
const REVIEW_KEY=STORAGE_KEY+'-review';
const svgHost=document.getElementById('svgHost');
const completion=document.getElementById('completion');
const masterReveal=document.getElementById('masterReveal');
const revealSource=document.getElementById('revealSource');
const showcasePanel=document.getElementById('showcasePanel');
const referenceOverlay=document.getElementById('referenceOverlay');
const paletteHost=document.getElementById('palette');
const count=document.getElementById('count');
const bar=document.getElementById('bar');
const message=document.getElementById('message');
const artboard=document.getElementById('artboard');
const canvasContent=document.getElementById('canvasContent');
let selectedColor=PALETTE[0]?.colorId || '1';
let painted={};
let playMode='relaxed';
let inspect=false;
let referenceVisible=false;
let zoom=1;
let panX=0;
let panY=0;
let isDragging=false;
let dragStartX=0;
let dragStartY=0;
let pointerStartX=0;
let pointerStartY=0;
let hasDragged=false;
let pointerIsDown=false;
let numberFocus=false;
let hintedRegionId=null;
let completionShown=false;
let skippedColors=new Set();

try{
  const saved=JSON.parse(safeLocalGet(STORAGE_KEY)||'null');
  if(saved?.painted) painted=saved.painted;
  else if(saved && typeof saved==='object') painted=saved;
}catch(e){painted={}}


function setupMasterReveal(){
  const baseSvg=svgHost.querySelector('svg');
  if(!baseSvg || !masterReveal) return;

  const viewBox=baseSvg.getAttribute('viewBox') || `0 0 ${baseSvg.clientWidth||1000} ${baseSvg.clientHeight||1000}`;
  masterReveal.setAttribute('viewBox',viewBox);
  masterReveal.setAttribute('preserveAspectRatio','xMidYMid meet');
  masterReveal.innerHTML='';

  const defs=document.createElementNS('http://www.w3.org/2000/svg','defs');
  const clip=document.createElementNS('http://www.w3.org/2000/svg','clipPath');
  clip.setAttribute('id','paintedMasterClip');
  clip.setAttribute('clipPathUnits','userSpaceOnUse');
  defs.appendChild(clip);

  const image=document.createElementNS('http://www.w3.org/2000/svg','image');
  const parts=viewBox.trim().split(/\s+/).map(Number);
  const [vx,vy,vw,vh]=parts.length===4?parts:[0,0,1000,1000];
  image.setAttribute('x',vx);
  image.setAttribute('y',vy);
  image.setAttribute('width',vw);
  image.setAttribute('height',vh);
  image.setAttribute('preserveAspectRatio','xMidYMid meet');
  image.setAttribute('href',revealSource.src);
  image.setAttribute('clip-path','url(#paintedMasterClip)');

  masterReveal.append(defs,image);
  rebuildMasterReveal();
}

function rebuildMasterReveal(){
  const clip=masterReveal?.querySelector('#paintedMasterClip');
  if(!clip) return;
  clip.innerHTML='';

  for(const region of REGIONS){
    if(!painted[region.regionId]) continue;
    const source=document.getElementById(region.regionId);
    if(!source) continue;

    const clone=source.cloneNode(true);
    clone.removeAttribute('id');
    clone.removeAttribute('style');
    clone.removeAttribute('class');
    clone.setAttribute('fill','#000');
    clone.setAttribute('stroke','none');
    clone.setAttribute('pointer-events','none');
    clip.appendChild(clone);
  }
}

function revealPaintedRegion(regionId){
  const clip=masterReveal?.querySelector('#paintedMasterClip');
  const source=document.getElementById(regionId);
  if(!clip || !source) return;

  if(clip.querySelector(`[data-reveal-id="${regionId}"]`)) return;
  const clone=source.cloneNode(true);
  clone.removeAttribute('id');
  clone.removeAttribute('style');
  clone.removeAttribute('class');
  clone.setAttribute('data-reveal-id',regionId);
  clone.setAttribute('fill','#000');
  clone.setAttribute('stroke','none');
  clone.setAttribute('pointer-events','none');
  clip.appendChild(clone);
}

function downloadDataUrl(dataUrl,filename){
  const link=document.createElement('a');
  link.href=dataUrl;
  link.download=filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
}

async function createShowcaseCard(){
  await completion.decode?.().catch(()=>{});
  const source=completion;
  const maxWidth=1600;
  const sourceW=source.naturalWidth || 1200;
  const sourceH=source.naturalHeight || 1200;
  const scale=Math.min(1,maxWidth/sourceW);
  const artW=Math.round(sourceW*scale);
  const artH=Math.round(sourceH*scale);
  const pad=70;
  const footer=150;

  const canvas=document.createElement('canvas');
  canvas.width=artW+pad*2;
  canvas.height=artH+pad*2+footer;
  const ctx=canvas.getContext('2d');

  ctx.fillStyle='#fff8f1';
  ctx.fillRect(0,0,canvas.width,canvas.height);

  ctx.fillStyle='#ffffff';
  ctx.fillRect(pad-12,pad-12,artW+24,artH+24);
  ctx.drawImage(source,pad,pad,artW,artH);

  ctx.strokeStyle='#9c4a22';
  ctx.lineWidth=6;
  ctx.strokeRect(pad-16,pad-16,artW+32,artH+32);

  ctx.fillStyle='#2b190f';
  ctx.textAlign='center';
  ctx.font='700 42px Georgia, serif';
  ctx.fillText('My Completed Paint-by-Number Artwork',canvas.width/2,artH+pad+72);

  ctx.font='24px Arial, sans-serif';
  ctx.fillStyle='#6c4b3a';
  ctx.fillText('Created with Euqilegna Paint Studio',canvas.width/2,artH+pad+112);

  return canvas;
}

async function shareFinishedArtwork(){
  const canvas=await createShowcaseCard();
  const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/png'));
  if(!blob) return;

  const file=new File([blob],'euqilegna-painted-artwork.png',{type:'image/png'});
  if(navigator.canShare?.({files:[file]})){
    await navigator.share({
      title:'My Completed Paint-by-Number Artwork',
      text:'I completed this artwork with Euqilegna Paint Studio.',
      files:[file],
    });
  }else{
    downloadDataUrl(canvas.toDataURL('image/png'),'euqilegna-painted-artwork-showcase.png');
    message.textContent='Sharing is not supported in this browser, so the showcase card was downloaded instead.';
  }
}

function currentPlayMode(){
  return document.querySelector('input[name="playMode"]:checked')?.value || 'relaxed';
}


function ensureSelectionPattern(){
  const svg=svgHost.querySelector('svg');
  if(!svg) return;
  let defs=svg.querySelector('defs');
  if(!defs){
    defs=document.createElementNS('http://www.w3.org/2000/svg','defs');
    svg.insertBefore(defs,svg.firstChild);
  }
  if(svg.querySelector('#selectedGrayPixelPattern')) return;

  const pattern=document.createElementNS('http://www.w3.org/2000/svg','pattern');
  pattern.setAttribute('id','selectedGrayPixelPattern');
  pattern.setAttribute('patternUnits','userSpaceOnUse');
  pattern.setAttribute('width','12');
  pattern.setAttribute('height','12');

  const background=document.createElementNS('http://www.w3.org/2000/svg','rect');
  background.setAttribute('width','12');
  background.setAttribute('height','12');
  background.setAttribute('fill','#d8d8d8');

  const sheen=document.createElementNS('http://www.w3.org/2000/svg','path');
  sheen.setAttribute('d','M0 12 L12 0');
  sheen.setAttribute('stroke','#eeeeee');
  sheen.setAttribute('stroke-width','3');
  sheen.setAttribute('opacity','.55');

  pattern.append(background,sheen);
  defs.appendChild(pattern);
}



function findRegionNumberLabel(regionId){
  return svgHost.querySelector(
    `.region-number[data-region-id="${CSS.escape(String(regionId))}"]`
  );
}

function ensureEveryPaintableRegionHasNumber(){
  for(const region of REGIONS){
    if(regionIsVisuallyPaintable(region)) repairRegionNumber(region);
  }
}

function svgPointInsideShape(shape,x,y){
  try{
    const svg=shape.ownerSVGElement;
    if(!svg || typeof shape.isPointInFill!=='function') return true;
    const point=svg.createSVGPoint();
    point.x=Number(x);point.y=Number(y);
    return !!shape.isPointInFill(point);
  }catch(error){
    return true;
  }
}

function findVisibleInteriorPoint(region,shape){
  const box=shape.getBBox();
  if(!box || box.width<=0 || box.height<=0) return null;

  const candidates=[];
  if(Number.isFinite(Number(region.label?.x)) && Number.isFinite(Number(region.label?.y))){
    candidates.push([Number(region.label.x),Number(region.label.y)]);
  }
  candidates.push([box.x+box.width/2,box.y+box.height/2]);

  // Search more densely so curved bands, crescents, rings, and narrow record
  // highlights always receive a usable label and hint point.
  const fractions=[.5,.42,.58,.34,.66,.26,.74,.18,.82,.1,.9];
  for(const fy of fractions){
    for(const fx of fractions){
      candidates.push([box.x+box.width*fx,box.y+box.height*fy]);
    }
  }

  for(const [x,y] of candidates){
    if(svgPointInsideShape(shape,x,y)) return {x,y,box};
  }

  // Never remove a visible region from the painting merely because a browser
  // could not confirm isPointInFill. The compiler-provided interior point is
  // preferred; the bounding-box center is the last-resort display point.
  const fallbackX=Number.isFinite(Number(region.label?.x))
    ? Number(region.label.x)
    : box.x+box.width/2;
  const fallbackY=Number.isFinite(Number(region.label?.y))
    ? Number(region.label.y)
    : box.y+box.height/2;
  return {x:fallbackX,y:fallbackY,box,fallback:true};
}

function regionIsVisuallyPaintable(region){
  // Every exported region is paintable. Tiny regions may need zoom or a hint,
  // but they must never be silently excluded from the customer's painting.
  const shape=document.getElementById(region.regionId);
  if(!shape) return false;
  try{
    const box=shape.getBBox();
    return box.width>0 && box.height>0;
  }catch(error){
    return true;
  }
}

function repairRegionNumber(region){
  const shape=document.getElementById(region.regionId);
  if(!shape) return null;
  const point=findVisibleInteriorPoint(region,shape);
  if(!point) return null;

  let label=findRegionNumberLabel(region.regionId);
  if(!label){
    label=document.createElementNS('http://www.w3.org/2000/svg','text');
    label.setAttribute('class','region-number');
    label.setAttribute('data-region-id',String(region.regionId));
    label.setAttribute('data-color-id',String(region.colorId));
    label.setAttribute('text-anchor','middle');
    label.setAttribute('dominant-baseline','middle');
    label.setAttribute('pointer-events','none');
    shape.ownerSVGElement.appendChild(label);
  }

  label.setAttribute('x',String(point.x));
  label.setAttribute('y',String(point.y));
  label.setAttribute('font-size',String(Math.max(8,Number(region.label?.fontSize||8))));
  label.setAttribute('paint-order','stroke');
  label.setAttribute('stroke','#ffffff');
  label.setAttribute('stroke-width','2.2');
  label.setAttribute('stroke-linejoin','round');
  label.setAttribute('fill','#111111');
  label.style.display='';
  label.style.visibility='visible';
  label.style.opacity='1';
  label.textContent=String(region.colorId);
  return label;
}

function regionHasUsableNumber(region){
  if(!regionIsVisuallyPaintable(region)) return false;
  const label=repairRegionNumber(region);
  if(!label) return false;
  return String(label.textContent||'').trim()===String(region.colorId);
}

function clearHintMarker(){
  const marker=svgHost.querySelector('#activeHintMarker');
  if(marker) marker.remove();
}

function showHintMarker(region){
  clearHintMarker();
  const shape=document.getElementById(region.regionId);
  if(!shape) return false;
  const point=findVisibleInteriorPoint(region,shape);
  if(!point) return false;

  const svg=shape.ownerSVGElement;
  const box=point.box;
  const viewBox=svg.viewBox.baseVal;
  const radius=Math.max(
    Math.min(viewBox.width,viewBox.height)*0.012,
    Math.min(Math.max(box.width,box.height)*0.65,Math.min(viewBox.width,viewBox.height)*0.035)
  );

  const group=document.createElementNS('http://www.w3.org/2000/svg','g');
  group.id='activeHintMarker';
  group.setAttribute('pointer-events','none');

  const ring=document.createElementNS('http://www.w3.org/2000/svg','circle');
  ring.setAttribute('class','hint-marker-ring');
  ring.setAttribute('cx',String(point.x));ring.setAttribute('cy',String(point.y));
  ring.setAttribute('r',String(radius));

  const core=document.createElementNS('http://www.w3.org/2000/svg','circle');
  core.setAttribute('class','hint-marker-core');
  core.setAttribute('cx',String(point.x));core.setAttribute('cy',String(point.y));
  core.setAttribute('r',String(radius*.58));

  const text=document.createElementNS('http://www.w3.org/2000/svg','text');
  text.setAttribute('class','hint-marker-text');
  text.setAttribute('x',String(point.x));text.setAttribute('y',String(point.y));
  text.setAttribute('font-size',String(Math.max(8,radius*.72)));
  text.textContent=String(region.colorId);

  group.append(ring,core,text);
  svg.appendChild(group);
  return true;
}


function refreshNumberLabels(){
  ensureEveryPaintableRegionHasNumber();
  const labels=[...svgHost.querySelectorAll('.region-number')];

  for(const label of labels){
    const regionId=label.getAttribute('data-region-id');
    const colorId=label.getAttribute('data-color-id');

    // Backward-compatible fallback: locate the corresponding region by matching
    // the label position against the generated region records.
    let record=null;
    if(regionId){
      record=REGIONS.find(r=>r.regionId===regionId);
    }
    if(!record){
      const x=parseFloat(label.getAttribute('x')||'0');
      const y=parseFloat(label.getAttribute('y')||'0');
      record=REGIONS.find(r=>
        Math.abs((r.label?.x||0)-x)<0.2 &&
        Math.abs((r.label?.y||0)-y)<0.2
      );
    }
    if(!record) continue;

    const paintedAlready=!!painted[record.regionId];
    const selected=String(record.colorId)===String(selectedColor);

    label.classList.remove('selected-number','unrelated-number','painted-number');

    if(paintedAlready){
      label.classList.add('painted-number');
      continue;
    }

    if(numberFocus){
      if(selected){
        label.classList.add('selected-number');
        label.style.fontSize=Math.max(10,Number(record.label?.fontSize||9)*1.18)+'px';
      }else{
        label.classList.add('unrelated-number');
      }
    }else{
      label.style.opacity='1';
      label.style.fontSize=(record.label?.fontSize||9)+'px';
    }
  }

  const toggle=document.getElementById('toggleNumberFocus');
  if(toggle){
    toggle.textContent=numberFocus?'Show All Numbers':'Focus Selected Number';
  }
}

function refreshSelectedRegions(){
  ensureSelectionPattern();
  for(const region of REGIONS){
    const el=document.getElementById(region.regionId);
    if(!el) continue;

    if(painted[region.regionId]){
      el.style.fill=region.fillColor || region.paletteColor;
      el.style.stroke='rgba(32,32,32,0.42)';
      el.style.opacity='1';
      el.style.filter='';
      const hit=svgHost.querySelector(
        `.region-hit[data-target-region="${region.regionId}"]`
      );
      if(hit) hit.style.pointerEvents='none';
      continue;
    }

    const matches=String(region.colorId)===String(selectedColor);
    const hinted=region.regionId===hintedRegionId;

    // Every matching unpainted region receives the soft gray guidance fill.
    // A requested hint adds a stronger glow, but the normal selection remains
    // visible at all times.
    el.style.fill=matches ? 'url(#selectedGrayPixelPattern)' : '#ffffff';
    el.style.opacity=matches ? '1' : '0.72';
    el.style.filter=hinted ? 'drop-shadow(0 0 10px #ff2f92)' : '';
  }
  refreshNumberLabels();
}

function applySaved(){
  for(const region of REGIONS){
    const el=document.getElementById(region.regionId);
    if(!el) continue;
    el.dataset.painted=painted[region.regionId]?'true':'false';
    const hit=svgHost.querySelector(
      `.region-hit[data-target-region="${region.regionId}"]`
    );
    if(hit){
      hit.style.pointerEvents=painted[region.regionId]?'none':'all';
    }
  }
  refreshSelectedRegions();
  renderProgress();
  updateColorStatus();
}

function resolveClickedRegion(target){
  const hit=target.closest?.('.region-hit');
  const shape=target.closest?.('.paint-region');
  const regionId=
    hit?.getAttribute('data-target-region') ||
    shape?.getAttribute('data-region-id') ||
    shape?.id;

  if(!regionId) return null;
  const region=REGIONS.find(item=>item.regionId===regionId);
  const element=document.getElementById(regionId);
  if(!region || !element) return null;
  return {region,element,hit};
}

// Event delegation is the permanent interaction path. It continues working
// even when SVG paths are rebuilt, labels are repaired, or hit targets overlap.
svgHost.addEventListener('click',event=>{
  if(hasDragged) return;
  const resolved=resolveClickedRegion(event.target);
  if(!resolved) return;
  event.preventDefault();
  event.stopPropagation();
  handleRegion(resolved.region,resolved.element);
});

function handleRegion(region,el){
  if(hasDragged) return;

  if(inspect){
    document.getElementById('regionInfo').textContent=
      `Region: ${region.regionId}\nColor: ${region.colorId}\nArea: ${region.area}\nOriginal: ${region.originalColor}\nPalette: ${region.paletteColor}`;
    return;
  }

  playMode=currentPlayMode();

  if(String(region.colorId)!==String(selectedColor)){
    message.textContent=`You are working on color ${selectedColor}. Finish every color ${selectedColor} section before moving to color ${region.colorId}.`;
    el.animate([{opacity:1},{opacity:.42},{opacity:1}],{duration:360});
    return;
  }

  painted[region.regionId]=true;
  revealPaintedRegion(region.regionId);
  if(hintedRegionId===region.regionId){hintedRegionId=null;clearHintMarker();}

  // Give immediate visual confirmation with the selected palette color.
  el.style.fill=region.fillColor || region.paletteColor;
  el.style.stroke='rgba(32,32,32,0.42)';
  el.style.opacity='1';

  const hit=svgHost.querySelector(
    `.region-hit[data-target-region="${region.regionId}"]`
  );
  if(hit) hit.style.pointerEvents='none';

  message.textContent=`Color ${selectedColor} section painted.`;
  el.dataset.painted='true';
  save(false);
  refreshSelectedRegions();
  renderProgress();
  updateColorStatus();

  const finishedColor=selectedColor;
  const finishedState=colorCompletionMap()[String(finishedColor)];
  if(finishedState?.complete){
    const next=chooseNextAvailableColor();

    if(next){
      selectedColor=next.colorId;
      hintedRegionId=null;
      renderPalette();
      refreshSelectedRegions();
      updateColorStatus();
      message.textContent=`Color ${finishedColor} complete ✓ Moving to color ${selectedColor}.`;
    }else{
      message.textContent=`Color ${finishedColor} complete ✓`;
    }
  }
}


function colorCompletionMap(){
  const map={};

  for(const item of PALETTE){
    const allRegions=REGIONS.filter(
      r=>String(r.colorId)===String(item.colorId)
    );

    // Only count regions that were successfully rendered into the SVG and can
    // actually be selected. This prevents invisible/missing vector regions from
    // permanently blocking a color at 49/51.
    const paintableRegions=allRegions.filter(
      r=>document.getElementById(r.regionId)
    );

    const missing=allRegions.length-paintableRegions.length;
    const done=paintableRegions.filter(r=>painted[r.regionId]).length;

    map[String(item.colorId)]={
      done,
      total:paintableRegions.length,
      sourceTotal:allRegions.length,
      missing,
      complete:paintableRegions.length>0&&done===paintableRegions.length
    };
  }

  return map;
}


function chooseNextAvailableColor(){
  const map=colorCompletionMap();

  let next=PALETTE.find(item=>{
    const state=map[String(item.colorId)];
    return state && !state.complete && !skippedColors.has(String(item.colorId));
  });

  // Once every remaining color has been skipped, clear the temporary skips and
  // cycle back through unfinished colors.
  if(!next){
    skippedColors.clear();
    next=PALETTE.find(item=>{
      const state=map[String(item.colorId)];
      return state && !state.complete;
    });
  }

  return next || null;
}

function updateColorStatus(){
  const map=colorCompletionMap();
  const completed=Object.values(map).filter(v=>v.complete).length;
  const total=Object.values(map).filter(v=>v.total>0).length;
  const selected=map[String(selectedColor)];

  document.getElementById('colorStatus').textContent=
    `${completed} of ${total} colors complete`;

  const selectedStatus=document.getElementById('selectedColorStatus');
  if(!selected){
    selectedStatus.textContent='';
    return;
  }

  const percent=selected.total?Math.round(selected.done/selected.total*100):0;
  const unavailableText=selected.missing
    ? ` • ${selected.missing} region reference${selected.missing===1?'':'s'} awaiting repair`
    : '';

  selectedStatus.textContent=selected.complete
    ? `Color ${selectedColor}: COMPLETE ✓ (${selected.done}/${selected.total})${unavailableText}`
    : `Color ${selectedColor}: ${selected.done}/${selected.total} regions • ${percent}% complete${unavailableText}`;
}

function centerOnSelectedColor(){
  const svg=svgHost.querySelector('svg');
  if(!svg) return;
  const matches=REGIONS
    .filter(r=>String(r.colorId)===String(selectedColor) && !painted[r.regionId] && regionIsVisuallyPaintable(r))
    .map(r=>document.getElementById(r.regionId))
    .filter(Boolean);

  if(!matches.length){
    const unfinishedWithoutNumbers=REGIONS.filter(
      r=>
        String(r.colorId)===String(selectedColor) &&
        !painted[r.regionId] &&
        document.getElementById(r.regionId) &&
        !regionHasUsableNumber(r)
    ).length;
    message.textContent=unfinishedWithoutNumbers
      ? `No valid numbered hint is available for color ${selectedColor}. The unnumbered section was excluded from hints.`
      : `Color ${selectedColor} is complete.`;
    return;
  }

  let box=null;
  for(const el of matches){
    const b=el.getBBox();
    if(!box){
      box={x:b.x,y:b.y,x2:b.x+b.width,y2:b.y+b.height};
    }else{
      box.x=Math.min(box.x,b.x);
      box.y=Math.min(box.y,b.y);
      box.x2=Math.max(box.x2,b.x+b.width);
      box.y2=Math.max(box.y2,b.y+b.height);
    }
  }

  const svgBox=svg.viewBox.baseVal;
  const cx=(box.x+box.x2)/2;
  const cy=(box.y+box.y2)/2;
  const normalizedX=(cx-(svgBox.x+svgBox.width/2))/svgBox.width;
  const normalizedY=(cy-(svgBox.y+svgBox.height/2))/svgBox.height;

  const rect=artboard.getBoundingClientRect();
  if(zoom<2) zoom=2;
  panX=-normalizedX*rect.width*zoom;
  panY=-normalizedY*rect.height*zoom;
  applyTransform();
  document.getElementById('zoomReadout').textContent=Math.round(zoom*100)+'%';
  message.textContent=`Centered on remaining regions for color ${selectedColor}.`;
}

function renderPalette(){
  paletteHost.innerHTML='';
  const completion=colorCompletionMap();

  for(const item of PALETTE){
    const state=completion[String(item.colorId)] || {complete:false,done:0,total:0};
    const percent=state.total?Math.round(state.done/state.total*100):0;
    const button=document.createElement('button');

    const activeState=completion[String(selectedColor)];
    const locked=(
      String(item.colorId)!==String(selectedColor)
      && activeState
      && !activeState.complete
    );

    button.className='swatch'
      +(String(item.colorId)===String(selectedColor)?' active':'')
      +(state.complete?' complete':'')
      +(locked?' locked':'');

    button.style.background=item.hex;
    button.style.color=luminance(item.rgb)<.48?'#fff':'#111';
    button.title=state.complete
      ? `Color ${item.colorId} complete`
      : `Color ${item.colorId}: ${state.done} of ${state.total} regions`;

    const number=document.createElement('span');
    number.className='swatch-number';
    number.textContent=item.colorId;

    const countLabel=document.createElement('span');
    countLabel.className='swatch-count';
    countLabel.textContent=`${state.done}/${state.total}`;

    const progress=document.createElement('span');
    progress.className='swatch-progress';
    progress.style.width=percent+'%';

    button.append(number,countLabel,progress);

    button.onclick=()=>{
      const current=colorCompletionMap()[String(selectedColor)];

      if(
        String(item.colorId)!==String(selectedColor)
        && current
        && !current.complete
        && !skippedColors.has(String(selectedColor))
      ){
        message.textContent=
          `Color ${selectedColor} is still in progress (${current.done}/${current.total}). Complete it or use “Skip This Color for Now.”`;
        return;
      }

      selectedColor=item.colorId;
      skippedColors.delete(String(selectedColor));
      hintedRegionId=null;
      renderPalette();
      refreshSelectedRegions();
      updateColorStatus();

      message.textContent=state.complete
        ? `Color ${item.colorId} is complete. Choose an unfinished color.`
        : `Color ${item.colorId} selected — ${state.done} of ${state.total} sections finished.`;
    };

    paletteHost.appendChild(button);
  }
}
function luminance(rgb){return (.2126*rgb[0]+.7152*rgb[1]+.0722*rgb[2])/255}


function runCompletionReveal(){
  zoom=1;panX=0;panY=0;
  canvasContent.style.transform='translate3d(0px, 0px, 0) scale(1)';
  document.getElementById('zoomReadout').textContent='100%';

  referenceOverlay.style.display='none';
  masterReveal.style.display='none';
  svgHost.style.display='none';

  // The outcome is the untouched high-resolution artwork uploaded by the
  // creator—not the quantized palette preview and not the polygon rendering.
  completion.style.display='block';
  completion.style.clipPath='none';
  completion.style.opacity='0';
  completion.style.objectFit='contain';
  completion.style.imageRendering='auto';
  completion.style.width='100%';
  completion.style.height='100%';
  completion.style.background='#fff';
  completion.style.mixBlendMode='normal';

  requestAnimationFrame(()=>{completion.style.opacity='1'});
  setTimeout(()=>{
    const banner=document.getElementById('completionMessage');
    banner.textContent='Masterpiece complete — your original artwork is ready to download and showcase.';
    banner.style.opacity='1';
    banner.style.transform='translateY(0)';
    showcasePanel.style.opacity='1';
    showcasePanel.style.transform='translate(-50%,0)';
    showcasePanel.style.pointerEvents='auto';
  },500);
}

function resetCompletionReveal(){
  const banner=document.getElementById('completionMessage');
  banner.style.opacity='0';
  banner.style.transform='translateY(12px)';
  completion.style.opacity='0';
  completion.style.clipPath='inset(0 100% 0 0)';
  completion.style.display='block';
  svgHost.style.display='block';
  svgHost.style.opacity='1';
  svgHost.style.filter='none';
  masterReveal.style.display='block';
  showcasePanel.style.opacity='0';
  showcasePanel.style.transform='translate(-50%,18px)';
  showcasePanel.style.pointerEvents='none';
  referenceOverlay.style.display='block';
}

function renderProgress(){
  const paintable=REGIONS.filter(
    r=>document.getElementById(r.regionId)
  );
  const done=paintable.filter(r=>painted[r.regionId]).length;
  const total=paintable.length;
  const percent=total?Math.round(done/total*100):0;
  count.textContent=`${done} / ${total} regions painted`;
  bar.style.width=percent+'%';
  renderPalette();
  updateColorStatus();
  if(done===total && total){
    message.textContent='Complete! Revealing your finished artwork…';
    if(!completionShown){runCompletionReveal();completionShown=true;}
  }else{
    completionShown=false;
    resetCompletionReveal();
    referenceOverlay.style.opacity=referenceVisible?'.55':'0';
  }
}
function safeLocalSet(key,value){
  try{localStorage.setItem(key,value);return true}catch(error){return false}
}
function safeLocalGet(key){
  try{return localStorage.getItem(key)}catch(error){return null}
}
function progressPayload(){
  return {
    version:2,
    artwork:location.pathname,
    selectedColor,
    painted,
    skippedColors:[...skippedColors],
    savedAt:new Date().toISOString()
  };
}
function save(show=true){
  const ok=safeLocalSet(STORAGE_KEY,JSON.stringify(progressPayload()));
  if(show) message.textContent=ok
    ? 'Progress saved on this device.'
    : 'Browser storage is unavailable. Use Download Progress to save a file.';
  return ok;
}
function downloadJson(data,filename){
  const blob=new Blob([JSON.stringify(data,null,2)],{type:'application/json'});
  const url=URL.createObjectURL(blob);
  const link=document.createElement('a');link.href=url;link.download=filename;link.click();
  URL.revokeObjectURL(url);
}
function restoreProgressPayload(payload){
  if(!payload) return false;
  painted=payload.painted && typeof payload.painted==='object' ? payload.painted : {};
  selectedColor=String(payload.selectedColor||selectedColor);
  skippedColors=new Set(Array.isArray(payload.skippedColors)?payload.skippedColors:[]);
  rebuildMasterReveal();
  applySaved();
  refreshSelectedRegions();
  renderPalette();
  renderProgress();
  updateColorStatus();
  return true;
}
function applyTransform(){
  panX=Math.round(panX);panY=Math.round(panY);
  canvasContent.style.transform=`translate3d(${panX}px, ${panY}px, 0) scale(${zoom})`;
}
function setZoom(next){
  zoom=Math.max(.6,Math.min(8,next));
  if(zoom<=1){
    panX=0;panY=0;
    isDragging=false;pointerIsDown=false;hasDragged=false;
    artboard.classList.remove('dragging');
  }
  applyTransform();
  artboard.style.cursor=zoom>1?'grab':'';
  document.getElementById('zoomReadout').textContent=Math.round(zoom*100)+'%';
}
function setMode(mode){
  document.body.className=mode==='review'?'review-mode':'customer-mode';
  document.getElementById('reviewModeBtn').className=mode==='review'?'primary':'secondary';
  document.getElementById('customerModeBtn').className=mode==='customer'?'primary':'secondary';
  inspect=false;
  message.textContent=mode==='review'?'Private testing mode. Nothing is uploaded.':'Customer preview mode.';
}
function difficulty(){
  const total=REGIONS.length;
  const tiny=REGIONS.filter(r=>r.area<80).length;
  const ratio=total?tiny/total:0;
  let label='Comfortable';
  if(total>1200||ratio>.35) label='Very detailed';
  else if(total>700||ratio>.22) label='Detailed';
  else if(total<300&&ratio<.12) label='Easy';
  const badge=document.getElementById('difficultyBadge');
  badge.textContent=`Difficulty: ${label} • ${total} regions • ${tiny} tiny`;
  badge.style.background=label==='Very detailed'?'#f8e3e3':label==='Easy'?'#e4f4e7':'#fff0d8';
}



function paintRegionProgrammatically(region,reason='assisted completion'){
  const el=document.getElementById(region.regionId);
  if(!el || painted[region.regionId]) return false;

  painted[region.regionId]=true;
  revealPaintedRegion(region.regionId);
  el.dataset.painted='true';
  el.style.fill='rgba(255,255,255,0.015)';
  el.style.stroke='rgba(32,32,32,0.32)';
  el.style.opacity='1';
  el.style.filter='';

  if(hintedRegionId===region.regionId){
    hintedRegionId=null;
    clearHintMarker();
  }

  save(false);
  refreshSelectedRegions();
  renderProgress();
  updateColorStatus();
  message.textContent=`Color ${region.colorId} section completed by ${reason}.`;
  return true;
}

function unresolvedSelectedRegions(){
  return REGIONS.filter(
    r=>
      String(r.colorId)===String(selectedColor) &&
      !painted[r.regionId] &&
      document.getElementById(r.regionId)
  );
}

function resolveHiddenSelectedRegions(){
  const unresolved=unresolvedSelectedRegions();
  let repaired=0;
  for(const region of unresolved){
    const label=repairRegionNumber(region);
    if(label) repaired+=1;
  }
  refreshNumberLabels();
  return repaired;
}

document.getElementById('hintOneBtn').onclick=()=>{
  const remaining=REGIONS.filter(
    r=>
      String(r.colorId)===String(selectedColor) &&
      !painted[r.regionId] &&
      regionHasUsableNumber(r) &&
      regionIsVisuallyPaintable(r)
  );

  if(!remaining.length){
    const resolved=resolveHiddenSelectedRegions();
    hintedRegionId=null;
    clearHintMarker();
    refreshSelectedRegions();
    renderProgress();
    updateColorStatus();
    message.textContent=resolved
      ? `${resolved} color ${selectedColor} number reference${resolved===1?' was':'s were'} restored.`
      : `No visible unpainted color ${selectedColor} sections remain.`;
    return;
  }

  // Rotate only through regions that can be visibly marked and selected.
  let currentIndex=remaining.findIndex(r=>r.regionId===hintedRegionId);
  let next=null;
  for(let offset=1;offset<=remaining.length;offset++){
    const candidate=remaining[(currentIndex+offset)%remaining.length];
    if(showHintMarker(candidate)){
      next=candidate;
      break;
    }
  }

  if(!next){
    const resolved=resolveHiddenSelectedRegions();
    hintedRegionId=null;
    clearHintMarker();
    refreshSelectedRegions();
    renderProgress();
    updateColorStatus();
    message.textContent=resolved
      ? `${resolved} color ${selectedColor} number reference${resolved===1?' was':'s were'} repaired.`
      : `No usable color ${selectedColor} hint remains.`;
    return;
  }

  hintedRegionId=next.regionId;
  repairRegionNumber(next);
  refreshSelectedRegions();
  showHintMarker(next); // redraw after refresh so marker stays above all labels.

  const el=document.getElementById(hintedRegionId);
  if(el){
    const point=findVisibleInteriorPoint(next,el);
    const svg=svgHost.querySelector('svg');
    const svgBox=svg.viewBox.baseVal;
    const cx=point?.x ?? (el.getBBox().x+el.getBBox().width/2);
    const cy=point?.y ?? (el.getBBox().y+el.getBBox().height/2);
    const normalizedX=(cx-(svgBox.x+svgBox.width/2))/svgBox.width;
    const normalizedY=(cy-(svgBox.y+svgBox.height/2))/svgBox.height;
    if(zoom<3) zoom=3;
    const rect=artboard.getBoundingClientRect();
    panX=Math.round(-normalizedX*rect.width*zoom);
    panY=Math.round(-normalizedY*rect.height*zoom);
    applyTransform();
    document.getElementById('zoomReadout').textContent=Math.round(zoom*100)+'%';
  }

  message.textContent=`Hint: the pink marker identifies one visible number ${selectedColor} section.`;
};


document.getElementById('paintHintBtn').onclick=()=>{
  if(!hintedRegionId){
    message.textContent='Select Show One Hint first.';
    return;
  }
  const region=REGIONS.find(r=>r.regionId===hintedRegionId);
  if(!region){
    message.textContent='The highlighted section is no longer available.';
    return;
  }
  paintRegionProgrammatically(region,'the highlighted-section control');
};

document.getElementById('resolveHiddenBtn').onclick=()=>{
  const resolved=resolveHiddenSelectedRegions();
  refreshSelectedRegions();
  renderProgress();
  updateColorStatus();
  message.textContent=resolved
    ? `${resolved} hidden color ${selectedColor} section${resolved===1?' was':'s were'} completed.`
    : `No hidden color ${selectedColor} sections need recovery.`;
};

document.getElementById('clearHintBtn').onclick=()=>{
  hintedRegionId=null;
  clearHintMarker();
  refreshSelectedRegions();
  message.textContent='Hint cleared.';
};


document.getElementById('skipColorBtn').onclick=()=>{
  const current=String(selectedColor);
  const currentState=colorCompletionMap()[current];

  if(!currentState || currentState.complete){
    message.textContent=`Color ${selectedColor} is already complete.`;
    return;
  }

  skippedColors.add(current);
  hintedRegionId=null;

  const next=chooseNextAvailableColor();
  if(!next){
    message.textContent='No other unfinished colors are available.';
    return;
  }

  const skippedColor=selectedColor;
  selectedColor=next.colorId;
  renderPalette();
  refreshSelectedRegions();
  updateColorStatus();

  message.textContent=
    `Color ${skippedColor} saved for later. Continue with color ${selectedColor}.`;
};


document.getElementById('downloadArtworkBtn').onclick=()=>{
  downloadDataUrl(completion.src,'euqilegna-finished-artwork.png');
};

document.getElementById('downloadShowcaseBtn').onclick=async()=>{
  const canvas=await createShowcaseCard();
  downloadDataUrl(canvas.toDataURL('image/png'),'euqilegna-painted-artwork-showcase.png');
};

document.getElementById('shareArtworkBtn').onclick=async()=>{
  try{
    await shareFinishedArtwork();
  }catch(error){
    message.textContent='Sharing was cancelled or unavailable.';
  }
};


document.getElementById('exportProgressBtn').onclick=()=>{
  downloadJson(progressPayload(),'euqilegna-paint-progress.json');
  message.textContent='Progress file downloaded.';
};
document.getElementById('importProgressBtn').onclick=()=>{
  document.getElementById('progressImportFile').click();
};
document.getElementById('progressImportFile').onchange=async event=>{
  const file=event.target.files?.[0];
  if(!file) return;
  try{
    const payload=JSON.parse(await file.text());
    if(!restoreProgressPayload(payload)) throw new Error('Invalid progress payload');
    save(false);
    message.textContent='Progress restored from file.';
  }catch(error){
    message.textContent='The selected progress file could not be loaded.';
  }
};

document.getElementById('saveBtn').onclick=()=>save(true);
function resetArtworkForRepaint(){
  painted={};
  hintedRegionId=null;
  skippedColors=new Set();
  completionShown=false;
  numberFocus=false;
  clearHintMarker();
  safeLocalSet(STORAGE_KEY,JSON.stringify(progressPayload()));
  resetCompletionReveal();
  applySaved();
  rebuildMasterReveal();
  refreshSelectedRegions();
  refreshNumberLabels();
  renderPalette();
  renderProgress();
  updateColorStatus();
  setZoom(1);
  message.textContent='Artwork reset. All unpainted number references are visible.';
}

document.getElementById('resetBtn').onclick=()=>{
  if(!confirm('Reset this painting and start again? This will erase the current painting progress.')) return;
  resetArtworkForRepaint();
};

document.getElementById('paintAgainBtn').onclick=()=>{
  if(!confirm('Paint this artwork again? This will erase the current painting progress and restore the numbered canvas.')) return;
  resetArtworkForRepaint();
};

// Any drag past a few pixels moves the picture. A quick tap still paints,
// so customers never have to switch modes to look around a zoomed image.
const PAN_THRESHOLD=5;
let panPointerId=null;

artboard.addEventListener('pointerdown',event=>{
  if(event.button===2) return;
  pointerIsDown=true;
  pointerStartX=event.clientX;
  pointerStartY=event.clientY;
  dragStartX=event.clientX-panX;
  dragStartY=event.clientY-panY;
  hasDragged=false;
  isDragging=false;
  artboard.classList.remove('dragging');
  artboard.style.cursor=zoom>1?'grab':'';
  panPointerId=event.pointerId;
});

artboard.addEventListener('pointermove',event=>{
  if(!pointerIsDown || zoom<=1) return;

  if(!isDragging){
    const moved=Math.hypot(event.clientX-pointerStartX,event.clientY-pointerStartY);
    if(moved<PAN_THRESHOLD) return;
    isDragging=true;
    hasDragged=true;
    artboard.classList.add('dragging');
    artboard.style.cursor='grabbing';
    try{artboard.setPointerCapture(panPointerId)}catch(e){}
  }

  event.preventDefault();
  panX=event.clientX-dragStartX;
  panY=event.clientY-dragStartY;
  applyTransform();
});

artboard.addEventListener('pointerup',event=>{
  pointerIsDown=false;
  panPointerId=null;
  if(isDragging){
    event.preventDefault();
    event.stopPropagation();
  }

  isDragging=false;
  artboard.classList.remove('dragging');
  artboard.style.cursor=zoom>1?'grab':'';

  try{artboard.releasePointerCapture(event.pointerId)}catch(e){}
  setTimeout(()=>{hasDragged=false;},0);
});

artboard.addEventListener('pointercancel',()=>{
  pointerIsDown=false;
  isDragging=false;
  hasDragged=false;
  artboard.classList.remove('dragging');
});
artboard.addEventListener('lostpointercapture',()=>{
  pointerIsDown=false;
  isDragging=false;
  artboard.classList.remove('dragging');
});

artboard.addEventListener('wheel',event=>{
  event.preventDefault();
  const delta=event.deltaY<0?.25:-.25;
  setZoom(zoom+delta);
},{passive:false});

document.getElementById('zoomIn').onclick=()=>setZoom(Math.round((zoom+.25)*4)/4);
document.getElementById('zoomOut').onclick=()=>setZoom(Math.round((zoom-.25)*4)/4);
document.getElementById('zoomReset').onclick=()=>setZoom(1);
document.getElementById('centerSelected').onclick=centerOnSelectedColor;
document.getElementById('toggleNumberFocus').onclick=()=>{
  numberFocus=!numberFocus;
  refreshNumberLabels();
  document.getElementById('toggleNumberFocus').textContent=
    numberFocus ? 'Show All Numbers' : 'Focus Selected Number';
  message.textContent=numberFocus
    ? `Focus Mode on — only number ${selectedColor} is visible.`
    : 'All unpainted number references are visible.';
};
document.getElementById('toggleReference').onclick=()=>{
  referenceVisible=!referenceVisible;
  referenceOverlay.style.opacity=referenceVisible?'.55':'0';
  document.getElementById('toggleReference').textContent=referenceVisible?'Hide Reference Overlay':'Show Reference Overlay';
};
document.getElementById('inspectMode').onclick=()=>{
  inspect=!inspect;
  document.getElementById('inspectMode').textContent=inspect?'Stop Inspecting':'Inspect Regions';
  message.textContent=inspect?'Select any region to inspect it.':'Region inspection turned off.';
};
document.getElementById('reviewModeBtn').onclick=()=>setMode('review');
document.getElementById('customerModeBtn').onclick=()=>setMode('customer');


function reviewPayload(){
  return {
    notes:document.getElementById('reviewNotes').value,
    checklist:[...document.querySelectorAll('#checklist input')].map(box=>box.checked),
    approved:safeLocalGet(REVIEW_KEY)==='approved',
    updatedAt:new Date().toISOString()
  };
}
function saveReview(show=true){
  const ok=safeLocalSet(REVIEW_KEY+'-details',JSON.stringify(reviewPayload()));
  if(show){
    document.getElementById('approvalMessage').textContent=ok
      ? 'Review notes and checklist saved locally.'
      : 'Browser storage is unavailable. Download the QA Report to preserve your notes.';
  }
  return ok;
}
document.getElementById('saveReviewBtn').onclick=()=>saveReview(true);
document.getElementById('reviewNotes').addEventListener('input',()=>saveReview(false));
document.querySelectorAll('#checklist input').forEach(box=>box.addEventListener('change',()=>saveReview(false)));
try{
  const savedReview=JSON.parse(safeLocalGet(REVIEW_KEY+'-details')||'null');
  if(savedReview){
    document.getElementById('reviewNotes').value=savedReview.notes||'';
    [...document.querySelectorAll('#checklist input')].forEach((box,index)=>{
      box.checked=Boolean(savedReview.checklist?.[index]);
    });
  }
}catch(error){}

document.getElementById('downloadReport').onclick=()=>{
  const checks=[...document.querySelectorAll('#checklist input')].map((box,index)=>({item:index+1,passed:box.checked}));
  const report={
    generatedAt:new Date().toISOString(),
    totalRegions:REGIONS.length,
    tinyRegions:REGIONS.filter(r=>r.area<80).length,
    paletteColors:PALETTE.length,
    checklist:checks,
    notes:document.getElementById('reviewNotes').value,
    approved:localStorage.getItem(REVIEW_KEY)==='approved'
  };
  const blob=new Blob([JSON.stringify(report,null,2)],{type:'application/json'});
  const url=URL.createObjectURL(blob);
  const link=document.createElement('a');link.href=url;link.download='euqilegna_qa_report.json';link.click();
  URL.revokeObjectURL(url);
};
document.getElementById('approveBtn').onclick=()=>{
  const boxes=[...document.querySelectorAll('#checklist input')];
  if(!boxes.every(box=>box.checked)){
    document.getElementById('approvalMessage').textContent='Complete every checklist item before approval.';
    document.getElementById('approvalMessage').style.color='#8d1717';
    return;
  }
  localStorage.setItem(REVIEW_KEY,'approved');
  document.getElementById('approvalMessage').textContent='Approved locally for website publishing. The package has not been uploaded.';
  document.getElementById('approvalMessage').style.color='#216e39';
};

document.querySelectorAll('input[name="playMode"]').forEach(radio=>radio.onchange=()=>{
  playMode=currentPlayMode();
  message.textContent=`Play style changed to ${playMode}.`;
});

setupMasterReveal();
ensureEveryPaintableRegionHasNumber();
renderPalette();
applySaved();
refreshSelectedRegions();
numberFocus=false;
refreshNumberLabels();
document.getElementById('toggleNumberFocus').textContent='Focus Selected Number';
difficulty();
setMode('review');

__POINTER_ENGINE_V9__
</script>
</body>
</html>"""

    html = html.replace("__ASPECT_RATIO__", f"{completion_data.width}/{completion_data.height}")
    html = html.replace("__BLANK_SVG__", blank_svg)
    html = html.replace("__COMPLETION_URI__", completion_uri)
    html = html.replace("__REVEAL_URI__", reveal_uri)
    html = html.replace("__REFERENCE_URI__", reference_uri)
    html = html.replace("__REGIONS__", payload_regions)
    html = html.replace("__PALETTE__", payload_palette)
    html = html.replace(
        "__POINTER_ENGINE_V9__",
        build_pointer_engine_script(),
    )
    (output_dir / "interactive_player.html").write_text(html, encoding="utf-8")


def build_quality_report(
    output_dir: Path,
    records: list,
    palette: list,
    metadata: dict,
) -> dict:
    """Score paintability and create publish-gate artifacts for website release."""
    total = max(1, len(records))
    tiny = sum(1 for item in records if item.get("paintability") == "tiny")
    small = sum(1 for item in records if item.get("paintability") == "small")
    unlabeled = sum(
        1
        for item in records
        if not item.get("label")
        or item.get("label", {}).get("x") is None
        or item.get("label", {}).get("y") is None
    )
    high_detail = sum(1 for item in records if item.get("detailScore", 0) >= 0.62)

    tiny_ratio = tiny / total
    small_ratio = small / total
    unlabeled_ratio = unlabeled / total

    region_score = max(0, 100 - max(0, len(records) - 650) * 0.075)
    tiny_score = max(0, 100 - tiny_ratio * 180)
    label_score = max(0, 100 - unlabeled_ratio * 130)
    palette_score = 100 if 12 <= len(palette) <= 48 else 75
    completion_score = 100 if (output_dir / "completion_original.png").exists() else 0

    paintability_score = round(
        region_score * 0.25
        + tiny_score * 0.30
        + label_score * 0.25
        + palette_score * 0.10
        + completion_score * 0.10
    )

    if paintability_score >= 86 and tiny_ratio <= 0.12 and unlabeled_ratio <= 0.16:
        status = "PASS"
    elif paintability_score >= 72 and tiny_ratio <= 0.24:
        status = "REVIEW"
    else:
        status = "FAIL"

    difficulty = (
        "Easy"
        if len(records) <= 350 and tiny_ratio <= 0.08
        else "Intermediate"
        if len(records) <= 700 and tiny_ratio <= 0.18
        else "Advanced"
    )

    # Digital interaction estimate, not physical painting time.
    estimated_minutes = round(
        len(records) * 0.10
        + tiny * 0.05
        + high_detail * 0.025
    )
    estimated_minutes = max(5, estimated_minutes)

    recommendations = []
    if len(records) > 700:
        recommendations.append("Use Relaxed mode or lower the target region count.")
    if tiny_ratio > 0.12:
        recommendations.append("Merge more tiny regions before publishing.")
    if unlabeled_ratio > 0.16:
        recommendations.append("Improve label placement or require zoom-assisted play.")
    if len(palette) > 40:
        recommendations.append("Consider reducing the palette for easier customer navigation.")
    if not recommendations:
        recommendations.append("The project is suitable for private customer-experience testing.")

    report = {
        "studio": "Euqilegna Paint Studio 1.0",
        "status": status,
        "paintabilityScore": paintability_score,
        "difficulty": difficulty,
        "estimatedInteractiveMinutes": estimated_minutes,
        "metrics": {
            "regions": len(records),
            "colors": len(palette),
            "tinyRegions": tiny,
            "tinyRegionPercent": round(tiny_ratio * 100, 1),
            "smallRegions": small,
            "smallRegionPercent": round(small_ratio * 100, 1),
            "unlabeledRegions": unlabeled,
            "unlabeledRegionPercent": round(unlabeled_ratio * 100, 1),
            "highDetailRegions": high_detail,
        },
        "publishGate": {
            "automaticApproval": status == "PASS",
            "manualReviewRequired": status != "PASS",
            "websiteReady": False,
        },
        "recommendations": recommendations,
    }

    (output_dir / "quality_report.json").write_text(
        json.dumps(report, indent=2),
        encoding="utf-8",
    )

    manifest = {
        "schemaVersion": "1.0",
        "projectType": "interactive-paint-by-number",
        "studio": "Euqilegna Paint Studio",
        "files": {
            "paintMap": "paintMap.svg",
            "referenceMap": "paintMap_reference.svg",
            "paletteMap": "paintMap_palette.svg",
            "inkOverlay": "ink_overlay.svg",
            "regions": "regions.json",
            "palette": "palette.json",
            "completionImage": "finished_masterpiece.png",
            "progressiveRevealImage": "completion_reveal.png",
            "interactivePlayer": "interactive_player.html",
            "qualityReport": "quality_report.json",
        },
        "customerDefaults": {
            "playMode": "relaxed",
            "autoSave": True,
            "highlightColor": True,
            "zoomControls": True,
            "completionReveal": True,
        },
        "quality": report,
    }
    (output_dir / "project_manifest.json").write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )

    recommendation_html = "".join(
        f"<li>{item}</li>" for item in recommendations
    )
    dashboard = f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Euqilegna QA Report</title>
<style>
body{{font-family:Arial,sans-serif;background:#fff8f1;color:#2b190f;margin:0;padding:24px}}
main{{max-width:900px;margin:auto;background:white;border:1px solid #ead6c5;border-radius:18px;padding:26px}}
h1{{font-family:Georgia,serif}}.score{{font-size:54px;font-weight:800}}
.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}
.card{{border:1px solid #ead6c5;border-radius:12px;padding:14px;background:#fffaf6}}
.pass{{color:#216e39}}.review{{color:#9a5b00}}.fail{{color:#a71919}}
@media(max-width:700px){{.grid{{grid-template-columns:1fr}}}}
</style></head>
<body><main>
<h1>Euqilegna Paint Studio QA</h1>
<p class="{status.lower()}"><strong>{status}</strong></p>
<div class="score">{paintability_score}/100</div>
<p>Difficulty: <strong>{difficulty}</strong> · Estimated interactive time: <strong>{estimated_minutes} minutes</strong></p>
<div class="grid">
<div class="card"><strong>{len(records)}</strong><br>regions</div>
<div class="card"><strong>{tiny}</strong><br>tiny regions ({tiny_ratio*100:.1f}%)</div>
<div class="card"><strong>{unlabeled}</strong><br>unlabeled regions ({unlabeled_ratio*100:.1f}%)</div>
</div>
<h2>Recommendations</h2><ul>{recommendation_html}</ul>
<p>This report is a pre-publish gate. Complete the private Review Lab before setting websiteReady to true.</p>
</main></body></html>"""
    (output_dir / "quality_dashboard.html").write_text(
        dashboard,
        encoding="utf-8",
    )
    return report


def build_pdf(path: Path, palette: list, metadata: dict) -> None:
    c = canvas.Canvas(str(path), pagesize=letter)
    width, height = letter
    c.setFont("Helvetica-Bold", 18)
    c.drawString(48, height - 52, "Euqilegna Paint-by-Number Palette")
    c.setFont("Helvetica", 10)
    c.drawString(
        48,
        height - 72,
        f"Regions: {metadata['regions']}   Colors: {metadata['colors']}   "
        f"Engine: V6 Illustration",
    )

    y = height - 110
    for item in palette:
        if y < 70:
            c.showPage()
            y = height - 60

        r, g, b = [v / 255.0 for v in item["rgb"]]
        c.setFillColor(pdfcolors.Color(r, g, b))
        c.rect(48, y - 14, 28, 20, fill=1, stroke=1)
        c.setFillColor(pdfcolors.black)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(86, y - 2, item["colorId"])
        c.setFont("Helvetica", 10)
        c.drawString(112, y - 2, item["hex"])
        y -= 30
    c.save()



def save_completion_source(
    source_rgb: np.ndarray,
    output_path: Path,
    max_side: int = 2400,
) -> None:
    """Save the uploaded source artwork as the clean final reveal image."""
    array = np.asarray(source_rgb, dtype=np.uint8)
    image = Image.fromarray(array, mode="RGB")

    width, height = image.size
    longest = max(width, height)

    if longest > max_side:
        scale = max_side / longest
        image = image.resize(
            (
                max(1, round(width * scale)),
                max(1, round(height * scale)),
            ),
            Image.Resampling.LANCZOS,
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, format="PNG", optimize=True)



def render_artistic_finish(rgb: np.ndarray, finish_mode: str) -> np.ndarray:
    """Create a non-destructive artistic master used by reveal and final export."""
    mode = (finish_mode or "original").strip().lower()
    source = np.ascontiguousarray(rgb.astype(np.uint8))
    if mode == "original":
        return source.copy()

    try:
        if mode == "watercolor":
            painted = cv2.stylization(source, sigma_s=75, sigma_r=0.38)
            gray = cv2.cvtColor(source, cv2.COLOR_RGB2GRAY)
            ink = cv2.adaptiveThreshold(
                gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY, 9, 4
            )
            ink_rgb = cv2.cvtColor(ink, cv2.COLOR_GRAY2RGB)
            painted = cv2.addWeighted(painted, 0.90, ink_rgb, 0.10, 0)
            # Cold-press paper variation.
            noise = np.random.default_rng(4173).normal(0, 5.0, painted.shape[:2])
            paper = np.clip(248 + noise, 228, 255).astype(np.uint8)
            paper_rgb = np.repeat(paper[:, :, None], 3, axis=2)
            return cv2.addWeighted(painted, 0.94, paper_rgb, 0.06, 0)

        # Acrylic and mixed media preserve local detail while introducing pigment depth.
        smooth = cv2.bilateralFilter(source, d=9, sigmaColor=55, sigmaSpace=55)
        detail = cv2.detailEnhance(smooth, sigma_s=10, sigma_r=0.16)
        hsv = cv2.cvtColor(detail, cv2.COLOR_RGB2HSV).astype(np.float32)
        hsv[:, :, 1] *= 1.10 if mode == "acrylic" else 1.16
        hsv[:, :, 2] *= 0.99
        detail = cv2.cvtColor(np.clip(hsv, 0, 255).astype(np.uint8), cv2.COLOR_HSV2RGB)

        h, w = detail.shape[:2]
        yy, xx = np.mgrid[0:h, 0:w]
        canvas = (
            3.2 * np.sin(xx / 5.7) +
            2.7 * np.sin(yy / 7.9) +
            1.8 * np.sin((xx + yy) / 13.0)
        )
        rng = np.random.default_rng(4173)
        canvas += rng.normal(0, 2.0, (h, w))
        textured = np.clip(detail.astype(np.float32) + canvas[:, :, None], 0, 255).astype(np.uint8)

        if mode == "mixed_media":
            edges = cv2.Canny(cv2.cvtColor(source, cv2.COLOR_RGB2GRAY), 70, 150)
            edges = cv2.dilate(edges, np.ones((2, 2), np.uint8), iterations=1)
            textured[edges > 0] = (textured[edges > 0] * 0.52).astype(np.uint8)
        return textured
    except Exception:
        return source.copy()


def compile_artwork(
    input_path: str | Path,
    output_dir: str | Path,
    preset: str = "illustration",
    colors: int | None = None,
    min_region_area: int | None = None,
    merge_strength: float | None = None,
    outline_width: float = 0.38,
    simplify_tolerance: float | None = None,
    smoothing_passes: int = 0,
    auto_crop: bool = True,
    exclude_background: bool = True,
    generate_pdf: bool = True,
    experience_mode: str = "relaxed",
    target_regions: int = 650,
    design_style: str = "smart_auto",
    finish_mode: str = "original",
    progress_callback: Callable[[int, str, dict], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict:
    del merge_strength, smoothing_passes, exclude_background  # Kept for API compatibility.

    started_at = time.time()

    def update(percent: int, stage: str, stats: dict | None = None) -> None:
        payload = {
            "elapsedSeconds": round(time.time() - started_at, 1),
            **(stats or {}),
        }
        print(f"[V7] {percent}% {stage}", flush=True)
        if progress_callback:
            progress_callback(percent, stage, payload)
        if cancel_check and cancel_check():
            raise RuntimeError("Compilation cancelled by user.")

    input_path = Path(input_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    cfg = PRESETS.get(preset, PRESETS["illustration"])
    color_count = cfg.colors if colors is None else colors
    min_area = cfg.min_region_area if min_region_area is None else min_region_area
    tolerance = (
        cfg.simplify_tolerance
        if simplify_tolerance is None
        else simplify_tolerance
    )

    update(3, "Loading artwork")
    original = Image.open(input_path).convert("RGB")
    original.save(output_dir / "original.png")
    original_rgb = np.array(original)

    update(7, "Analyzing artwork type")
    analysis = classify_image_pipeline(original_rgb)
    artwork_profile, profile_analysis = infer_profile(
        original_rgb,
        input_path=input_path,
        pipeline_analysis=analysis,
    )

    if design_style == "smart_auto":
        active_pipeline = artwork_profile.pipeline
    elif design_style in {"photo", "basic_scenic"}:
        active_pipeline = "photo"
    elif design_style in {"premium_lineart", "coloring_book"}:
        active_pipeline = "lineart"
    else:
        active_pipeline = "illustration"

    profile_settings = apply_profile_settings(
        artwork_profile,
        color_count=color_count,
        min_area=min_area,
        target_regions=target_regions,
        tolerance=tolerance,
        experience_mode=experience_mode,
    )
    color_count = profile_settings["color_count"]
    min_area = profile_settings["min_area"]
    target_regions = profile_settings["target_regions"]
    tolerance = profile_settings["tolerance"]
    experience_mode = profile_settings["experience_mode"]

    update(
        10,
        f"Selected {artwork_profile.name.replace('_', ' ').title()} profile",
        {
            "detectedPipeline": analysis["pipeline"],
            "pipelineConfidence": analysis["confidence"],
            "activePipeline": active_pipeline,
            "artworkProfile": artwork_profile.name,
            "profileSource": profile_analysis["source"],
        },
    )

    rgb = original_rgb
    fg, foreground_meta = safe_foreground_mask(
        original_rgb,
        use_full_canvas=(
            artwork_profile.use_full_canvas
            or active_pipeline in {"photo", "lineart"}
        ),
    )
    crop_meta = {
        "cropped": False,
        "box": [0, 0, rgb.shape[1], rgb.shape[0]],
        "foreground": foreground_meta,
    }

    should_crop = (
        auto_crop
        and artwork_profile.allow_auto_crop
        and not artwork_profile.use_full_canvas
        and not foreground_meta.get("fallbackUsed", False)
    )
    if should_crop:
        rgb, fg, crop_result = crop_subject(rgb, fg)
        crop_meta.update(crop_result)
    elif auto_crop and not artwork_profile.allow_auto_crop:
        update(
            11,
            "Auto-crop disabled for this full-frame artwork profile",
            {"artworkProfile": artwork_profile.name},
        )

    # Preserve the uploaded source and create the selected artistic master.
    save_completion_source(original_rgb, output_dir / "completion_original.png", max_side=6000)
    update(13, f"Rendering {finish_mode.replace('_', ' ').title()} finish")
    artistic_master = render_artistic_finish(original_rgb, finish_mode)
    artistic_reveal = render_artistic_finish(rgb, finish_mode)
    save_completion_source(artistic_master, output_dir / "finished_masterpiece.png", max_side=6000)
    save_completion_source(artistic_reveal, output_dir / "completion_reveal.png", max_side=6000)

    rgb = resize_keep_aspect(rgb, cfg.max_side)
    fg = cv2.resize(
        fg.astype(np.uint8),
        (rgb.shape[1], rgb.shape[0]),
        interpolation=cv2.INTER_NEAREST,
    ).astype(bool)

    if active_pipeline == "lineart":
        update(16, "Tracing clean adult line art")
        processed = rgb.copy()
        min_area = max(28, min_area)
        experience_mode = "relaxed"

        regions, barrier, barrier_strength = premium_lineart_regions(
            processed,
            min_region_area=min_area,
        )
        fg = regions > 0
        detail_map = np.where(fg, 0.55, 0.0).astype(np.float32)
        marker_count = int(len(np.unique(regions[regions > 0])))

        if marker_count < 3:
            update(
                22,
                "Line-art closure was insufficient — using full-frame illustration recovery",
                {"lineArtRegions": marker_count},
            )
            active_pipeline = "illustration"
            rgb = resize_keep_aspect(original_rgb, cfg.max_side)
            fg = np.ones(rgb.shape[:2], dtype=bool)
            processed = preprocess(rgb)
            barrier, barrier_strength = detect_ink_barriers(processed, fg, cfg)
            detail_map = adaptive_detail_map(processed, fg, barrier_strength)
            pixel_colors, initial_centers = quantize_pixels(
                processed,
                fg,
                max(color_count, cfg.colors),
            )
            markers = build_markers(
                pixel_colors,
                fg,
                barrier,
                detail_map,
                cfg.seed_min_area,
                experience_mode,
            )
            marker_count = int(markers.max())
            if marker_count < 2:
                update(
                    28,
                    "Illustration recovery was sparse — using complete photo regions",
                )
                active_pipeline = "photo"
                processed = preprocess_basic_scenic(rgb)
                regions = photo_slic_regions(
                    processed,
                    target_regions=target_regions,
                    min_region_area=max(min_area, 42),
                )
                fg = np.ones(regions.shape, dtype=bool)
                barrier = np.zeros(regions.shape, dtype=bool)
                barrier_strength = np.zeros(regions.shape, dtype=np.float32)
                detail_map = adaptive_detail_map(
                    processed,
                    fg,
                    cv2.Canny(
                        cv2.cvtColor(processed, cv2.COLOR_RGB2GRAY),
                        55,
                        125,
                    ).astype(np.float32) / 255.0,
                )
                marker_count = int(len(np.unique(regions[regions > 0])))
            else:
                regions = illustration_watershed(
                    processed,
                    fg,
                    barrier,
                    barrier_strength,
                    markers,
                )
                regions = adaptive_merge_regions(
                    processed,
                    regions,
                    barrier_strength,
                    detail_map,
                    min_area,
                    experience_mode,
                    target_regions,
                )
                illustration_region_count = int(len(np.unique(regions[regions > 0])))
                illustration_hard_cap = max(int(target_regions * 1.35), target_regions + 60)
                if illustration_region_count > illustration_hard_cap:
                    update(
                        60,
                        "Illustration region count exceeded the hard cap — merging to reduce",
                        {
                            "illustrationRegionCount": illustration_region_count,
                            "hardCap": illustration_hard_cap,
                        },
                    )
                    regions = adaptive_merge_regions(
                        processed,
                        regions,
                        barrier_strength,
                        detail_map,
                        min_area,
                        experience_mode,
                        illustration_hard_cap,
                    )
                    illustration_region_count = int(len(np.unique(regions[regions > 0])))
        update(
            64,
            "Closed paint regions created",
            {
                "lineArtRegions": marker_count,
                "illustrationRegionCount": illustration_region_count,
                "recoveryPipeline": active_pipeline,
            },
        )

    elif active_pipeline == "photo":
        update(16, "Simplifying photographic gradients and visual noise")
        processed = preprocess_basic_scenic(rgb)
        color_count = min(color_count, 24)
        min_area = max(min_area, 70)
        target_regions = min(max(180, target_regions), 520)
        experience_mode = "relaxed"
        Image.fromarray(processed).save(output_dir / "photo_pipeline_preview.png")

        update(25, "Creating complete photo superpixels")
        regions = photo_slic_regions(
            processed,
            target_regions=target_regions,
            min_region_area=min_area,
        )
        fg = np.ones(regions.shape, dtype=bool)
        barrier = np.zeros(regions.shape, dtype=bool)
        barrier_strength = np.zeros(regions.shape, dtype=np.float32)
        detail_map = adaptive_detail_map(
            processed,
            fg,
            cv2.Canny(
                cv2.cvtColor(processed, cv2.COLOR_RGB2GRAY),
                55,
                125,
            ).astype(np.float32) / 255.0,
        )
        marker_count = int(len(np.unique(regions[regions > 0])))
        update(
            64,
            "Photo regions cover the complete image",
            {"photoRegions": marker_count},
        )
    else:
        if artwork_profile.name == "graphic_monochrome":
            processed = preprocess_graphic_monochrome(rgb)
            color_count = min(max(color_count, 8), 12)
            min_area = min(min_area, 14)
            target_regions = min(max(target_regions, 650), 1100)
            tolerance = min(tolerance, 0.10)
            experience_mode = "detailed"
            update(
                16,
                "Preserving monochrome fills, grooves, and metallic line detail",
                {"profile": artwork_profile.name, "targetRegions": target_regions},
            )
        else:
            processed = preprocess(rgb)

        update(20, "Detecting and freezing original ink lines")
        barrier, barrier_strength = detect_ink_barriers(processed, fg, cfg)

        update(26, "Analyzing adaptive detail zones")
        detail_map = adaptive_detail_map(processed, fg, barrier_strength)
        seed_min_area = cfg.seed_min_area
        if artwork_profile.name == "graphic_monochrome":
            gray_detail = cv2.cvtColor(processed, cv2.COLOR_RGB2GRAY)
            local_edges = cv2.Canny(gray_detail, 24, 82).astype(np.float32) / 255.0
            detail_map = np.clip(
                np.maximum(detail_map, cv2.GaussianBlur(local_edges, (0, 0), 0.7)),
                0.0,
                1.0,
            )
            seed_min_area = max(4, cfg.seed_min_area // 3)

        update(31, "Quantizing colors without destroying line boundaries")
        pixel_colors, initial_centers = quantize_pixels(
            processed,
            fg,
            max(color_count, cfg.colors),
        )

        update(43, "Building closed-region markers")
        markers = build_markers(
            pixel_colors,
            fg,
            barrier,
            detail_map,
            seed_min_area,
            experience_mode,
        )
        marker_count = int(markers.max())
        if marker_count < 2:
            update(
                44,
                "Illustration markers were insufficient — switching to full-canvas Photo recovery",
                {"markerSeeds": marker_count},
            )
            active_pipeline = "photo"
            processed = preprocess_basic_scenic(rgb)
            regions = photo_slic_regions(
                processed,
                target_regions=target_regions,
                min_region_area=max(min_area, 42),
            )
            fg = np.ones(regions.shape, dtype=bool)
            barrier = np.zeros(regions.shape, dtype=bool)
            barrier_strength = np.zeros(regions.shape, dtype=np.float32)
            detail_map = adaptive_detail_map(
                processed,
                fg,
                cv2.Canny(
                    cv2.cvtColor(processed, cv2.COLOR_RGB2GRAY),
                    55,
                    125,
                ).astype(np.float32) / 255.0,
            )
            marker_count = int(len(np.unique(regions[regions > 0])))
        else:
            update(55, "Running ink-first watershed", {"markerSeeds": marker_count})
            regions = illustration_watershed(
                processed,
                fg,
                barrier,
                barrier_strength,
                markers,
            )

            update(64, "Removing microscopic islands")
            merge_min_area = min_area
            merge_target_regions = target_regions
            if artwork_profile.name == "graphic_monochrome":
                merge_min_area = max(6, min(min_area, 12))
                merge_target_regions = max(target_regions, 750)
            regions = adaptive_merge_regions(
                processed,
                regions,
                barrier_strength,
                detail_map,
                merge_min_area,
                experience_mode,
                merge_target_regions,
            )

        # Safety check: if Smart Auto chose illustration but the foreground
        # detector retained only a small portion of the full canvas, the result
        # will look like disconnected islands on a white page. Automatically
        # rerun the complete-image Photo pipeline instead.
        foreground_coverage = float(fg.mean())
        segmented_coverage = float((regions > 0).mean())

        if (
            design_style == "smart_auto"
            and (
                foreground_coverage < 0.72
                or segmented_coverage < 0.72
            )
        ):
            update(
                58,
                "Illustration coverage too low — switching to Photo pipeline",
                {
                    "foregroundCoverage": round(foreground_coverage, 3),
                    "segmentedCoverage": round(segmented_coverage, 3),
                },
            )

            active_pipeline = "photo"
            rgb = resize_keep_aspect(original_rgb, cfg.max_side)
            processed = preprocess_basic_scenic(rgb)

            color_count = min(color_count, 24)
            min_area = max(min_area, 70)
            target_regions = min(max(180, target_regions), 520)
            experience_mode = "relaxed"

            regions = photo_slic_regions(
                processed,
                target_regions=target_regions,
                min_region_area=min_area,
            )
            fg = np.ones(regions.shape, dtype=bool)
            barrier = np.zeros(regions.shape, dtype=bool)
            barrier_strength = np.zeros(regions.shape, dtype=np.float32)
            detail_map = adaptive_detail_map(
                processed,
                fg,
                cv2.Canny(
                    cv2.cvtColor(processed, cv2.COLOR_RGB2GRAY),
                    55,
                    125,
                ).astype(np.float32) / 255.0,
            )
            marker_count = int(len(np.unique(regions[regions > 0])))
            crop_meta = {
                "cropped": False,
                "box": [0, 0, original_rgb.shape[1], original_rgb.shape[0]],
            }

            save_completion_source(
                original_rgb,
                output_dir / "completion_reveal.png",
                max_side=6000,
            )
            Image.fromarray(processed).save(
                output_dir / "photo_pipeline_preview.png"
            )

    Image.fromarray(
        np.clip(detail_map * 255, 0, 255).astype(np.uint8)
    ).save(output_dir / "detail_map.png")

    update(66, "Repairing edge coverage and orphan pixels")
    regions = repair_region_coverage(
        regions,
        min_component_area=max(4, min_area // 8),
    )

    update(69, "Validating and repairing customer paintability")
    validation_tolerance = (
        max(tolerance, 1.15)
        if active_pipeline == "lineart"
        else tolerance
    )
    minimum_paintable_area = max(10, min(28, min_area // 2))
    regions, paintability_engine_report = guarantee_paintable_regions(
        regions,
        tolerance=validation_tolerance,
        min_area=minimum_paintable_area,
        min_label_radius=2.25,
    )
    if not paintability_engine_report["passed"]:
        remaining = list(paintability_engine_report.get("remainingInvalid", []))
        fallback_merged = 0
        for item in remaining[:12]:
            try:
                rid = int(item.get("region"))
            except Exception:
                continue
            if not np.any(regions == rid):
                continue
            target = _best_neighbor_for_merge_fast(regions, rid)
            if target is None:
                continue
            regions[regions == rid] = target
            fallback_merged += 1

        if fallback_merged:
            regions = _compact_connected_labels(regions)
            regions, paintability_engine_report = guarantee_paintable_regions(
                regions,
                tolerance=validation_tolerance,
                min_area=minimum_paintable_area,
                min_label_radius=2.25,
                max_passes=2,
            )
            paintability_engine_report["fallbackMergedRegions"] = fallback_merged
            paintability_engine_report["fallbackStrategy"] = "merge-last-invalid-once"

        (output_dir / "paintability_engine_report.json").write_text(
            json.dumps(paintability_engine_report, indent=2),
            encoding="utf-8",
        )

        if not paintability_engine_report["passed"]:
            raise ValueError(
                "Guaranteed Paintability validation failed before export. "
                f"{len(paintability_engine_report['remainingInvalid'])} "
                "regions could not be repaired after bounded fallback."
            )

    coverage_stats = region_coverage_stats(regions)
    (output_dir / "paintability_engine_report.json").write_text(
        json.dumps(paintability_engine_report, indent=2),
        encoding="utf-8",
    )

    effective_vector_min = minimum_paintable_area
    region_ids = [
        int(rid)
        for rid in np.unique(regions)
        if rid > 0
    ]

    illustration_hard_cap = max(int(target_regions * 1.35), target_regions + 60)
    if active_pipeline == "illustration" and len(region_ids) > illustration_hard_cap:
        raise ValueError(
            f"Illustration region cap was not achieved before export: "
            f"{len(region_ids)} > {illustration_hard_cap}"
        )

    if not region_ids:
        raise ValueError(
            "No usable paint regions were generated. Reduce minimum region area."
        )

    update(72, "Assigning final paint colors", {"regionCandidates": len(region_ids)})
    if active_pipeline == "lineart":
        region_to_color, centers, original_region_means = assign_premium_lineart_palette(
            regions,
            processed,
        )
    else:
        region_to_color, centers, original_region_means = assign_region_palette(
            processed,
            regions,
            region_ids,
            color_count,
        )

    update(80, "Vectorizing closed paint regions", {"regionCandidates": len(region_ids)})
    h, w = processed.shape[:2]
    records: List[dict] = []
    region_labels: dict[str, int] = {}
    rendered_coverage = np.zeros((h, w), dtype=bool)

    total_region_ids = len(region_ids)
    for index, rid in enumerate(region_ids, start=1):
        if index == 1 or index % 100 == 0 or index == total_region_ids:
            update(
                min(89, 80 + int(9 * index / max(1, total_region_ids))),
                "Vectorizing closed paint regions",
                {"vectorized": index - 1, "regionCandidates": total_region_ids},
            )
        mask = regions == rid
        area = int(mask.sum())
        region_detail = float(detail_map[mask].mean()) if area else 0.0
        poly = mask_polygon(mask, validation_tolerance)
        if poly is None:
            raise ValueError(
                f"Validated region {rid} became non-vectorizable during export."
            )
        path = polygon_path(poly)
        if not path:
            raise ValueError(
                f"Validated region {rid} produced an empty SVG path."
            )

        rendered_coverage |= mask
        color_id = region_to_color[rid]
        palette_fill = rgb_to_hex(centers[color_id])
        original_fill = rgb_to_hex(
            np.clip(np.round(original_region_means[rid]), 0, 255).astype(np.uint8)
        )
        x, y, radius = label_point(mask)
        region_id = f"region_{index}"
        region_labels[region_id] = int(rid)
        records.append(
            {
                "regionId": region_id,
                "colorId": str(color_id + 1),
                "fillColor": palette_fill,
                "paletteColor": palette_fill,
                "originalColor": original_fill,
                "painted": False,
                "area": area,
                "detailScore": round(region_detail, 4),
                "paintability": (
                    "tiny" if area < 80
                    else "small" if area < 180
                    else "comfortable"
                ),
                "path": path,
                "label": {
                    "x": x,
                    "y": y,
                    "radius": radius,
                    "fontSize": max(5.2, min(14.0, radius * 0.68)),
                    "visible": True,
                },
            }
        )

    if not records:
        raise ValueError("No vector paint regions could be created.")

    missing_render_pixels = int((~rendered_coverage).sum())
    rendered_coverage_percent = round(
        float(rendered_coverage.mean()) * 100.0,
        6,
    )

    if missing_render_pixels > 0:
        raise ValueError(
            "Pixel Coverage Guarantee failed: "
            f"{missing_render_pixels} canvas pixels were not converted into "
            "selectable paint regions. The package was not exported."
        )

    records.sort(key=lambda item: item["area"], reverse=True)

    update(90, "Tracing preserved ink overlay", {"finalRegions": len(records)})
    ink_paths = trace_ink_paths(barrier, fg)

    palette = [
        {
            "colorId": str(i + 1),
            "hex": rgb_to_hex(center),
            "rgb": [int(v) for v in center],
            "label": f"Color {i + 1}",
        }
        for i, center in enumerate(centers)
    ]

    palette_preview = np.full((h, w, 3), 255, dtype=np.uint8)
    reference_preview = np.full((h, w, 3), 255, dtype=np.uint8)
    for rid in region_ids:
        palette_preview[regions == rid] = centers[region_to_color[rid]]
        reference_preview[regions == rid] = np.clip(
            np.round(original_region_means[rid]), 0, 255
        ).astype(np.uint8)
    Image.fromarray(palette_preview).save(output_dir / "preview_palette.png")
    Image.fromarray(reference_preview).save(output_dir / "preview_reference.png")
    Image.fromarray(reference_preview).save(output_dir / "preview.png")
    source_gray = cv2.cvtColor(processed, cv2.COLOR_RGB2GRAY)
    preview_gray = cv2.cvtColor(reference_preview, cv2.COLOR_RGB2GRAY)
    source_edges = cv2.Canny(source_gray, 45, 120) > 0
    preview_edges = cv2.Canny(preview_gray, 45, 120) > 0
    edge_union = int(np.logical_or(source_edges, preview_edges).sum())
    edge_overlap = int(np.logical_and(source_edges, preview_edges).sum())
    edge_retention = round(edge_overlap / edge_union, 4) if edge_union else 1.0
    tonal_mae = round(float(np.abs(
        source_gray.astype(np.float32) - preview_gray.astype(np.float32)
    ).mean()), 3)
    fidelity_report = {
        "artworkProfile": artwork_profile.name,
        "edgeRetention": edge_retention,
        "tonalMeanAbsoluteError": tonal_mae,
        "regions": len(records),
        "passed": (
            edge_retention >= (0.18 if artwork_profile.name == "graphic_monochrome" else 0.10)
            and tonal_mae <= 42.0
        ),
    }
    (output_dir / "visual_fidelity_report.json").write_text(
        json.dumps(fidelity_report, indent=2),
        encoding="utf-8",
    )
    Image.fromarray(palette_preview).save(output_dir / "painted_palette_preview.png")


    region_css = (
        f".paint-region{{stroke:#202020;stroke-width:{outline_width};"
        "stroke-linecap:round;stroke-linejoin:round;"
        "vector-effect:non-scaling-stroke;cursor:pointer}}"
    )
    text_css = (
        ".region-number{font-family:Arial,sans-serif;font-weight:800;"
        "text-anchor:middle;dominant-baseline:middle;pointer-events:none;"
        "paint-order:stroke;stroke:#ffffff;stroke-width:2.2px;"
        "stroke-linejoin:round;fill:#111111}"
    )
    hit_css = (
        ".region-hit{fill:rgba(0,0,0,0.001);stroke:rgba(0,0,0,0.001);stroke-width:8px;"
        "vector-effect:non-scaling-stroke;pointer-events:all;cursor:pointer}"
    )
    ink_css = (
        ".ink-line{fill:none;stroke:#171717;stroke-width:0.34;"
        "stroke-linecap:round;stroke-linejoin:round;"
        "vector-effect:non-scaling-stroke;pointer-events:none}"
    )

    palette_svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}">',
        f"<style>{region_css}{ink_css}</style>",
    ]
    reference_svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}">',
        f"<style>{region_css}{ink_css}</style>",
    ]
    blank = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}">',
        f"<style>{region_css}.paint-region{{fill:#fff}}{text_css}{hit_css}{ink_css}</style>",
    ]

    for record in records:
        attrs = (
            f'id="{record["regionId"]}" '
            f'data-region-id="{record["regionId"]}" '
            f'data-color-id="{record["colorId"]}" '
            f'data-fill-color="{record["fillColor"]}" '
            f'fill-rule="evenodd" d="{record["path"]}"'
        )
        palette_svg.append(
            f'<path class="paint-region" {attrs} fill="{record["paletteColor"]}"/>'
        )
        reference_svg.append(
            f'<path class="paint-region" {attrs} fill="{record["originalColor"]}"/>'
        )
        blank.append(f'<path class="paint-region" {attrs} fill="#ffffff"/>')
        blank.append(
            f'<path class="region-hit" '
            f'data-target-region="{record["regionId"]}" '
            f'data-color-id="{record["colorId"]}" '
            f'fill-rule="evenodd" fill="rgba(0,0,0,0.001)" '
            f'd="{record["path"]}"/>'
        )

    for index, path in enumerate(ink_paths, start=1):
        element = f'<path class="ink-line" id="ink_{index}" d="{path}"/>'
        palette_svg.append(element)
        reference_svg.append(element)
        blank.append(element)

    for record in records:
        label = record["label"]
        blank.append(
            f'<text class="region-number" '
            f'data-region-id="{record["regionId"]}" '
            f'data-color-id="{record["colorId"]}" '
            f'x="{label["x"]:.2f}" y="{label["y"]:.2f}" '
            f'font-size="{label["fontSize"]:.1f}px">'
            f'{record["colorId"]}</text>'
        )

    palette_svg.append("</svg>")
    reference_svg.append("</svg>")
    blank.append("</svg>")

    blank_svg_text = "\n".join(blank)
    simulation_report = simulate_complete_painting(
        records,
        palette,
        blank_svg_text,
        regions,
        region_labels,
)
    (output_dir / "paintability_simulation.json").write_text(
        json.dumps(simulation_report, indent=2),
        encoding="utf-8",
    )
    if not simulation_report["passed"]:
        raise ValueError(
            "Pre-export customer simulation failed. "
            f"{simulation_report['simulated']} of "
            f"{simulation_report['expected']} regions passed."
        )

    (output_dir / "paintMap_palette.svg").write_text(
        "\n".join(palette_svg),
        encoding="utf-8",
    )
    (output_dir / "paintMap_reference.svg").write_text(
        "\n".join(reference_svg),
        encoding="utf-8",
    )
    # Backward compatibility: colored now means the faithful reference preview.
    (output_dir / "paintMap_colored.svg").write_text(
        "\n".join(reference_svg),
        encoding="utf-8",
    )
    (output_dir / "paintMap.svg").write_text(
        blank_svg_text,
        encoding="utf-8",
    )

    build_interactive_player(
        output_dir,
        records,
        palette,
        blank_svg_text,
        "\n".join(reference_svg),
    )

    ink_svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}">',
        f"<style>{ink_css}</style>",
    ]
    ink_svg.extend(
        f'<path class="ink-line" id="ink_{i}" d="{path}"/>'
        for i, path in enumerate(ink_paths, start=1)
    )
    ink_svg.append("</svg>")
    (output_dir / "ink_overlay.svg").write_text(
        "\n".join(ink_svg),
        encoding="utf-8",
    )

    (output_dir / "regions.json").write_text(
        json.dumps(records, indent=2),
        encoding="utf-8",
    )
    (output_dir / "palette.json").write_text(
        json.dumps(palette, indent=2),
        encoding="utf-8",
    )

    metadata = {
        "engine": "Euqilegna Version 11.2 Complete Paintability Engine",
        "inputFile": input_path.name,
        "width": w,
        "height": h,
        "regions": len(records),
        "inkPaths": len(ink_paths),
        "markerSeeds": marker_count,
        "colors": len(palette),
        "preset": preset,
        "minRegionArea": min_area,
        "outlineWidth": outline_width,
        "simplifyTolerance": tolerance,
        "autoCrop": auto_crop,
        "crop": crop_meta,
        "completionImage": "finished_masterpiece.png",
            "progressiveRevealImage": "completion_reveal.png",
        "inkOverlay": "ink_overlay.svg",
        "detailMap": "detail_map.png",
        "paintabilityEngine": "paintability_engine_report.json",
        "paintabilitySimulation": "paintability_simulation.json",
        "paintabilityGuaranteed": True,
        "experienceMode": experience_mode,
        "targetRegions": target_regions,
        "designStyle": design_style,
        "activePipeline": active_pipeline,
        "pipelineAnalysis": analysis,
        "artworkProfile": artwork_profile.name,
        "artworkProfileAnalysis": profile_analysis,
        "foregroundAnalysis": foreground_meta,
        "automaticRecoveryEnabled": True,
        "modularCompilerVersion": "11.0",
        "visualFidelityReport": "visual_fidelity_report.json",
        "visualFidelity": fidelity_report,
        "finalCanvasCoverage": round(float((regions > 0).mean()), 6),
        "pixelCoveragePercent": rendered_coverage_percent,
        "coverageErrors": 0 if missing_render_pixels == 0 else 1,
        "orphanPixels": missing_render_pixels,
        "edgeRepair": "completed",
        "coverageGuarantee": missing_render_pixels == 0,
        "completionImageSource": "artistic_master",
        "finishMode": finish_mode,
        "tinyRegions": sum(1 for item in records if item["paintability"] == "tiny"),
        "smallRegions": sum(1 for item in records if item["paintability"] == "small"),
    }
    if "metadata_validation_summary" in locals():
        metadata.update(metadata_validation_summary)

    (output_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2),
        encoding="utf-8",
    )

    update(94, "Scoring customer paintability")
    quality_report = build_quality_report(
        output_dir,
        records,
        palette,
        metadata,
    )
    metadata["qualityStatus"] = quality_report["status"]
    metadata["paintabilityScore"] = quality_report["paintabilityScore"]
    metadata["estimatedInteractiveMinutes"] = quality_report["estimatedInteractiveMinutes"]
    (output_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2),
        encoding="utf-8",
    )

    if generate_pdf:
        update(96, "Creating palette PDF", {"finalRegions": len(records), "inkPaths": len(ink_paths)})
        build_pdf(output_dir / "palette_guide.pdf", palette, metadata)

    update(
        100,
        "Complete",
        {
            "finalRegions": len(records),
            "inkPaths": len(ink_paths),
            "colors": len(palette),
            "pixelCoveragePercent": rendered_coverage_percent,
            "orphanPixels": missing_render_pixels,
            "edgeRepair": "completed",
            "totalSeconds": round(time.time() - started_at, 1),
        },
    )
    return metadata

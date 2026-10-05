from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np


@dataclass(frozen=True, slots=True)
class ArtworkProfile:
    name: str
    pipeline: str
    use_full_canvas: bool
    allow_auto_crop: bool
    foreground_required: bool
    color_cap: int | None
    minimum_region_area: int
    target_region_min: int
    target_region_max: int
    experience_mode: str
    simplify_tolerance_floor: float
    description: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


PROFILES: dict[str, ArtworkProfile] = {
    "portrait": ArtworkProfile(
        name="portrait",
        pipeline="illustration",
        use_full_canvas=False,
        allow_auto_crop=True,
        foreground_required=False,
        color_cap=34,
        minimum_region_area=20,
        target_region_min=460,
        target_region_max=1150,
        experience_mode="detailed",
        simplify_tolerance_floor=0.12,
        description="Character or portrait artwork with meaningful background detail.",
    ),
    "line_art": ArtworkProfile(
        name="line_art",
        pipeline="lineart",
        use_full_canvas=True,
        allow_auto_crop=False,
        foreground_required=False,
        color_cap=28,
        minimum_region_area=28,
        target_region_min=300,
        target_region_max=880,
        experience_mode="relaxed",
        simplify_tolerance_floor=0.20,
        description="High-contrast line art, vinyl graphics, coloring-book work, and graphic illustration.",
    ),
    "graphic_monochrome": ArtworkProfile(
    name="graphic_monochrome",
    pipeline="illustration",
    use_full_canvas=True,
    allow_auto_crop=False,
    foreground_required=False,
    color_cap=12,
    minimum_region_area=36,
    target_region_min=520,
    target_region_max=1000,
    experience_mode="detailed",
    simplify_tolerance_floor=0.18,
    description="Black, cream, and gray graphic artwork with broad dark fills plus fine protected line detail.",
  ),
  "landscape": ArtworkProfile(
        name="landscape",
        pipeline="photo",
        use_full_canvas=True,
        allow_auto_crop=False,
        foreground_required=False,
        color_cap=28,
        minimum_region_area=54,
        target_region_min=400,
        target_region_max=1050,
        experience_mode="relaxed",
        simplify_tolerance_floor=0.24,
        description="Full-frame scenic, café, street, cityscape, or environmental artwork.",
    ),
    "decorative": ArtworkProfile(
        name="decorative",
        pipeline="illustration",
        use_full_canvas=True,
        allow_auto_crop=False,
        foreground_required=False,
        color_cap=32,
        minimum_region_area=18,
        target_region_min=500,
        target_region_max=1250,
        experience_mode="detailed",
        simplify_tolerance_floor=0.13,
        description="Ornamental, botanical, koi, peacock, floral, symmetrical, or patterned artwork.",
    ),
    "full_frame_illustration": ArtworkProfile(
        name="full_frame_illustration",
        pipeline="illustration",
        use_full_canvas=True,
        allow_auto_crop=False,
        foreground_required=False,
        color_cap=34,
        minimum_region_area=20,
        target_region_min=460,
        target_region_max=1180,
        experience_mode="detailed",
        simplify_tolerance_floor=0.14,
        description="Illustration whose subject and background both belong to the composition.",
    ),
    "photo": ArtworkProfile(
        name="photo",
        pipeline="photo",
        use_full_canvas=True,
        allow_auto_crop=False,
        foreground_required=False,
        color_cap=24,
        minimum_region_area=70,
        target_region_min=340,
        target_region_max=900,
        experience_mode="relaxed",
        simplify_tolerance_floor=0.28,
        description="Photographic or gradient-heavy source artwork.",
    ),
}


PREMIUM_PROFILE_OVERRIDES = {
    "midnight-jazz": "portrait",
    "noir-vinyl": "graphic_monochrome",
    "afrofuturist-stargazer": "portrait",
    "golden-koi": "decorative",
    "rainy-cafe": "landscape",
    "botanical-portrait": "decorative",
    "art-deco-peacock": "decorative",
    "brownstone-jazz": "landscape",
}


def _image_metrics(rgb: np.ndarray) -> dict[str, float]:
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)

    edges = cv2.Canny(gray, 60, 140)
    edge_density = float((edges > 0).mean())
    saturation = float(hsv[..., 1].mean() / 255.0)
    value_std = float(gray.std() / 255.0)

    dark_ratio = float((gray < 45).mean())
    light_ratio = float((gray > 220).mean())
    near_binary_ratio = float(((gray < 45) | (gray > 210)).mean())

    h, w = gray.shape
    aspect_ratio = float(w / max(1, h))

    # Horizontal/vertical edge balance helps identify decorative line work.
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    orientation_balance = float(
        min(np.abs(gx).mean(), np.abs(gy).mean())
        / max(1e-6, max(np.abs(gx).mean(), np.abs(gy).mean()))
    )

    return {
        "edgeDensity": edge_density,
        "saturation": saturation,
        "valueStd": value_std,
        "darkRatio": dark_ratio,
        "lightRatio": light_ratio,
        "nearBinaryRatio": near_binary_ratio,
        "aspectRatio": aspect_ratio,
        "orientationBalance": orientation_balance,
    }


def infer_profile(
    rgb: np.ndarray,
    input_path: Path | str | None = None,
    pipeline_analysis: dict[str, Any] | None = None,
) -> tuple[ArtworkProfile, dict[str, Any]]:
    path = Path(input_path) if input_path else None
    stem = path.stem.lower() if path else ""

    for sample_name, profile_name in PREMIUM_PROFILE_OVERRIDES.items():
        if sample_name in stem:
            profile = PROFILES[profile_name]
            return profile, {
                "source": "premium_override",
                "matchedArtwork": sample_name,
                "metrics": _image_metrics(rgb),
                "profile": profile.to_dict(),
            }

    metrics = _image_metrics(rgb)
    detected_pipeline = str((pipeline_analysis or {}).get("pipeline", ""))

    # Strong black/cream or graphic-line signal.
    if (
        metrics["nearBinaryRatio"] >= 0.72
        and metrics["edgeDensity"] >= 0.045
        and metrics["saturation"] <= 0.34
    ):
        profile_name = (
            "graphic_monochrome"
            if metrics["darkRatio"] >= 0.30
            else "line_art"
        )
    # Wide environmental artwork is usually full-frame.
    elif metrics["aspectRatio"] >= 1.28 and detected_pipeline == "photo":
        profile_name = "landscape"
    # Highly saturated, edge-dense, balanced art tends to be decorative.
    elif (
        metrics["saturation"] >= 0.42
        and metrics["edgeDensity"] >= 0.075
        and metrics["orientationBalance"] >= 0.55
    ):
        profile_name = "decorative"
    elif detected_pipeline == "photo":
        profile_name = "photo"
    elif metrics["aspectRatio"] >= 1.35:
        profile_name = "full_frame_illustration"
    else:
        profile_name = "portrait"

    profile = PROFILES[profile_name]
    return profile, {
        "source": "automatic_classifier",
        "metrics": metrics,
        "detectedPipeline": detected_pipeline,
        "profile": profile.to_dict(),
    }


def apply_profile_settings(
    profile: ArtworkProfile,
    *,
    color_count: int,
    min_area: int,
    target_regions: int,
    tolerance: float,
    experience_mode: str,
) -> dict[str, Any]:
    if profile.color_cap is not None:
        color_count = min(color_count, profile.color_cap)

    return {
        "color_count": max(8, color_count),
        "min_area": max(min_area, profile.minimum_region_area),
        "target_regions": min(
            max(target_regions, profile.target_region_min),
            profile.target_region_max,
        ),
        "tolerance": max(tolerance, profile.simplify_tolerance_floor),
        "experience_mode": profile.experience_mode or experience_mode,
    }


__all__ = [
    "ArtworkProfile",
    "PROFILES",
    "PREMIUM_PROFILE_OVERRIDES",
    "infer_profile",
    "apply_profile_settings",
]

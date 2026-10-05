from __future__ import annotations

from typing import Any


def build_attempts(source_analysis: dict[str, Any], requested: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Generate bounded recovery attempts. The user's requested settings are tried
    first; later attempts change only the processing strategy needed to recover
    a valid package.
    """
    family = source_analysis.get("family", "general_artwork")
    attempts: list[dict[str, Any]] = []

    primary = dict(requested)
    primary["name"] = "smart-auto"
    attempts.append(primary)

    if family == "graphic_monochrome":
        detailed = dict(requested)
        detailed.update({
            "name": "graphic-detail-recovery",
            "design_style": "smart_auto",
            "auto_crop": False,
            "colors": min(max(int(requested.get("colors") or 12), 8), 14),
            "min_region_area": max(int(requested.get("min_region_area") or 36), 36),
            "target_regions": min(int(requested.get("target_regions") or 760), 760),
            "simplify_tolerance": max(float(requested.get("simplify_tolerance") or 0.18), 0.18),
            "experience_mode": "detailed",
        })
        attempts.append(detailed)

    illustration = dict(requested)
    illustration.update({
    "name": "full-canvas-illustration-recovery",
    "design_style": "illustration",
    "auto_crop": False,
    "min_region_area": max(int(requested.get("min_region_area") or 36), 36),
    "target_regions": min(int(requested.get("target_regions") or 760), 760),
    "experience_mode": "relaxed",
})
    attempts.append(illustration)

    photo = dict(requested)
    photo.update({
        "name": "full-canvas-photo-recovery",
        "design_style": "photo",
        "auto_crop": False,
        "min_region_area": max(42, int(requested.get("min_region_area") or 42)),
        "target_regions": min(max(420, int(requested.get("target_regions") or 700)), 950),
        "experience_mode": "relaxed",
    })
    attempts.append(photo)

    # De-duplicate equivalent settings.
    unique: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    for attempt in attempts:
        key = tuple(sorted((k, str(v)) for k, v in attempt.items() if k != "name"))
        if key not in seen:
            seen.add(key)
            unique.append(attempt)
    return unique

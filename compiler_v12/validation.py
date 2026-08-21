from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import re

import cv2
import numpy as np
from PIL import Image


REQUIRED_FILES = (
    "interactive_player.html",
    "metadata.json",
    "preview.png",
)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _image_similarity(original_path: Path, preview_path: Path) -> dict[str, float]:
    original = np.array(Image.open(original_path).convert("RGB"))
    preview = np.array(Image.open(preview_path).convert("RGB"))

    if original.shape[:2] != preview.shape[:2]:
        preview = cv2.resize(
            preview,
            (original.shape[1], original.shape[0]),
            interpolation=cv2.INTER_AREA,
        )

    og = cv2.cvtColor(original, cv2.COLOR_RGB2GRAY)
    pg = cv2.cvtColor(preview, cv2.COLOR_RGB2GRAY)

    mae = float(np.abs(og.astype(np.float32) - pg.astype(np.float32)).mean())
    tonal_similarity = max(0.0, 1.0 - mae / 255.0)

    oe = cv2.Canny(og, 45, 120) > 0
    pe = cv2.Canny(pg, 45, 120) > 0
    union = int(np.logical_or(oe, pe).sum())
    overlap = int(np.logical_and(oe, pe).sum())
    edge_iou = (overlap / union) if union else 1.0

    # Tone matters more than exact edge overlap because paint-by-number
    # intentionally simplifies edges.
    score = tonal_similarity * 0.72 + edge_iou * 0.28
    return {
        "score": round(score, 5),
        "tonalSimilarity": round(tonal_similarity, 5),
        "edgeIoU": round(edge_iou, 5),
        "meanAbsoluteError": round(mae, 3),
    }


def validate_package(
    output_dir: str | Path,
    *,
    source_path: str | Path,
    minimum_similarity: float = 0.68,
    target_regions: int | None = None,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    source_path = Path(source_path)
    errors: list[str] = []
    warnings: list[str] = []

    for filename in REQUIRED_FILES:
        if not (output_dir / filename).exists():
            errors.append(f"Missing required file: {filename}")

    metadata = _load_json(output_dir / "metadata.json")
    validation_report = _load_json(output_dir / "validation_report.json")

    if validation_report and not validation_report.get("passed", False):
        errors.extend(validation_report.get("errors", []))

    regions = int(metadata.get("regions") or metadata.get("finalRegions") or 0)
    colors = int(metadata.get("colors") or 0)
    if regions <= 0:
        errors.append("No paintable regions were exported.")
    if colors <= 0:
        errors.append("No paint palette was exported.")

    if target_regions and target_regions > 0:
        hard_region_cap = max(int(target_regions * 1.35), target_regions + 60)
        if regions > hard_region_cap:
            errors.append(
                f"Region count exploded beyond the usability budget "
                f"({regions} > {hard_region_cap}; target {target_regions})."
            )

        tiny_regions = int(metadata.get("tinyRegions") or 0)
        tiny_ratio = (tiny_regions / regions) if regions else 0.0
        if regions >= 100 and tiny_ratio > 0.40:
            errors.append(
                f"Too many tiny paint regions for customer use "
                f"({tiny_regions}/{regions}, {tiny_ratio:.1%})."
            )

    player_path = output_dir / "interactive_player.html"
    if player_path.exists():
        player = player_path.read_text(encoding="utf-8", errors="ignore")
        if "const REGIONS=" not in player:
            errors.append("Interactive player has no embedded region data.")
        if "const PALETTE=" not in player:
            errors.append("Interactive player has no embedded palette data.")
        if "Paint Again" not in player:
            warnings.append("Paint Again control is missing.")
        if "Canvas Locked" not in player:
            warnings.append("Canvas Lock control is missing.")

    preview_path = output_dir / "preview_reference.png"
    if not preview_path.exists():
        preview_path = output_dir / "preview.png"

    similarity = {"score": 0.0}
    if source_path.exists() and preview_path.exists():
        similarity = _image_similarity(source_path, preview_path)
        if similarity["score"] < minimum_similarity:
            errors.append(
                "Visual fidelity is below the publishing threshold "
                f"({similarity['score']:.3f} < {minimum_similarity:.3f})."
            )

    passed = not errors
    report = {
        "passed": passed,
        "regions": regions,
        "colors": colors,
        "similarity": similarity,
        "errors": errors,
        "warnings": warnings,
    }
    (output_dir / "v12_validation_report.json").write_text(
        json.dumps(report, indent=2),
        encoding="utf-8",
    )
    return report

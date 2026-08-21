from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any
import json

from numbering_engine import label_is_visible


def validate_compiled_records(records: list[dict], palette_size: int) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []

    region_ids = [str(item.get("regionId", "")) for item in records]
    duplicate_ids = [key for key, count in Counter(region_ids).items() if key and count > 1]
    if duplicate_ids:
        errors.append(f"Duplicate region IDs: {duplicate_ids[:10]}")

    used_colors: set[int] = set()
    missing_labels: list[str] = []
    invalid_paths: list[str] = []

    for item in records:
        region_id = str(item.get("regionId", "unknown"))
        path = str(item.get("path", "")).strip()
        if not path:
            invalid_paths.append(region_id)

        try:
            color_id = int(item.get("colorId"))
            used_colors.add(color_id)
            if color_id < 1 or color_id > palette_size:
                errors.append(
                    f"{region_id} references palette color {color_id}, "
                    f"outside 1-{palette_size}."
                )
        except (TypeError, ValueError):
            errors.append(f"{region_id} has an invalid color ID.")

        if not label_is_visible(item):
            missing_labels.append(region_id)

        if item.get("painted") not in (False, None):
            warnings.append(
                f"{region_id} was exported in a pre-painted state and was reset."
            )

    if invalid_paths:
        errors.append(f"Regions with empty SVG paths: {invalid_paths[:10]}")
    if missing_labels:
        errors.append(f"Regions without usable number labels: {missing_labels[:10]}")

    unused_colors = sorted(set(range(1, palette_size + 1)) - used_colors)
    if unused_colors:
        warnings.append(f"Unused palette colors: {unused_colors}")

    return {
        "passed": not errors,
        "regionCount": len(records),
        "paletteSize": palette_size,
        "usedColors": sorted(used_colors),
        "errors": errors,
        "warnings": warnings,
    }


def write_validation_report(output_dir: Path, report: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "validation_report.json").write_text(
        json.dumps(report, indent=2),
        encoding="utf-8",
    )

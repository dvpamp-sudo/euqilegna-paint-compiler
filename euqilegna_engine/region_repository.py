from __future__ import annotations

from pathlib import Path
import json

from .models import RegionGeometry


def load_regions(output_dir: Path) -> list[RegionGeometry]:
    path = output_dir / "regions.json"
    if not path.is_file():
        raise FileNotFoundError("regions.json was not generated.")

    records = json.loads(path.read_text(encoding="utf-8"))
    regions: list[RegionGeometry] = []

    for record in records:
        label = record.get("label") or {}
        regions.append(
            RegionGeometry(
                region_id=str(record["regionId"]),
                color_id=str(record["colorId"]),
                area=int(record.get("area", 0)),
                path=str(record.get("path", "")),
                label_x=float(label.get("x", -1)),
                label_y=float(label.get("y", -1)),
                label_radius=float(label.get("radius", 0)),
                palette_color=str(record.get("paletteColor", "#ffffff")),
                original_color=str(record.get("originalColor", "#ffffff")),
                paintability=str(record.get("paintability", "unknown")),
                metadata=record,
            )
        )

    return regions


def load_palette(output_dir: Path) -> list[dict]:
    path = output_dir / "palette.json"
    if not path.is_file():
        raise FileNotFoundError("palette.json was not generated.")
    return json.loads(path.read_text(encoding="utf-8"))

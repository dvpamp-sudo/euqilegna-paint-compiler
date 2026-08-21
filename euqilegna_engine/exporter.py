from __future__ import annotations

from pathlib import Path
import zipfile


EXPORT_FILES = (
    "interactive_player.html",
    "paintMap.svg",
    "paintMap_palette.svg",
    "paintMap_reference.svg",
    "palette.json",
    "regions.json",
    "metadata.json",
    "platform_quality_report.json",
    "paintability_engine_report.json",
    "paintability_simulation.json",
    "finished_masterpiece.png",
    "completion_original.png",
    "completion_reveal.png",
    "paint_by_number.pdf",
)


def export_zip(output_dir: Path, zip_path: Path) -> Path:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename in EXPORT_FILES:
            path = output_dir / filename
            if path.is_file():
                archive.write(path, filename)
    return zip_path

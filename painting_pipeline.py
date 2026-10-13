"""Bridge between the paint compiler core and the Painting data structure.

The compiler core turns an image into a paint-by-number package (regions.json,
palette.json, paintMap.svg, ...). This module records such a run as a Painting
so compiled artwork can be listed, tracked and re-opened like any other piece.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from paintings import Painting, PaintingStatus
from testing_runtime_v10 import RuntimeDatabase

# The compiler core writes these into every package directory.
SVG_FILENAME = "paintMap.svg"
PLAYER_FILENAME = "interactive_player.html"

# Beta uploads aim at a grown-up coloring-book page: the source's ink lines are
# kept as ink, the palette is deep and the regions small, and the printed
# outlines stay thin — the detailed line art a marker painter expects.
BETA_SETTINGS: dict[str, Any] = {
    "preset": "illustration",
    "design_style": "coloring_book",
    "colors": 20,
    "min_region_area": 28,
    "experience_mode": "relaxed",
    "target_regions": 360,
    "outline_width": 0.28,
    "simplify_tolerance": 0.24,
    "smoothing_passes": 0,
    "auto_crop": True,
    "exclude_background": True,
    "generate_pdf": False,
    "finish_mode": "original",
}


def record_painting(
    metadata: dict[str, Any],
    source_path: str | Path,
    package_dir: str | Path,
    *,
    runtime: RuntimeDatabase | None = None,
    title: str | None = None,
    medium: str = "Paint-by-number",
) -> Painting:
    """Record a finished compiler run as a Painting.

    The compiled package stays on disk; the Painting points at it and
    summarises what came out of the compiler core.
    """
    source_path = Path(source_path)
    package_dir = Path(package_dir)
    painting = Painting(
        title=title or source_path.stem,
        date_created=date.today().isoformat(),
        medium=medium,
        status=PaintingStatus.PLANNED,
        source_image=str(source_path),
        package_dir=str(package_dir),
        svg_path=_package_asset(package_dir, SVG_FILENAME),
        player_path=_package_asset(package_dir, PLAYER_FILENAME),
        region_count=int(metadata.get("regions") or 0),
        color_count=int(metadata.get("colors") or 0),
    )
    if runtime is not None:
        runtime.save_painting(painting)
    return painting


def _package_asset(package_dir: Path, filename: str) -> str | None:
    """Reference a compiler-generated file, or ``None`` if it was not written."""
    candidate = package_dir / filename
    return str(candidate) if candidate.is_file() else None

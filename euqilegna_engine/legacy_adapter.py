from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from compiler import compile_artwork as legacy_compile_artwork
from .models import CompileRequest


ProgressCallback = Callable[[int, str, dict[str, Any]], None]


def run_legacy_engine(
    request: CompileRequest,
    progress_callback: ProgressCallback | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """
    Compatibility adapter.

    The proven Version 9 image/geometry algorithms remain available while the
    orchestration, QA, storage, API, and export layers are separated into modules.
    """
    return legacy_compile_artwork(
        input_path=request.input_path,
        output_dir=request.output_dir,
        preset=request.preset,
        design_style=request.design_style,
        colors=request.colors,
        min_region_area=request.min_region_area,
        experience_mode=request.experience_mode,
        target_regions=request.target_regions,
        outline_width=request.outline_width,
        simplify_tolerance=request.simplify_tolerance,
        auto_crop=request.auto_crop,
        generate_pdf=request.generate_pdf,
        finish_mode=request.finish_mode,
        progress_callback=progress_callback,
        cancel_check=cancel_check,
    )

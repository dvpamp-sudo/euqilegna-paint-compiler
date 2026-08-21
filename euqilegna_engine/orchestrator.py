from __future__ import annotations

from pathlib import Path
from typing import Any, Callable
import shutil

from .image_loader import load_rgb
from .legacy_adapter import run_legacy_engine
from .models import CompileRequest, CompileResult
from .paintability_validator import validate_package, write_quality_report
from .region_repository import load_palette, load_regions


class CompilationQualityError(RuntimeError):
    pass


class CompilerOrchestrator:
    """
    Stable application boundary for the Euqilegna compiler.

    The API and UI call this class instead of importing the large image-processing
    implementation directly.
    """

    def compile(
        self,
        request: CompileRequest,
        progress_callback: Callable[[int, str, dict[str, Any]], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> CompileResult:
        load_rgb(request.input_path)

        if request.output_dir.exists():
            shutil.rmtree(request.output_dir)
        request.output_dir.mkdir(parents=True, exist_ok=True)

        metadata = run_legacy_engine(
            request,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
        )

        regions = load_regions(request.output_dir)
        palette = load_palette(request.output_dir)
        quality = validate_package(request.output_dir, regions, palette)
        write_quality_report(request.output_dir, quality)

        if not quality.passed:
            raise CompilationQualityError(
                "Platform QA blocked export: "
                f"{len(quality.issues)} paintability issue(s) remain."
            )

        metadata = dict(metadata)
        metadata["platformEngine"] = "Euqilegna Modular Compiler Platform 10.0"
        metadata["platformQualityPassed"] = True

        return CompileResult(
            output_dir=request.output_dir,
            metadata=metadata,
            quality_report=quality,
        )

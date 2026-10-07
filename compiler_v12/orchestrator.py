from __future__ import annotations

from pathlib import Path
from typing import Callable, Any
import json
import shutil
import tempfile
import time

from compiler_core import compile_artwork as compile_core
from .analyzer import analyze_source
from .models import AttemptResult
from .recovery import build_attempts
from .validation import validate_package


# Recovery is bounded by wall-clock time, not attempt count: a fast pipeline
# should get to try every planned strategy, while a slow one still cannot burn
# through hours of retries.
RECOVERY_TIME_BUDGET_SECONDS = 900.0

# A package at or above this fidelity publishes cleanly.
PUBLISH_SIMILARITY = 0.68

# Fidelity compares flat paint regions with the source pixel by pixel, so
# hand-hatched or scribbled line art scores low however good the package is
# (dense hatching alone costs ~0.1). When no strategy reaches the publishing
# threshold, the best package that is sound in every other respect and scores
# at least this much is delivered, flagged, instead of failing the upload.
# Anything lower is a genuinely poor reproduction and still fails.
ACCEPTABLE_SIMILARITY_FLOOR = 0.60


def _copy_tree_contents(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for child in destination.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    for child in source.iterdir():
        target = destination / child.name
        if child.is_dir():
            shutil.copytree(child, target)
        else:
            shutil.copy2(child, target)


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
    target_regions: int = 900,
    design_style: str = "smart_auto",
    finish_mode: str = "original",
    progress_callback: Callable[[int, str, dict], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict:
    input_path = Path(input_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    analysis = analyze_source(input_path)
    requested = {
        "preset": preset,
        "colors": colors,
        "min_region_area": min_region_area,
        "merge_strength": merge_strength,
        "outline_width": outline_width,
        "simplify_tolerance": simplify_tolerance,
        "smoothing_passes": smoothing_passes,
        "auto_crop": auto_crop,
        "exclude_background": exclude_background,
        "generate_pdf": generate_pdf,
        "experience_mode": experience_mode,
        "target_regions": target_regions,
        "design_style": design_style,
        "finish_mode": finish_mode,
    }

    attempts = build_attempts(analysis, requested)
    results: list[AttemptResult] = []
    best: AttemptResult | None = None

    if progress_callback:
        progress_callback(1, "Version 12 analyzing artwork and preparing recovery plan", {
            "sourceAnalysis": analysis,
            "attemptsPlanned": len(attempts),
        })

    with tempfile.TemporaryDirectory(prefix="euqilegna_v12_") as temp_root:
        root = Path(temp_root)
        recovery_started_at = time.monotonic()

        for index, settings in enumerate(attempts, start=1):
            if cancel_check and cancel_check():
                raise RuntimeError("Compilation cancelled by user.")

            attempt_name = str(settings.pop("name"))
            attempt_dir = root / f"{index:02d}_{attempt_name}"
            attempt_dir.mkdir(parents=True, exist_ok=True)

            if progress_callback:
                progress_callback(
                    max(2, min(12, 2 + index * 2)),
                    f"Version 12 attempt {index}/{len(attempts)}: {attempt_name}",
                    {"attempt": attempt_name, "sourceAnalysis": analysis},
                )

            try:
                def forward_progress(inner_percent: int, inner_message: str, inner_details: dict) -> None:
                    if progress_callback:
                        attempt_span = 92.0 / max(1, len(attempts))
                        attempt_start = 4.0 + (index - 1) * attempt_span
                        scaled = attempt_start + (max(0, min(100, inner_percent)) / 100.0) * attempt_span
                        progress_callback(
                            int(min(96, round(scaled))),
                            f"{attempt_name}: {inner_message}",
                            {
                                "attempt": attempt_name,
                                "attemptIndex": index,
                                "attemptCount": len(attempts),
                                "innerPercent": inner_percent,
                                "sourceAnalysis": analysis,
                                **(inner_details or {}),
                            },
                        )

                metadata = compile_core(
                    input_path=input_path,
                    output_dir=attempt_dir,
                    progress_callback=forward_progress,
                    cancel_check=cancel_check,
                    **settings,
                )
                if progress_callback:
                    progress_callback(
                        int(min(97, 4 + index * (92.0 / max(1, len(attempts))))),
                        f"{attempt_name}: validating compiled package",
                        {
                            "attempt": attempt_name,
                            "attemptIndex": index,
                            "attemptCount": len(attempts),
                            "sourceAnalysis": analysis,
                        },
                    )

                report = validate_package(
                    attempt_dir,
                    source_path=input_path,
                    minimum_similarity=PUBLISH_SIMILARITY,
                    target_regions=int(settings.get("target_regions") or target_regions or 900),
                )
                result = AttemptResult(
                    name=attempt_name,
                    success=bool(report["passed"]),
                    output_dir=str(attempt_dir),
                    score=float(report.get("similarity", {}).get("score", 0.0)),
                    validation=report,
                    metadata=metadata,
                )
            except Exception as exc:
                result = AttemptResult(
                    name=attempt_name,
                    success=False,
                    output_dir=str(attempt_dir),
                    error=str(exc),
                )

            results.append(result)

            if result.success and (best is None or result.score > best.score):
                best = result

            # A strong valid result is good enough; don't waste CPU.
            if result.success and result.score >= 0.82:
                break

            # Runtime guard: keep trying every planned strategy until the time
            # budget is spent, so a viable later attempt (e.g. the photo
            # recovery on a line-art source) is not cut off by attempt count.
            if best is None and time.monotonic() - recovery_started_at > RECOVERY_TIME_BUDGET_SECONDS:
                break

        below_threshold = False
        if best is None:
            # No strategy reached the publishing threshold. Fall back to the
            # best package whose only shortfall is visual fidelity.
            usable = [
                item
                for item in results
                if (item.validation or {}).get("fidelityOnly")
                and item.score >= ACCEPTABLE_SIMILARITY_FLOOR
            ]
            if usable:
                best = max(usable, key=lambda item: item.score)
                below_threshold = True

        if best is None:
            diagnostic = {
                "version": "12.0",
                "sourceAnalysis": analysis,
                "attempts": [item.to_dict() for item in results],
            }
            (output_dir / "v12_failed_attempts.json").write_text(
                json.dumps(diagnostic, indent=2),
                encoding="utf-8",
            )
            messages = [
                item.error or "; ".join((item.validation or {}).get("errors", []))
                for item in results
            ]
            raise ValueError(
                "Version 12 could not produce a package that passed validation. "
                + " | ".join(message for message in messages if message)[:1800]
            )

        _copy_tree_contents(Path(best.output_dir), output_dir)

    final_metadata_path = output_dir / "metadata.json"
    final_metadata = {}
    if final_metadata_path.exists():
        try:
            final_metadata = json.loads(final_metadata_path.read_text(encoding="utf-8"))
        except Exception:
            final_metadata = {}

    final_metadata.update({
        "engine": "Euqilegna Paint Compiler 12.0 Validation-First",
        "v12SourceAnalysis": analysis,
        "v12SelectedAttempt": best.name,
        "v12SimilarityScore": best.score,
        "v12ValidationPassed": not below_threshold,
        "v12FidelityBelowThreshold": below_threshold,
        "v12Attempts": [
            {
                "name": item.name,
                "success": item.success,
                "score": item.score,
                "error": item.error,
            }
            for item in results
        ],
    })
    final_metadata_path.write_text(
        json.dumps(final_metadata, indent=2),
        encoding="utf-8",
    )

    if progress_callback:
        progress_callback(
            100,
            (
                f"Complete — closest match found ({best.score:.2f}); very fine "
                "line work is simplified into paintable regions"
                if below_threshold
                else "Complete — Version 12 validation passed"
            ),
            {
                "selectedAttempt": best.name,
                "similarityScore": best.score,
                "validationPassed": not below_threshold,
            },
        )

    return final_metadata

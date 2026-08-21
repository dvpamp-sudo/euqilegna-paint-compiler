from __future__ import annotations

from typing import Any


def summarize_release_readiness(
    validation: dict[str, Any],
    quality_report: dict[str, Any],
) -> dict[str, Any]:
    validation_passed = bool(validation.get("passed"))
    quality_status = str(quality_report.get("status", "unknown"))
    ready = validation_passed and quality_status not in {"failed", "blocked"}
    return {
        "ready": ready,
        "validationPassed": validation_passed,
        "qualityStatus": quality_status,
        "blockingErrors": validation.get("errors", []),
        "warnings": validation.get("warnings", []),
    }

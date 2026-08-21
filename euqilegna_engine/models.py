from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class RegionGeometry:
    region_id: str
    color_id: str
    area: int
    path: str
    label_x: float
    label_y: float
    label_radius: float
    palette_color: str
    original_color: str
    paintability: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_labelable(self) -> bool:
        return self.label_radius > 0 and self.label_x >= 0 and self.label_y >= 0

    @property
    def is_vectorized(self) -> bool:
        return bool(self.path.strip())


@dataclass(slots=True)
class ValidationIssue:
    code: str
    message: str
    region_id: str | None = None
    severity: str = "error"
    repairable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class QualityReport:
    passed: bool
    region_count: int
    simulated_count: int
    numbered_count: int
    clickable_count: int
    hintable_count: int
    palette_count: int
    issues: list[ValidationIssue] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["issues"] = [issue.to_dict() for issue in self.issues]
        return payload


@dataclass(slots=True)
class CompileRequest:
    input_path: Path
    output_dir: Path
    preset: str = "illustration"
    design_style: str = "smart_auto"
    colors: int = 30
    min_region_area: int = 24
    experience_mode: str = "relaxed"
    target_regions: int = 600
    outline_width: float = 0.24
    simplify_tolerance: float = 0.18
    auto_crop: bool = False
    generate_pdf: bool = True
    finish_mode: str = "original"


@dataclass(slots=True)
class CompileResult:
    output_dir: Path
    metadata: dict[str, Any]
    quality_report: QualityReport

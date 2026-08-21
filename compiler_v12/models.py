from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


@dataclass
class AttemptResult:
    name: str
    success: bool
    output_dir: str
    score: float = 0.0
    error: str | None = None
    validation: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

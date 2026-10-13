from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4


class PaintingStatus(StrEnum):
    """Lifecycle states a painting can be in."""

    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ARCHIVED = "archived"


@dataclass(slots=True)
class Painting:
    """A painting in the personal collection.

    ``date_created`` is an ISO 8601 date string, e.g. ``"2026-10-03"``.
    ``source_image``, ``package_dir``, ``region_count`` and ``color_count`` are
    filled in when the compiler core produced the painting.
    ``svg_path`` and ``player_path`` point at the compiler-generated paint map
    and interactive player inside ``package_dir``.
    """

    title: str
    date_created: str
    medium: str
    status: PaintingStatus = PaintingStatus.PLANNED
    id: str = field(default_factory=lambda: uuid4().hex)
    source_image: str | None = None
    package_dir: str | None = None
    svg_path: str | None = None
    player_path: str | None = None
    region_count: int | None = None
    color_count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

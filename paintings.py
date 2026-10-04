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
    """

    title: str
    date_created: str
    medium: str
    status: PaintingStatus = PaintingStatus.PLANNED
    id: str = field(default_factory=lambda: uuid4().hex)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

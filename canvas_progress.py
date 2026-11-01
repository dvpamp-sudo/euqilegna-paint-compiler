"""Server-side record of a Color-by-Number canvas session.

The canvas paints optimistically and mirrors its state to SQLite through this
record, so a session can be resumed exactly where it stopped — after a browser
storage clear, or on another device that carries the same session id.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class CanvasProgress:
    """What one session has painted on one artwork.

    ``completed`` holds the indices of the painted regions, ``region_total`` the
    region count the artwork had while painting, and ``saves`` how many times the
    session wrote its progress to the database.
    """

    session_id: str
    artwork_key: str
    selected: int = 1
    completed: tuple[int, ...] = ()
    region_total: int | None = None
    saves: int = 0
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        """Camel-cased shape for the canvas, which mirrors this record in JavaScript."""
        return {
            "sessionId": self.session_id,
            "artworkKey": self.artwork_key,
            "selected": self.selected,
            "completed": list(self.completed),
            "regionTotal": self.region_total,
            "saves": self.saves,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "paintedCount": len(self.completed),
        }

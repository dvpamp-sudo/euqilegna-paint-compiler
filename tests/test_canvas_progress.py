from pathlib import Path

from canvas_progress import CanvasProgress
from testing_runtime_v10 import RuntimeDatabase


def make_database(tmp_path: Path) -> RuntimeDatabase:
    return RuntimeDatabase(tmp_path / "runtime.sqlite3")


def test_canvas_progress_round_trip(tmp_path):
    database = make_database(tmp_path)
    database.save_canvas_progress(
        CanvasProgress(
            session_id="session-abc123",
            artwork_key="dahlia-mandala",
            selected=3,
            completed=(0, 4, 7),
            region_total=51,
        )
    )

    [stored] = database.load_canvas_session("session-abc123")

    assert stored.artwork_key == "dahlia-mandala"
    assert stored.selected == 3
    assert stored.completed == (0, 4, 7)
    assert stored.region_total == 51
    assert stored.saves == 1
    assert stored.to_dict()["artworkKey"] == "dahlia-mandala"
    assert stored.to_dict()["completed"] == [0, 4, 7]


def test_canvas_progress_updates_the_existing_row(tmp_path):
    database = make_database(tmp_path)
    for completed in [(0,), (0, 1, 9)]:
        database.save_canvas_progress(
            CanvasProgress(
                session_id="session-abc123",
                artwork_key="dahlia-mandala",
                completed=completed,
                region_total=51,
            )
        )

    [stored] = database.load_canvas_session("session-abc123")

    assert stored.completed == (0, 1, 9)
    assert stored.saves == 2
    assert stored.created_at <= stored.updated_at


def test_canvas_progress_is_kept_per_artwork(tmp_path):
    database = make_database(tmp_path)
    database.save_canvas_progress(
        CanvasProgress(session_id="session-abc123", artwork_key="dahlia-mandala")
    )
    database.save_canvas_progress(
        CanvasProgress(session_id="session-abc123", artwork_key="6f1c9job", completed=(5,))
    )

    records = database.load_canvas_session("session-abc123")

    assert {record.artwork_key for record in records} == {"dahlia-mandala", "6f1c9job"}
    assert {record.session_id for record in records} == {"session-abc123"}


def test_canvas_progress_ignores_other_sessions(tmp_path):
    database = make_database(tmp_path)
    database.save_canvas_progress(
        CanvasProgress(session_id="session-abc123", artwork_key="dahlia-mandala", completed=(1,))
    )
    database.save_canvas_progress(
        CanvasProgress(session_id="session-xyz789", artwork_key="dahlia-mandala", completed=(2, 3))
    )

    [stored] = database.load_canvas_session("session-xyz789")

    assert stored.completed == (2, 3)

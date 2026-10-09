from admin_dashboard import build_canvas_overview
from canvas_progress import CanvasProgress


def test_overview_summarizes_progress_and_completion():
    sessions = [
        CanvasProgress(
            session_id="session-aaa", artwork_key="dahlia-mandala",
            completed=tuple(range(51)), region_total=51, saves=4,
        ),
        CanvasProgress(
            session_id="session-aaa", artwork_key="6f1c9job",
            completed=(0, 1, 2, 3, 4), region_total=10, saves=2,
        ),
        CanvasProgress(
            session_id="session-bbb", artwork_key="dahlia-mandala",
            completed=(), region_total=51, saves=1,
        ),
    ]

    overview = build_canvas_overview(sessions)
    summary = overview["summary"]

    assert summary["testers"] == 2
    assert summary["paintings"] == 3
    assert summary["completed"] == 1
    assert summary["active"] == 1
    assert summary["notStarted"] == 1
    assert summary["totalSaves"] == 7
    assert summary["averageCompletion"] == 50.0
    assert [row["status"] for row in overview["sessions"]] == [
        "completed", "in-progress", "not-started"
    ]


def test_overview_without_sessions_is_empty():
    overview = build_canvas_overview([])

    assert overview["sessions"] == []
    assert overview["summary"]["testers"] == 0
    assert overview["summary"]["averageCompletion"] is None
    assert overview["summary"]["lastActivity"] is None


def test_overview_completion_is_unknown_without_a_region_total():
    overview = build_canvas_overview(
        [CanvasProgress(session_id="session-xyz", artwork_key="dahlia-mandala", completed=(1, 2))]
    )

    [row] = overview["sessions"]
    assert row["completion"] is None
    assert row["status"] == "in-progress"
    assert overview["summary"]["averageCompletion"] is None

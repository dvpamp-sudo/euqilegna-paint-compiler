from admin_dashboard import build_admin_overview
from canvas_progress import CanvasProgress


def _feedback(**overrides):
    row = {
        "submitted_at": "2026-10-08T20:00:00+00:00",
        "tester_name": "Ada",
        "device": "iPad",
        "artwork": "dahlia-mandala",
        "completion_status": "Reached 50%",
        "enjoyment": 4,
        "ease_of_use": 5,
        "recommend": 8,
        "pointer_worked": 1,
        "save_worked": 1,
        "hint_worked": 0,
        "comments": "Loved the colors.",
    }
    row.update(overrides)
    return row


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

    overview = build_admin_overview(sessions)
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
    overview = build_admin_overview([])

    assert overview["sessions"] == []
    assert overview["feedback"] == []
    assert overview["summary"]["testers"] == 0
    assert overview["summary"]["averageCompletion"] is None
    assert overview["summary"]["lastActivity"] is None


def test_overview_completion_is_unknown_without_a_region_total():
    overview = build_admin_overview(
        [CanvasProgress(session_id="session-xyz", artwork_key="dahlia-mandala", completed=(1, 2))]
    )

    [row] = overview["sessions"]
    assert row["completion"] is None
    assert row["status"] == "in-progress"
    assert overview["summary"]["averageCompletion"] is None


def test_overview_lists_feedback_testers_and_averages():
    feedback = [
        _feedback(tester_name="Ada", enjoyment=4, recommend=8),
        _feedback(tester_name="Grace", device="iPhone", enjoyment=2, recommend=6),
    ]

    overview = build_admin_overview([], feedback)
    summary = overview["summary"]

    assert summary["feedbackResponses"] == 2
    assert summary["averageEnjoyment"] == 3.0
    assert summary["averageRecommend"] == 7.0
    assert [row["testerName"] for row in overview["feedback"]] == ["Ada", "Grace"]
    assert overview["feedback"][0]["pointerWorked"] == 1
    assert overview["feedback"][0]["hintWorked"] == 0
    assert overview["feedback"][0]["completionStatus"] == "Reached 50%"


def test_overview_feedback_without_a_name_is_anonymous():
    overview = build_admin_overview([], [_feedback(tester_name=None, enjoyment=None)])

    [row] = overview["feedback"]
    assert row["testerName"] == "Anonymous"
    assert overview["summary"]["averageEnjoyment"] is None

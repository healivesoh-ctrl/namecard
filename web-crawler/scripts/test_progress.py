"""scripts/test_progress.py — ProgressTracker 단위 테스트"""
import json
from progress import ProgressTracker


def test_progress_tracker_init(tmp_path):
    tracker = ProgressTracker(str(tmp_path / "progress.json"), total_estimate=100)
    status = tracker.read()
    assert status["total_estimate"] == 100
    assert status["collected"] == 0
    assert status["status"] == "running"


def test_progress_tracker_update(tmp_path):
    tracker = ProgressTracker(str(tmp_path / "progress.json"), total_estimate=100)
    tracker.update(collected=25, current_page=5)
    status = tracker.read()
    assert status["collected"] == 25
    assert status["current_page"] == 5


def test_progress_tracker_error(tmp_path):
    tracker = ProgressTracker(str(tmp_path / "progress.json"), total_estimate=100)
    tracker.add_error("Page 3: HTTP 429")
    status = tracker.read()
    assert status["errors"] == 1
    assert "Page 3: HTTP 429" in status["error_log"]


def test_progress_tracker_complete(tmp_path):
    tracker = ProgressTracker(str(tmp_path / "progress.json"), total_estimate=100)
    tracker.update(collected=95)
    tracker.complete()
    status = tracker.read()
    assert status["status"] == "completed"


def test_progress_tracker_fail(tmp_path):
    tracker = ProgressTracker(str(tmp_path / "progress.json"), total_estimate=100)
    tracker.fail("Connection timeout")
    status = tracker.read()
    assert status["status"] == "failed"
    assert "Connection timeout" in status["error_log"]

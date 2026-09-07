"""scripts/progress.py — 백그라운드 Spider 진행상황 추적"""
import json
import os
import time


class ProgressTracker:
    """Spider 진행상황을 JSON 파일로 기록/조회."""

    def __init__(self, filepath: str, total_estimate: int = 0):
        self.filepath = filepath
        os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
        self._state = {
            "status": "running",
            "total_estimate": total_estimate,
            "collected": 0,
            "current_page": 0,
            "errors": 0,
            "error_log": [],
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._write()

    def update(self, collected: int = None, current_page: int = None):
        if collected is not None:
            self._state["collected"] = collected
        if current_page is not None:
            self._state["current_page"] = current_page
        self._state["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._write()

    def add_error(self, message: str):
        self._state["errors"] += 1
        self._state["error_log"].append(message)
        self._write()

    def complete(self):
        self._state["status"] = "completed"
        self._state["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._write()

    def fail(self, reason: str):
        self._state["status"] = "failed"
        self._state["error_log"].append(reason)
        self._write()

    def read(self) -> dict:
        with open(self.filepath, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write(self):
        with open(self.filepath, "w", encoding="utf-8") as f:
            json.dump(self._state, f, ensure_ascii=False, indent=2)

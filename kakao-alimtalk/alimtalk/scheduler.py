"""백그라운드 워커: 주기적으로 발송 시각이 된 작업을 처리한다."""

from __future__ import annotations

import logging
import threading
import time

from .config import Config
from .providers import Provider
from .service import run_due
from .store import Store

log = logging.getLogger("alimtalk.worker")


class Worker:
    def __init__(self, cfg: Config, store: Store, provider: Provider, interval: float = 15):
        self.cfg, self.store, self.provider, self.interval = cfg, store, provider, interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> int:
        try:
            results = run_due(self.cfg, self.store, self.provider)
        except Exception:  # noqa: BLE001
            log.exception("워커 처리 중 오류")
            return 0
        for job, res in results:
            log.info("job#%s %s → %s %s", job.id, job.message_name, "OK" if res.ok else "FAIL", res.error or "")
        return len(results)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.interval)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="alimtalk-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

"""Thread-based background jobs with progress streaming.

The extraction point for a future worker service: the API talks only to
JobManager; swapping threads for a queue+workers keeps the interface intact.
"""

from __future__ import annotations

import queue
import threading
import traceback
from typing import Any, Callable

from dtb.simulation.engine import SimControl


class Job:
    def __init__(self, job_id: str, kind: str):
        self.id = job_id
        self.kind = kind
        self.control = SimControl()
        self.status = "pending"  # pending|running|paused|completed|failed|cancelled
        self.error: str | None = None
        self.progress: dict[str, Any] = {}
        self._subscribers: list[queue.SimpleQueue] = []
        self._lock = threading.Lock()

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            self.progress = {**self.progress, **event}
            for q in list(self._subscribers):
                q.put(event)

    def subscribe(self) -> queue.SimpleQueue:
        q: queue.SimpleQueue = queue.SimpleQueue()
        with self._lock:
            self._subscribers.append(q)
            q.put({"status": self.status, **self.progress})
        return q

    def unsubscribe(self, q: queue.SimpleQueue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)


class JobManager:
    def __init__(self, max_jobs_history: int = 200):
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._max = max_jobs_history
        self._lock = threading.Lock()

    def start(
        self,
        job_id: str,
        kind: str,
        work: Callable[[Job], None],
    ) -> Job:
        job = Job(job_id, kind)
        with self._lock:
            self._jobs[job_id] = job
            self._order.append(job_id)
            while len(self._order) > self._max:
                dead = self._order.pop(0)
                self._jobs.pop(dead, None)

        def runner() -> None:
            job.status = "running"
            job.publish({"status": "running"})
            try:
                work(job)
                if job.control.cancelled:
                    job.status = "cancelled"
                elif job.status == "running":
                    job.status = "completed"
            except Exception as exc:  # noqa: BLE001 — reported to the client
                job.status = "failed"
                job.error = f"{type(exc).__name__}: {exc}"
                traceback.print_exc()
            job.publish({"status": job.status, "error": job.error, "done": True})

        thread = threading.Thread(target=runner, name=f"dtb-job-{job_id}", daemon=True)
        thread.start()
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def pause(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job and job.status == "running":
            job.control.pause()
            job.status = "paused"
            job.publish({"status": "paused"})
            return True
        return False

    def resume(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job and job.status == "paused":
            job.status = "running"
            job.control.resume()
            job.publish({"status": "running"})
            return True
        return False

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job and job.status in ("pending", "running", "paused"):
            job.control.cancel()
            job.publish({"status": "cancelling"})
            return True
        return False

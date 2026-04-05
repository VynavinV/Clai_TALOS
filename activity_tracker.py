import asyncio
import time
import uuid
from typing import Any


class ActivityTracker:
    def __init__(self):
        self._events: list[dict] = []
        self._max_events = 500
        self._subscribers: list[asyncio.Queue] = []
        self._lock = asyncio.Lock()

    def _trim(self):
        if len(self._events) > self._max_events:
            self._events = self._events[-self._max_events:]

    async def emit(self, event_type: str, agent_id: str, label: str, detail: str = "", extra: dict | None = None):
        evt = {
            "id": uuid.uuid4().hex[:8],
            "type": event_type,
            "agent": agent_id,
            "label": label,
            "detail": detail,
            "extra": extra or {},
            "ts": time.time(),
        }
        async with self._lock:
            self._events.append(evt)
            self._trim()
            dead = []
            for q in self._subscribers:
                try:
                    q.put_nowait(evt)
                except asyncio.QueueFull:
                    dead.append(q)
            for q in dead:
                self._subscribers.remove(q)

    async def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        async with self._lock:
            self._subscribers.append(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue):
        async with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def get_recent(self, limit: int = 100) -> list[dict]:
        return list(self._events[-limit:])

    def get_active_agents(self) -> list[dict]:
        now = time.time()
        agents: dict[str, dict] = {}
        for evt in reversed(self._events):
            aid = evt["agent"]
            if aid in agents:
                continue
            age = now - evt["ts"]
            if age > 300:
                continue
            agents[aid] = {
                "id": aid,
                "last_type": evt["type"],
                "last_label": evt["label"],
                "last_detail": evt["detail"],
                "last_ts": evt["ts"],
                "age_s": round(age, 1),
            }
        return list(agents.values())


tracker = ActivityTracker()


def get_tracker() -> ActivityTracker:
    return tracker

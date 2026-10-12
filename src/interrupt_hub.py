"""Registry of in-flight agent runs, keyed by user.

New user messages used to queue behind the lock and were only processed
AFTER the running task finished. This hub lets a message be delivered to a
running agent between its model calls instead: the running turn drains its
interrupt queue at every round boundary, so an injected message reaches the
model within one round.

The registry is intentionally tiny: one handle per user, holding the queue the
agent already polls plus the list of injected texts so they can be persisted
to chat history in order once the turn ends.
"""

import asyncio
import logging
import time

logger = logging.getLogger("talos.interrupt_hub")


class RunHandle:
    def __init__(self, user_id: int):
        self.user_id = user_id
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=20)
        self.injected: list[str] = []
        self.created_at = time.monotonic()

    def inject(self, text: str, source: str = "user") -> bool:
        """Queue a message for the running agent. Returns False if it was lost."""
        text = str(text or "").strip()
        if not text:
            return False
        try:
            self.queue.put_nowait({"text": text, "source": source})
        except asyncio.QueueFull:
            return False
        if source == "user":
            self.injected.append(text)
        logger.info(f"injected {source} message into running agent for user {self.user_id}: {text[:120]}")
        return True


_handles: dict[int, RunHandle] = {}


def register(user_id: int) -> RunHandle:
    handle = RunHandle(user_id)
    _handles[int(user_id)] = handle
    return handle


def unregister(user_id: int, handle: RunHandle) -> None:
    if _handles.get(int(user_id)) is handle:
        _handles.pop(int(user_id), None)


def active_handle(user_id: int) -> RunHandle | None:
    handle = _handles.get(int(user_id))
    if handle is None:
        return None
    return handle


def inject_message(user_id: int, text: str) -> bool:
    """Deliver a user message to their running agent, if any.

    Returns True when the message was injected (the caller should NOT queue a
    new processing turn for it), False when there is no live run to receive it
    (the caller should process it normally).
    """
    handle = active_handle(user_id)
    if handle is None:
        return False
    return handle.inject(text, source="user")


def queue_is_live(interrupt_queue: asyncio.Queue | None) -> bool:
    """True while some registered run is still around to drain this queue."""
    if interrupt_queue is None:
        return False
    return any(h.queue is interrupt_queue for h in _handles.values())


def take_injected(user_id: int, interrupt_queue: asyncio.Queue | None) -> list[str]:
    """Pop the injected user texts recorded for a run, once.

    Only matches when `interrupt_queue` belongs to the currently registered
    handle, so a stale run never steals another run's messages.
    """
    handle = _handles.get(int(user_id))
    if handle is None or interrupt_queue is None or handle.queue is not interrupt_queue:
        return []
    texts = list(handle.injected)
    handle.injected = []
    return texts

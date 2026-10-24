"""Panel popups: let the agent push popups to open Panel browser tabs.

Two kinds:
- confirm: a yes/no decision. The tool call blocks until a human answers on
  any open Panel page (or a timeout passes), and the answer goes back to the
  agent as the tool result.
- html: a temporary custom HTML card (diagrams, tables, sketches). Fire and
  forget; it disappears when dismissed from the Panel or when its TTL ends.

Delivery is poll-based (GET /api/panel/popups) so it works on the old iPads
the Panel targets — no websockets, no SSE-only path, nothing newer than
what panel.html already uses.
"""

from __future__ import annotations

import asyncio
import time
import uuid

MAX_HTML_CHARS = 20000
MAX_TITLE_CHARS = 200
MAX_TEXT_CHARS = 2000
MAX_PENDING = 8
MAX_CONFIRM_TIMEOUT_S = 300
MAX_HTML_TTL_S = 3600


def _norm_reply(text: str) -> str:
    """Normalize a chat reply for confirm matching (case, punctuation, emoji)."""
    cleaned = str(text or "").strip().lower()
    # Strip surrounding punctuation, emojis, and formatting characters.
    while cleaned and (cleaned[0] in "!.?\"'`*_~-([ " or ord(cleaned[0]) > 0x2000):
        cleaned = cleaned[1:]
    while cleaned and (cleaned[-1] in "!.?\"'`*_~-)] " or ord(cleaned[-1]) > 0x2000):
        cleaned = cleaned[:-1]
    return cleaned


class PanelPopupHub:
    def __init__(self):
        self._popups: dict[str, dict] = {}
        self._events: dict[str, asyncio.Event] = {}

    # -- internal -----------------------------------------------------------

    def _expire_stale(self):
        now = time.time()
        for popup_id in list(self._popups.keys()):
            popup = self._popups[popup_id]
            if popup["status"] != "pending":
                continue
            if popup.get("expires_ts") and popup["expires_ts"] <= now:
                popup["status"] = "expired"
                popup["answer"] = None
                event = self._events.get(popup_id)
                if event:
                    event.set()

    def _prune(self):
        # Drop finished popups older than 10 minutes so the dict never grows.
        now = time.time()
        for popup_id in list(self._popups.keys()):
            popup = self._popups[popup_id]
            if popup["status"] == "pending":
                continue
            if popup.get("finished_ts", 0) + 600 < now:
                del self._popups[popup_id]
                self._events.pop(popup_id, None)
        # Cap live popups: oldest pending ones expire first.
        pending = [p for p in self._popups.values() if p["status"] == "pending"]
        if len(pending) > MAX_PENDING:
            pending.sort(key=lambda p: p["created_ts"])
            for popup in pending[: len(pending) - MAX_PENDING]:
                popup["status"] = "expired"
                popup["answer"] = None
                popup["finished_ts"] = time.time()
                event = self._events.get(popup["id"])
                if event:
                    event.set()

    @staticmethod
    def _clip(value, max_len: int) -> str:
        text = str(value or "").strip()
        return text[:max_len] if max_len > 0 else ""

    # -- create -------------------------------------------------------------

    def create_confirm(
        self,
        title: str,
        text: str,
        timeout_s: int = 90,
        yes_label: str = "Yes",
        no_label: str = "No",
    ) -> dict:
        try:
            timeout_s = int(timeout_s)
        except (TypeError, ValueError):
            timeout_s = 90
        timeout_s = max(5, min(timeout_s, MAX_CONFIRM_TIMEOUT_S))
        self._expire_stale()
        popup = {
            "id": uuid.uuid4().hex[:12],
            "kind": "confirm",
            "title": self._clip(title, MAX_TITLE_CHARS) or "Decision needed",
            "text": self._clip(text, MAX_TEXT_CHARS),
            "yes_label": self._clip(yes_label, 24) or "Yes",
            "no_label": self._clip(no_label, 24) or "No",
            "timeout_s": timeout_s,
            "created_ts": time.time(),
            "expires_ts": time.time() + timeout_s,
            "status": "pending",
            "answer": None,
        }
        self._popups[popup["id"]] = popup
        self._events[popup["id"]] = asyncio.Event()
        self._prune()
        return self._public(popup)

    def show_html(self, title: str, html: str, ttl_s: int = 300, text: str = "") -> dict:
        try:
            ttl_s = int(ttl_s)
        except (TypeError, ValueError):
            ttl_s = 300
        ttl_s = max(5, min(ttl_s, MAX_HTML_TTL_S))
        html = str(html or "").strip()
        if not html:
            return {"error": "html is required"}
        if len(html) > MAX_HTML_CHARS:
            html = html[:MAX_HTML_CHARS]
        self._expire_stale()
        popup = {
            "id": uuid.uuid4().hex[:12],
            "kind": "html",
            "title": self._clip(title, MAX_TITLE_CHARS) or "From your assistant",
            "text": self._clip(text, MAX_TEXT_CHARS),
            "html": html,
            "ttl_s": ttl_s,
            "created_ts": time.time(),
            "expires_ts": time.time() + ttl_s,
            "status": "pending",
            "answer": None,
        }
        self._popups[popup["id"]] = popup
        self._events[popup["id"]] = asyncio.Event()
        self._prune()
        return self._public(popup)

    # -- answer / close -----------------------------------------------------

    def respond(self, popup_id: str, answer: str, via: str = "panel") -> dict:
        self._expire_stale()
        popup = self._popups.get(str(popup_id or ""))
        if not popup or popup["kind"] != "confirm":
            return {"ok": False, "error": "No such pending question."}
        if popup["status"] != "pending":
            return {"ok": False, "error": "Already answered."}
        answer = str(answer or "").strip().lower()
        if answer not in ("yes", "no"):
            return {"ok": False, "error": "Answer must be yes or no."}
        popup["status"] = "answered"
        popup["answer"] = answer
        popup["answered_via"] = str(via or "panel")
        popup["finished_ts"] = time.time()
        event = self._events.get(popup["id"])
        if event:
            event.set()
        return {"ok": True, "answer": answer, "via": popup["answered_via"]}

    def answer_from_text(self, text: str) -> dict | None:
        """Resolve the oldest pending confirm from a chat reply, if it matches.

        Lets the user answer a confirm question from chat (Telegram, web)
        instead of only the Panel. Only exact replies count — "yes", "no",
        "y", "n", or the popup's own option labels — so ordinary messages
        are never swallowed. Returns the respond() result, or None.
        """
        cleaned = _norm_reply(text)
        if not cleaned:
            return None
        self._expire_stale()
        pending = sorted(
            (p for p in self._popups.values() if p["kind"] == "confirm" and p["status"] == "pending"),
            key=lambda p: p["created_ts"],
        )
        if not pending:
            return None
        popup = pending[0]
        yes = _norm_reply(popup.get("yes_label")) or "yes"
        no = _norm_reply(popup.get("no_label")) or "no"
        if cleaned in ("y", "yes") or cleaned == yes:
            return self.respond(popup["id"], "yes", via="chat")
        if cleaned in ("n", "no") or cleaned == no:
            return self.respond(popup["id"], "no", via="chat")
        return None

    def dismiss(self, popup_id: str) -> dict:
        self._expire_stale()
        popup = self._popups.get(str(popup_id or ""))
        if not popup or popup["status"] != "pending":
            return {"ok": False, "error": "No such pending popup."}
        popup["status"] = "dismissed"
        popup["answer"] = None
        popup["finished_ts"] = time.time()
        event = self._events.get(popup["id"])
        if event:
            event.set()
        return {"ok": True}

    def dismiss_pending_confirms(self) -> int:
        """Close every pending confirm so it can't swallow later chat replies.

        Used when the run that was waiting on an answer is stopped: a stale
        pending confirm would otherwise eat a bare "yes"/"no" message sent
        long after anyone was listening.
        """
        self._expire_stale()
        count = 0
        for popup in self._popups.values():
            if popup["kind"] == "confirm" and popup["status"] == "pending":
                popup["status"] = "dismissed"
                popup["answer"] = None
                popup["finished_ts"] = time.time()
                event = self._events.get(popup["id"])
                if event:
                    event.set()
                count += 1
        return count

    # -- wait ---------------------------------------------------------------

    async def wait_for_answer(self, popup_id: str, timeout_s: int) -> dict:
        popup = self._popups.get(str(popup_id or ""))
        if not popup:
            return {"status": "missing"}
        event = self._events.get(popup["id"])
        if event is None:
            return {"status": popup["status"], "answer": popup.get("answer"), "via": popup.get("answered_via")}
        try:
            await asyncio.wait_for(event.wait(), timeout=max(1, int(timeout_s)))
        except asyncio.TimeoutError:
            self._expire_stale()
        popup = self._popups.get(popup["id"], popup)
        return {
            "status": popup["status"],
            "answer": popup.get("answer"),
            "via": popup.get("answered_via"),
        }

    # -- read ---------------------------------------------------------------

    @staticmethod
    def _public(popup: dict) -> dict:
        out = {k: v for k, v in popup.items() if k in (
            "id", "kind", "title", "text", "html",
            "yes_label", "no_label", "timeout_s", "ttl_s",
            "created_ts", "expires_ts", "status",
        )}
        return out

    def pending_list(self) -> list[dict]:
        self._expire_stale()
        self._prune()
        pending = [p for p in self._popups.values() if p["status"] == "pending"]
        pending.sort(key=lambda p: p["created_ts"])
        return [self._public(p) for p in pending]

    def get_html(self, popup_id: str) -> str | None:
        """The HTML body of a pending popup, for same-origin iframe rendering."""
        self._expire_stale()
        popup = self._popups.get(str(popup_id or ""))
        if not popup or popup["kind"] != "html" or popup["status"] != "pending":
            return None
        return popup.get("html", "")


hub = PanelPopupHub()


def get_hub() -> PanelPopupHub:
    return hub


def answer_from_text(text: str) -> dict | None:
    """Resolve the oldest pending confirm from a chat reply, if it matches.

    Convenience wrapper around PanelPopupHub.answer_from_text so chat
    frontends (Telegram bot, web chat) can accept confirm answers without
    touching hub internals. Returns None when the text is not an answer.
    """
    return hub.answer_from_text(text)

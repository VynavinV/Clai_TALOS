import json
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from difflib import SequenceMatcher

import db

STOP_WORDS = {
    "the", "and", "for", "with", "that", "this", "from", "your", "you", "are", "was", "were",
    "have", "has", "had", "will", "would", "could", "should", "can", "not", "but", "about",
    "into", "over", "under", "after", "before", "what", "when", "where", "why", "how", "all",
    "any", "more", "most", "other", "some", "such", "only", "same", "very", "just", "then",
    "there", "their", "them", "they", "which", "while", "been", "being", "also", "still",
    "like", "want", "know", "think", "make", "take", "give", "need", "work", "good", "great",
    "really", "maybe", "probably", "always", "never", "often", "sometimes", "thing",
}


def _conn() -> sqlite3.Connection:
    return db._conn()


def _clamp_importance(value: int | float | None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 5
    return max(1, min(10, parsed))


def _safe_json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [str(v).strip().lower() for v in data if str(v).strip()]


def _extract_keywords(text: str, max_keywords: int = 15) -> list[str]:
    words = re.findall(r"\b[a-z0-9]{3,}\b", str(text or "").lower())
    filtered = [word for word in words if word not in STOP_WORDS]
    if not filtered:
        return []
    counts = Counter(filtered)
    return [word for word, _ in counts.most_common(max_keywords)]


def _calculate_relevance(query_keywords: list[str], memory_keywords: list[str]) -> float:
    if not query_keywords or not memory_keywords:
        return 0.0
    query_set = set(query_keywords)
    memory_set = set(memory_keywords)
    union = len(query_set | memory_set)
    if union == 0:
        return 0.0
    return len(query_set & memory_set) / union


def _parse_timestamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def _recency_score(created_at: str | None, last_accessed: str | None) -> float:
    now = datetime.now(timezone.utc)
    pivot = _parse_timestamp(last_accessed) or _parse_timestamp(created_at)
    if pivot is None:
        return 0.5
    age_days = max(0.0, (now - pivot).total_seconds() / 86400.0)
    return 1.0 / (1.0 + (age_days / 30.0))


def init() -> None:
    with _conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                description TEXT,
                keywords TEXT NOT NULL DEFAULT '[]',
                category TEXT,
                importance INTEGER NOT NULL DEFAULT 5,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                last_accessed TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_memories_keywords ON memories(keywords);
            """
        )

        cols = [row["name"] for row in conn.execute("PRAGMA table_info(memories)").fetchall()]
        if "description" not in cols:
            conn.execute("ALTER TABLE memories ADD COLUMN description TEXT")


def save_memory(
    user_id: int,
    content: str,
    category: str | None = None,
    importance: int = 5,
    description: str | None = None,
) -> dict:
    clean_content = str(content or "").strip()
    clean_category = str(category or "").strip() or None
    clean_description = str(description or "").strip() or None
    clean_importance = _clamp_importance(importance)
    keywords = _extract_keywords(clean_content + " " + (clean_description or ""))

    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO memories (user_id, content, description, keywords, category, importance)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, clean_content, clean_description, json.dumps(keywords), clean_category, clean_importance),
        )
        memory_id = int(cur.lastrowid)

    return {
        "id": memory_id,
        "content": clean_content,
        "description": clean_description,
        "keywords": keywords,
        "category": clean_category,
        "importance": clean_importance,
    }


def get_relevant_memories(user_id: int, message: str, threshold: float = 0.15, limit: int = 5) -> list[dict]:
    query_keywords = _extract_keywords(message)

    with _conn() as conn:
        rows = conn.execute(
            """SELECT id, content, description, keywords, category, importance, created_at, last_accessed
               FROM memories WHERE user_id = ? ORDER BY importance DESC, created_at DESC""",
            (user_id,),
        ).fetchall()

    scored = []
    for row in rows:
        keywords = _safe_json_list(row["keywords"])
        if not keywords:
            keywords = _extract_keywords(str(row["content"] or "") + " " + str(row["description"] or ""))
        relevance = _calculate_relevance(query_keywords, keywords)
        if relevance < threshold:
            continue

        importance = _clamp_importance(row["importance"])
        score = (relevance * 0.65) + ((importance / 10.0) * 0.35)
        scored.append(
            {
                "id": row["id"],
                "content": row["content"],
                "description": row["description"],
                "category": row["category"],
                "importance": importance,
                "relevance": round(relevance, 3),
                "score": round(score, 3),
                "created_at": row["created_at"],
            }
        )

    scored.sort(key=lambda item: (item["score"], item["importance"]), reverse=True)
    return scored[: max(1, int(limit))]


def search_memories(user_id: int, query: str, limit: int = 10) -> list[dict]:
    clean_query = str(query or "").strip()
    if not clean_query:
        return []

    query_keywords = _extract_keywords(clean_query)
    query_lower = clean_query.lower()

    with _conn() as conn:
        rows = conn.execute(
            """SELECT id, content, description, keywords, category, importance, created_at, last_accessed
               FROM memories WHERE user_id = ? ORDER BY created_at DESC""",
            (user_id,),
        ).fetchall()

    scored = []
    for row in rows:
        content = str(row["content"] or "")
        description = str(row["description"] or "")
        combined = (content + "\n" + description).strip()
        keywords = _safe_json_list(row["keywords"])
        if not keywords:
            keywords = _extract_keywords(combined)

        relevance = _calculate_relevance(query_keywords, keywords)
        bonus = 0.35 if query_lower in combined.lower() else 0.0
        importance = _clamp_importance(row["importance"])
        score = relevance + bonus + (importance / 40.0)
        if score <= 0:
            continue

        scored.append(
            {
                "id": row["id"],
                "content": content,
                "description": description or None,
                "category": row["category"],
                "importance": importance,
                "relevance": round(relevance, 3),
                "score": round(score, 3),
                "created_at": row["created_at"],
                "last_accessed": row["last_accessed"],
            }
        )

    scored.sort(key=lambda item: (item["score"], item["importance"], item["created_at"]), reverse=True)
    return scored[: max(1, int(limit))]


def list_memories(user_id: int, category: str | None = None, limit: int = 20) -> list[dict]:
    max_limit = max(1, min(int(limit), 200))
    with _conn() as conn:
        if category:
            rows = conn.execute(
                """SELECT id, content, description, category, importance, created_at, last_accessed
                   FROM memories WHERE user_id = ? AND category = ?
                   ORDER BY importance DESC, created_at DESC LIMIT ?""",
                (user_id, category, max_limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, content, description, category, importance, created_at, last_accessed
                   FROM memories WHERE user_id = ?
                   ORDER BY importance DESC, created_at DESC LIMIT ?""",
                (user_id, max_limit),
            ).fetchall()
    return [dict(row) for row in rows]


def delete_memory(user_id: int, memory_id: int) -> bool:
    with _conn() as conn:
        cur = conn.execute("DELETE FROM memories WHERE id = ? AND user_id = ?", (memory_id, user_id))
        return cur.rowcount > 0


def update_memory(
    user_id: int,
    memory_id: int,
    content: str | None = None,
    category: str | None = None,
    importance: int | None = None,
    description: str | None = None,
) -> bool:
    updates = []
    params = []
    if content is not None:
        updates.append("content = ?")
        params.append(str(content).strip())
    if description is not None:
        updates.append("description = ?")
        params.append(str(description).strip() or None)
    if category is not None:
        updates.append("category = ?")
        params.append(str(category).strip() or None)
    if importance is not None:
        updates.append("importance = ?")
        params.append(_clamp_importance(importance))

    if not updates:
        return False

    params.extend([memory_id, user_id])
    with _conn() as conn:
        cur = conn.execute(f"UPDATE memories SET {', '.join(updates)} WHERE id = ? AND user_id = ?", params)
        return cur.rowcount > 0


def get_categories(user_id: int) -> list[str]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT category FROM memories WHERE user_id = ? AND category IS NOT NULL AND TRIM(category) <> ''",
            (user_id,),
        ).fetchall()
    return [row["category"] for row in rows]


def format_memories_for_context(memories: list[dict]) -> str:
    if not memories:
        return ""
    lines = ["[Relevant Memories]"]
    for memory_item in memories:
        category_prefix = f"[{memory_item['category']}] " if memory_item.get("category") else ""
        description = str(memory_item.get("description") or "").strip()
        details = f" | {description}" if description else ""
        lines.append(
            f"- {category_prefix}{memory_item['content']}{details} "
            f"(importance: {memory_item.get('importance', 5)}, relevance: {memory_item.get('relevance', 0)})"
        )
    return "\n".join(lines)


def consolidate_memories(
    user_id: int,
    min_importance: int = 4,
    relevance_threshold: float = 0.12,
    keep_ratio: float = 0.70,
    min_keep: int = 20,
    max_keep: int = 200,
) -> dict:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT id, content, description, keywords, category, importance, created_at, last_accessed
               FROM memories WHERE user_id = ? ORDER BY created_at DESC""",
            (user_id,),
        ).fetchall()

    items = [dict(row) for row in rows]
    total_before = len(items)
    if total_before == 0:
        return {
            "ok": True,
            "total_before": 0,
            "kept": 0,
            "deleted": 0,
            "duplicates_removed": 0,
            "context_keywords": [],
            "top_kept": [],
            "note": "No memories to consolidate.",
        }

    history = db.get_history(user_id, limit=40)
    context_keywords = _extract_keywords(" ".join(str(msg.get("content") or "") for msg in history), max_keywords=30)

    scored = []
    for row in items:
        content = str(row.get("content") or "").strip()
        normalized = re.sub(r"\s+", " ", (content + " " + str(row.get("description") or "")).lower()).strip()
        keywords = _safe_json_list(row.get("keywords"))
        if not keywords:
            keywords = _extract_keywords(content + " " + str(row.get("description") or ""))
        importance = _clamp_importance(row.get("importance"))
        relevance = _calculate_relevance(context_keywords, keywords) if context_keywords else 0.0
        recency = _recency_score(row.get("created_at"), row.get("last_accessed"))
        if context_keywords:
            score = (relevance * 0.50) + ((importance / 10.0) * 0.40) + (recency * 0.10)
        else:
            score = ((importance / 10.0) * 0.80) + (recency * 0.20)
        scored.append(
            {
                "id": int(row["id"]),
                "content": content,
                "category": row.get("category"),
                "importance": importance,
                "relevance": round(relevance, 4),
                "score": round(score, 4),
                "normalized": normalized,
            }
        )

    scored.sort(key=lambda item: (item["score"], item["importance"]), reverse=True)

    deduped = []
    duplicates_removed = 0
    for item in scored:
        duplicate = False
        for kept in deduped:
            if item["normalized"] and kept["normalized"] and item["normalized"] == kept["normalized"]:
                duplicate = True
                break
            if item.get("category") and kept.get("category") and item["category"] != kept["category"]:
                continue
            if SequenceMatcher(None, item["normalized"], kept["normalized"]).ratio() >= 0.93:
                duplicate = True
                break
        if duplicate:
            duplicates_removed += 1
        else:
            deduped.append(item)

    min_importance = _clamp_importance(min_importance)
    keep_ratio = max(0.25, min(0.95, float(keep_ratio)))
    min_keep = max(1, int(min_keep))
    max_keep = max(min_keep, int(max_keep))

    must_keep_ids = {
        item["id"]
        for item in deduped
        if item["importance"] >= 8 or (item["importance"] >= min_importance and item["relevance"] >= relevance_threshold)
    }
    target_keep = int(round(len(deduped) * keep_ratio))
    target_keep = max(min_keep, target_keep, len(must_keep_ids))
    target_keep = min(max_keep, target_keep, len(deduped))

    keep_ids = list(must_keep_ids)
    for item in deduped:
        if len(keep_ids) >= target_keep:
            break
        if item["id"] not in must_keep_ids:
            keep_ids.append(item["id"])

    if not keep_ids and deduped:
        keep_ids = [deduped[0]["id"]]

    keep_set = set(keep_ids)
    delete_ids = [item["id"] for item in scored if item["id"] not in keep_set]
    deleted = 0
    with _conn() as conn:
        for i in range(0, len(delete_ids), 300):
            chunk = delete_ids[i : i + 300]
            if not chunk:
                continue
            placeholders = ",".join("?" for _ in chunk)
            cur = conn.execute(f"DELETE FROM memories WHERE user_id = ? AND id IN ({placeholders})", [user_id] + chunk)
            deleted += int(cur.rowcount or 0)

    top_kept = [
        {
            "id": item["id"],
            "importance": item["importance"],
            "relevance": item["relevance"],
            "score": item["score"],
            "category": item.get("category"),
            "preview": (item["content"][:120] + "...") if len(item["content"]) > 120 else item["content"],
        }
        for item in deduped
        if item["id"] in keep_set
    ][:10]

    return {
        "ok": True,
        "total_before": total_before,
        "kept": len(keep_set),
        "deleted": deleted,
        "duplicates_removed": duplicates_removed,
        "context_keywords": context_keywords[:20],
        "thresholds": {
            "min_importance": min_importance,
            "relevance_threshold": float(relevance_threshold),
            "keep_ratio": keep_ratio,
            "min_keep": min_keep,
            "max_keep": max_keep,
        },
        "top_kept": top_kept,
    }


def dream(user_id: int) -> dict:
    return consolidate_memories(user_id)

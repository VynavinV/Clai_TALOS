import sqlite3
import os
import re
import json
from datetime import datetime, timezone
from typing import Optional
from collections import Counter

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "talos.db")

STOP_WORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "must", "shall", "can", "need", "dare",
    "ought", "used", "to", "of", "in", "for", "on", "with", "at", "by",
    "from", "as", "into", "through", "during", "before", "after", "above",
    "below", "between", "under", "again", "further", "then", "once", "here",
    "there", "when", "where", "why", "how", "all", "each", "few", "more",
    "most", "other", "some", "such", "no", "nor", "not", "only", "own",
    "same", "so", "than", "too", "very", "just", "and", "but", "if", "or",
    "because", "until", "while", "although", "though", "i", "me", "my",
    "myself", "we", "our", "ours", "ourselves", "you", "your", "yours",
    "yourself", "yourselves", "he", "him", "his", "himself", "she", "her",
    "hers", "herself", "it", "its", "itself", "they", "them", "their",
    "theirs", "themselves", "what", "which", "who", "whom", "this", "that",
    "these", "those", "am", "about", "also", "now", "get", "got", "like",
    "want", "know", "think", "see", "make", "go", "say", "come", "take",
    "use", "tell", "ask", "seem", "feel", "try", "leave", "call", "keep",
    "let", "begin", "show", "hear", "play", "run", "move", "live", "put",
    "set", "add", "change", "help", "close", "open", "find", "give", "work",
    "any", "much", "many", "even", "still", "ever", "never", "always",
    "often", "sometimes", "usually", "already", "yet", "back", "up", "down",
    "out", "off", "over", "away", "well", "ok", "okay", "yes", "no", "not",
    "dont", "don't", "wont", "won't", "cant", "can't", "didnt", "didn't",
    "isnt", "isn't", "arent", "aren't", "wasnt", "wasn't", "werent", "weren't",
    "hasnt", "hasn't", "havent", "haven't", "wouldnt", "wouldn't", "shouldnt",
    "shouldn't", "couldnt", "couldn't", "im", "i'm", "ive", "i've", "id", "i'd",
    "youre", "you're", "youve", "you've", "youd", "you'd", "he's", "she's",
    "it's", "we're", "we've", "we'd", "they're", "they've", "they'd", "that's",
    "whats", "what's", "wheres", "where's", "whos", "who's", "hows", "how's",
    "lets", "let's", "thing", "things", "stuff", "something", "anything",
    "everything", "nothing", "someone", "anyone", "everyone", "nobody",
    "somewhere", "anywhere", "everywhere", "nowhere", "really", "actually",
    "basically", "probably", "definitely", "maybe", "perhaps", "sure",
    "right", "wrong", "good", "bad", "better", "worse", "best", "worst",
    "great", "nice", "cool", "fine", "okay", "pretty", "kind", "sort",
}


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init():
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                keywords TEXT NOT NULL DEFAULT '[]',
                category TEXT,
                importance INTEGER DEFAULT 5,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                last_accessed TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_memories_user
                ON memories(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_memories_keywords
                ON memories(keywords);
        """)


def _extract_keywords(text: str, max_keywords: int = 15) -> list[str]:
    text_lower = text.lower()
    words = re.findall(r'\b[a-z]{3,}\b', text_lower)
    
    filtered = [w for w in words if w not in STOP_WORDS]
    
    word_counts = Counter(filtered)
    keywords = [word for word, _ in word_counts.most_common(max_keywords)]
    
    return keywords


def _calculate_relevance(message_keywords: list[str], memory_keywords: list[str]) -> float:
    if not message_keywords or not memory_keywords:
        return 0.0
    
    message_set = set(message_keywords)
    memory_set = set(memory_keywords)
    
    intersection = len(message_set & memory_set)
    union = len(message_set | memory_set)
    
    if union == 0:
        return 0.0
    
    return intersection / union


def save_memory(user_id: int, content: str, category: Optional[str] = None, importance: int = 5) -> dict:
    keywords = _extract_keywords(content)
    
    with _conn() as conn:
        cursor = conn.execute(
            """INSERT INTO memories (user_id, content, keywords, category, importance)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, content, json.dumps(keywords), category, importance)
        )
        memory_id = cursor.lastrowid
    
    return {
        "id": memory_id,
        "content": content,
        "keywords": keywords,
        "category": category,
        "importance": importance
    }


def get_relevant_memories(user_id: int, message: str, threshold: float = 0.15, limit: int = 5) -> list[dict]:
    message_keywords = _extract_keywords(message)
    
    with _conn() as conn:
        rows = conn.execute(
            """SELECT id, content, keywords, category, importance, created_at, last_accessed
               FROM memories WHERE user_id = ?
               ORDER BY importance DESC, created_at DESC""",
            (user_id,)
        ).fetchall()
    
    scored_memories = []
    for row in rows:
        memory_keywords = json.loads(row["keywords"])
        relevance = _calculate_relevance(message_keywords, memory_keywords)
        
        if relevance >= threshold:
            scored_memories.append({
                "id": row["id"],
                "content": row["content"],
                "category": row["category"],
                "importance": row["importance"],
                "relevance": round(relevance, 2),
                "created_at": row["created_at"]
            })
    
    scored_memories.sort(key=lambda m: (m["relevance"] * m["importance"]), reverse=True)
    
    if scored_memories:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        top_ids = [m["id"] for m in scored_memories[:limit]]
        with _conn() as conn:
            conn.execute(
                f"UPDATE memories SET last_accessed = ? WHERE id IN ({','.join('?'*len(top_ids))})",
                [now] + top_ids
            )
    
    return scored_memories[:limit]


def search_memories(user_id: int, query: str, limit: int = 10) -> list[dict]:
    query_keywords = _extract_keywords(query)
    
    with _conn() as conn:
        rows = conn.execute(
            """SELECT id, content, keywords, category, importance, created_at
               FROM memories WHERE user_id = ?
               ORDER BY created_at DESC""",
            (user_id,)
        ).fetchall()
    
    scored = []
    for row in rows:
        memory_keywords = json.loads(row["keywords"])
        relevance = _calculate_relevance(query_keywords, memory_keywords)
        
        if relevance > 0:
            scored.append({
                "id": row["id"],
                "content": row["content"],
                "category": row["category"],
                "importance": row["importance"],
                "relevance": round(relevance, 2),
                "created_at": row["created_at"]
            })
    
    scored.sort(key=lambda m: m["relevance"], reverse=True)
    return scored[:limit]


def list_memories(user_id: int, category: Optional[str] = None, limit: int = 20) -> list[dict]:
    with _conn() as conn:
        if category:
            rows = conn.execute(
                """SELECT id, content, category, importance, created_at, last_accessed
                   FROM memories WHERE user_id = ? AND category = ?
                   ORDER BY importance DESC, created_at DESC LIMIT ?""",
                (user_id, category, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, content, category, importance, created_at, last_accessed
                   FROM memories WHERE user_id = ?
                   ORDER BY importance DESC, created_at DESC LIMIT ?""",
                (user_id, limit)
            ).fetchall()
    
    return [dict(row) for row in rows]


def delete_memory(user_id: int, memory_id: int) -> bool:
    with _conn() as conn:
        cursor = conn.execute(
            "DELETE FROM memories WHERE id = ? AND user_id = ?",
            (memory_id, user_id)
        )
        return cursor.rowcount > 0


def update_memory(user_id: int, memory_id: int, content: Optional[str] = None, 
                  category: Optional[str] = None, importance: Optional[int] = None) -> bool:
    updates = []
    params = []
    
    if content is not None:
        keywords = _extract_keywords(content)
        updates.append("content = ?, keywords = ?")
        params.extend([content, json.dumps(keywords)])
    
    if category is not None:
        updates.append("category = ?")
        params.append(category)
    
    if importance is not None:
        updates.append("importance = ?")
        params.append(importance)
    
    if not updates:
        return False
    
    params.extend([memory_id, user_id])
    
    with _conn() as conn:
        cursor = conn.execute(
            f"UPDATE memories SET {', '.join(updates)} WHERE id = ? AND user_id = ?",
            params
        )
        return cursor.rowcount > 0


def get_categories(user_id: int) -> list[str]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT category FROM memories WHERE user_id = ? AND category IS NOT NULL",
            (user_id,)
        ).fetchall()
    return [row["category"] for row in rows]


def format_memories_for_context(memories: list[dict]) -> str:
    if not memories:
        return ""
    
    lines = ["[Relevant Memories]"]
    for m in memories:
        category_str = f"[{m['category']}] " if m.get('category') else ""
        lines.append(f"- {category_str}{m['content']} (relevance: {m['relevance']})")
    
    return "\n".join(lines)


init()

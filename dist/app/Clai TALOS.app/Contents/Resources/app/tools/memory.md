# Memory System

Persistent memory storage with automatic relevance retrieval.

## save_memory

Save information to long-term memory for future reference.

**Parameters:**
- `content` (string, required): The information to remember
- `category` (string, optional): Category for organization (e.g., "preferences", "projects", "facts")
- `importance` (number, optional): 1-10 scale, default 5. Higher = more likely to be retrieved

**When to use:**
- User shares preferences ("I prefer dark mode")
- Important facts about user ("I work at Company X")
- Project details ("My project uses React and Node")
- Decisions made ("We decided to use PostgreSQL")
- Recurring context needed

**Example:**
```json
{
  "content": "User prefers TypeScript over JavaScript for all new projects",
  "category": "preferences",
  "importance": 7
}
```

## search_memories

Search through stored memories.

**Parameters:**
- `query` (string, required): Search terms

**Returns:** List of matching memories with relevance scores

## list_memories

List all memories or filter by category.

**Parameters:**
- `category` (string, optional): Filter by category
- `limit` (number, optional): Max results, default 20

## delete_memory

Remove a memory by ID.

**Parameters:**
- `memory_id` (number, required): ID of memory to delete

## update_memory

Update an existing memory.

**Parameters:**
- `memory_id` (number, required): ID of memory to update
- `content` (string, optional): New content
- `category` (string, optional): New category
- `importance` (number, optional): New importance

## Automatic Retrieval

Memories are automatically retrieved when relevant to the current conversation. The system uses keyword matching to find relevant memories. Higher importance memories are prioritized.

## Best Practices

- Save specific, actionable information
- Use meaningful categories
- Set appropriate importance (7-10 for critical, 1-3 for minor)
- Update memories when information changes
- Delete outdated memories

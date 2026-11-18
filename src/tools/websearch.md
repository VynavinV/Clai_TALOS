# Web Search Tool

Search the web for current information using Parallel's free, zero-auth search service.

## Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| query | string | Yes | The search query |
| scope | string | No | Search scope filter (e.g., "news", "academic") |
| location | string | No | Location context for localized results |
| recent_days | number | No | Only include results from the last N days |

## Usage

Use `web_search` when you need current information from the internet:
- Current events and news
- Latest documentation or API changes
- Real-time data (weather, prices, etc.)
- Fact-checking with recent sources

## Examples

**Basic search:**
```
web_search(query="latest Python 3.14 features")
```

**News search (recent):**
```
web_search(query="AI news", scope="news", recent_days=7)
```

**Location-aware search:**
```
web_search(query="weather forecast", location="New York")
```

## Response Format

Returns a JSON object with:
- `query`: The original search query
- `results`: Array of search results with title, link, content (answer-ready excerpts), and publish_date
- `result_count`: Number of results found
- `source`: "Parallel Web Search"
- `note`: Reminds the model that excerpts usually suffice without scraping

## Notes

- No API key needed — the Parallel MCP endpoint (https://mcp.parallel.ai/mcp) is free with hobbyist rate limits
- Returns clean markdown excerpts, so scraping result links is usually unnecessary; combine with scrape_url only when excerpts are insufficient
- If the service is unreachable, search falls back to DuckDuckGo
- Set PARALLEL_MCP_URL in .env to point at a different MCP search server (e.g., with a paid key for higher limits)
- Use specific queries for better results
- Combine with memory to save important findings

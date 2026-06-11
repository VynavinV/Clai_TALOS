# Web Search Tool

Search the web for current information using Google Search via Gemini.

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
- `results`: Array of search results with title, link, and content
- `result_count`: Number of results found
- `source`: "Google Search via Gemini"

## Notes

- Requires GEMINI_API_KEY in .env
- Uses Gemini 2.0 Flash with Google Search grounding
- Results are grounded in real Google Search data
- Use specific queries for better results
- Combine with memory to save important findings
- Combine with scrape_url to get detailed content from result links

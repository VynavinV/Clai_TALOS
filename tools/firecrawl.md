# Firecrawl Tool

Scrape and extract content from web pages using Firecrawl's web scraping service.

## Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| url | string | Yes | The URL to scrape |
| formats | array | No | Output formats: "markdown" (default), "html", "links", "screenshot" |
| only_main_content | boolean | No | Extract only main content, excluding nav/footer (default: true) |
| timeout | number | No | Timeout in milliseconds (default: 30000, max: 300000) |
| max_age | number | No | Cache max age in ms (default: 172800000 = 2 days) |

## Usage

Use `scrape_url` when you need to:
- Extract content from a specific web page
- Get clean markdown from a URL
- Collect links from a page
- Take screenshots of web pages
- Extract structured data from websites

## Examples

**Basic scrape (markdown):**
```
scrape_url(url="https://example.com/article")
```

**Multiple formats:**
```
scrape_url(url="https://docs.python.org/3/library/asyncio.html", formats=["markdown", "links"])
```

**Full HTML content:**
```
scrape_url(url="https://example.com", formats=["html"], only_main_content=false)
```

**With screenshot:**
```
scrape_url(url="https://example.com", formats=["markdown", "screenshot"])
```

## Response Format

Returns a JSON object with:
- `url`: The scraped URL
- `success`: Boolean indicating success
- `markdown`: Extracted markdown content (if requested)
- `html`: Cleaned HTML content (if requested)
- `links`: List of links on the page (if requested)
- `screenshot`: Screenshot URL (if requested, expires after 24h)
- `metadata`: Page metadata (title, description, language, final_url)

## Notes

- Requires FIRECRAWL_API_KEY in .env
- Get an API key at https://www.firecrawl.dev/app
- Uses credits from your Firecrawl account
- Screenshots expire after 24 hours
- Cached results are returned if page was scraped recently (configurable via max_age)
- Combine with web_search to find URLs, then scrape for detailed content

## Common Use Cases

**Documentation extraction:**
```
scrape_url(url="https://docs.example.com/api", formats=["markdown"])
```

**News article analysis:**
```
scrape_url(url="https://news.example.com/article/123")
```

**Link collection:**
```
scrape_url(url="https://example.com/resources", formats=["links"])
```

**Visual verification:**
```
scrape_url(url="https://example.com/dashboard", formats=["screenshot"])
```

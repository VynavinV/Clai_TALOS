# Project Gateway

Create and serve web projects (websites, presentations, apps) from the bot's web server. One tool call does everything.

## create_project

Create a web project and make it instantly live. Handles directory creation, writing index.html, and registration in one call.

**Parameters:**
- `name` (string, required): Project name (alphanumeric, hyphens, underscores). Used in the URL.
- `html` (string, required): The full HTML content for index.html
- `description` (string, optional): Short description

**Returns:**
```json
{
  "status": "live",
  "url": "https://hostname.tailnet.ts.net/projects/my-deck/",
  "share_this_link": "https://hostname.tailnet.ts.net/projects/my-deck/",
  "path": "/path/to/projects/my-deck",
  "instruction": "Send the url to the user"
}
```

**IMPORTANT:** The `url` field is the full live link. Send it to the user exactly as returned.

## list_projects

List all registered projects.

**Returns:**
```json
{
  "projects": [
    {
      "name": "my-deck",
      "path": "/path/to/projects/my-deck",
      "description": "Startup pitch deck",
      "has_index": true,
      "url": "https://hostname.tailnet.ts.net/projects/my-deck/"
    }
  ]
}
```

## Workflow

1. Call `create_project` with a name, the full HTML content, and a description
2. Send the returned `url` to the user — that's it, one step

## Adding Extra Files (images, CSS, JS)

If you need to add files beyond index.html (e.g. images), use `write_file` with the project path returned by `create_project`:
```
write_file(path="<project_path>/images/photo.jpg", content=..., create_dirs=true)
```

## Notes

- Projects are served from the same web server as the dashboard (same port)
- If Tailscale Funnel is active, projects are publicly accessible via HTTPS
- All projects visible at /projects in the dashboard
- Each project is a folder with index.html — no build step

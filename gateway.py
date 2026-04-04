"""
Lightweight project gateway — serves static files from the projects directory.

No Express.js, no Node.js, no extra processes. Just aiohttp routes added
to the existing web server. The bot creates files with write_file, and
they're instantly accessible at /projects/<name>/.

Config lives in projects/gateway.json (auto-created on first run).
"""

import os
import json
import logging
import mimetypes
from aiohttp import web

logger = logging.getLogger("talos.gateway")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

_base_url: str | None = None


def _get_base_url() -> str:
    """Return the public base URL (Tailscale Funnel) or fall back to localhost."""
    global _base_url
    if _base_url is not None:
        return _base_url

    import shutil
    import subprocess

    # Try Tailscale hostname first
    if shutil.which("tailscale"):
        try:
            result = subprocess.run(
                ["tailscale", "status", "--json"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                import json as _json
                data = _json.loads(result.stdout)
                dns_name = data.get("Self", {}).get("DNSName", "").rstrip(".")
                if dns_name:
                    # Check if funnel is active
                    fn = subprocess.run(
                        ["tailscale", "funnel", "status"],
                        capture_output=True, text=True, timeout=5
                    )
                    port = os.getenv("WEB_PORT", "8080")
                    if fn.returncode == 0 and port in fn.stdout:
                        _base_url = f"https://{dns_name}"
                        return _base_url
                    _base_url = f"http://{dns_name}:{port}"
                    return _base_url
        except Exception:
            pass

    port = os.getenv("WEB_PORT", "8080")
    _base_url = f"http://localhost:{port}"
    return _base_url


def get_full_url(path: str) -> str:
    """Build a full URL for a project path."""
    return f"{_get_base_url()}{path}"


# Default: projects/ inside the repo. Override with PROJECTS_DIR env var.
def _get_projects_dir() -> str:
    from_env = os.getenv("PROJECTS_DIR", "").strip()
    if from_env:
        return os.path.expanduser(from_env)
    return os.path.join(SCRIPT_DIR, "projects")


def _get_config_path() -> str:
    return os.path.join(_get_projects_dir(), "gateway.json")


def _load_config() -> dict:
    path = _get_config_path()
    if os.path.isfile(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"projects": {}}


def _save_config(config: dict) -> None:
    path = _get_config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(config, f, indent=2)


def init():
    """Ensure the projects directory and config exist."""
    global _base_url
    _base_url = None  # Re-detect on next use
    projects_dir = _get_projects_dir()
    os.makedirs(projects_dir, exist_ok=True)
    config_path = _get_config_path()
    if not os.path.isfile(config_path):
        _save_config({"projects": {}})
    logger.info(f"Gateway projects dir: {projects_dir}")


def register_project(name: str, project_path: str | None = None, description: str = "") -> dict:
    """Register a project for serving. If no path given, uses projects/<name>/."""
    projects_dir = _get_projects_dir()
    if not project_path:
        project_path = os.path.join(projects_dir, name)

    project_path = os.path.expanduser(project_path)
    os.makedirs(project_path, exist_ok=True)

    config = _load_config()
    config["projects"][name] = {
        "path": project_path,
        "description": description,
    }
    _save_config(config)
    relative = f"/projects/{name}/"
    return {"registered": name, "path": project_path, "url": get_full_url(relative)}


def unregister_project(name: str) -> bool:
    config = _load_config()
    if name in config["projects"]:
        del config["projects"][name]
        _save_config(config)
        return True
    return False


def list_projects() -> list[dict]:
    config = _load_config()
    result = []
    for name, info in config["projects"].items():
        path = info.get("path", "")
        has_index = os.path.isfile(os.path.join(path, "index.html")) if path else False
        relative = f"/projects/{name}/"
        result.append({
            "name": name,
            "path": path,
            "description": info.get("description", ""),
            "has_index": has_index,
            "url": get_full_url(relative),
        })
    return result


def _resolve_project_file(name: str, file_path: str) -> str | None:
    """Resolve a file path within a project, preventing directory traversal."""
    config = _load_config()
    project = config.get("projects", {}).get(name)
    if not project:
        return None

    base = os.path.realpath(project["path"])
    if not file_path or file_path == "/":
        file_path = "index.html"

    # Strip leading slashes
    file_path = file_path.lstrip("/")

    resolved = os.path.realpath(os.path.join(base, file_path))

    # Directory traversal check
    if not resolved.startswith(base + os.sep) and resolved != base:
        return None

    if os.path.isfile(resolved):
        return resolved

    # Try index.html for directory requests
    if os.path.isdir(resolved):
        index = os.path.join(resolved, "index.html")
        if os.path.isfile(index):
            return index

    return None


# ---------------------------------------------------------------------------
# aiohttp route handlers
# ---------------------------------------------------------------------------

async def handle_projects_index(request):
    """GET /projects/ — list all registered projects as a simple HTML page."""
    projects = list_projects()

    if not projects:
        html = """<!DOCTYPE html><html><head><meta charset="utf-8">
        <title>Projects</title>
        <style>body{font-family:system-ui;background:#0a0a0f;color:#e0e0e0;padding:2rem;max-width:800px;margin:0 auto;}
        h1{color:#fff;} p{color:#666;}</style></head>
        <body><h1>Projects</h1><p>No projects yet. Ask the bot to create one.</p></body></html>"""
        return web.Response(text=html, content_type="text/html")

    items = []
    for p in projects:
        status = "ready" if p["has_index"] else "no index.html"
        desc = f' — {p["description"]}' if p["description"] else ""
        items.append(
            f'<li><a href="{p["url"]}">{p["name"]}</a>{desc}'
            f' <span style="color:#666;font-size:0.8em">({status})</span></li>'
        )

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
    <title>Projects</title>
    <style>
    body{{font-family:system-ui;background:#0a0a0f;color:#e0e0e0;padding:2rem;max-width:800px;margin:0 auto;}}
    h1{{color:#fff;}} a{{color:#63b3ed;text-decoration:none;}} a:hover{{text-decoration:underline;}}
    li{{margin:0.5rem 0;}} ul{{list-style:none;padding:0;}}
    </style></head>
    <body><h1>Projects</h1><ul>{"".join(items)}</ul></body></html>"""
    return web.Response(text=html, content_type="text/html")


async def handle_project_file(request):
    """GET /projects/{name}/{path:.*} — serve a file from a project."""
    name = request.match_info["name"]
    file_path = request.match_info.get("path", "")

    resolved = _resolve_project_file(name, file_path)
    if not resolved:
        return web.Response(text="Not found", status=404)

    content_type, _ = mimetypes.guess_type(resolved)
    if not content_type:
        content_type = "application/octet-stream"

    return web.FileResponse(resolved, headers={"Content-Type": content_type})


async def handle_api_projects(request):
    """GET /api/projects — JSON list of projects."""
    return web.json_response(list_projects())


async def handle_api_register(request):
    """POST /api/projects/register — register a new project."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    name = str(body.get("name", "")).strip()
    if not name:
        return web.json_response({"error": "name is required"}, status=400)

    # Sanitize name — alphanumeric, hyphens, underscores only
    import re
    if not re.match(r'^[a-zA-Z0-9_-]+$', name):
        return web.json_response({"error": "name must be alphanumeric with hyphens/underscores only"}, status=400)

    path = body.get("path", "")
    description = str(body.get("description", "")).strip()

    result = register_project(name, path or None, description)
    return web.json_response(result)


async def handle_api_unregister(request):
    """POST /api/projects/unregister — remove a project from the gateway."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    name = str(body.get("name", "")).strip()
    if not name:
        return web.json_response({"error": "name is required"}, status=400)

    ok = unregister_project(name)
    return web.json_response({"ok": ok})


def setup_routes(app: web.Application):
    """Add gateway routes to the existing aiohttp app."""
    # Public routes — projects are meant to be shared
    app.router.add_get("/projects/", handle_projects_index)
    app.router.add_get("/projects/{name}/", handle_project_file)
    app.router.add_get("/projects/{name}/{path:.*}", handle_project_file)

    # API routes (behind auth in telegram_bot.py if needed)
    app.router.add_get("/api/projects", handle_api_projects)
    app.router.add_post("/api/projects/register", handle_api_register)
    app.router.add_post("/api/projects/unregister", handle_api_unregister)

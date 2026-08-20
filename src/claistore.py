"""
Claistore - GitHub-backed skill marketplace module.

Provides functions for reading/writing skills to a GitHub repository
as a decentralized skill store.
"""

import os
import re
import json
import base64
import logging
from typing import Optional, List, Dict, Any, Tuple

import aiohttp

logger = logging.getLogger(__name__)

# Configuration - read from environment
CLAISTORE_GITHUB_REPO = os.getenv("CLAISTORE_GITHUB_REPO", "").strip()
CLAISTORE_GITHUB_TOKEN = os.getenv("CLAISTORE_GITHUB_TOKEN", "").strip()
CLAISTORE_GITHUB_BRANCH = os.getenv("CLAISTORE_GITHUB_BRANCH", "main").strip()
CLAISTORE_SKILLS_DIR = "skills"
CLAISTORE_INDEX_FILE = "index.json"


def is_configured() -> bool:
    """Check if Claistore is configured with required credentials."""
    return bool(CLAISTORE_GITHUB_REPO and CLAISTORE_GITHUB_TOKEN)


async def _claistore_github_request(method: str, path: str, data: Optional[Dict] = None) -> Dict:
    """Make a request to the GitHub API for Claistore operations."""
    if not is_configured():
        raise RuntimeError("Claistore not configured: set CLAISTORE_GITHUB_REPO and CLAISTORE_GITHUB_TOKEN")

    url = f"https://api.github.com/repos/{CLAISTORE_GITHUB_REPO}/contents/{CLAISTORE_SKILLS_DIR}/{path}"
    headers = {
        "Authorization": f"Bearer {CLAISTORE_GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, headers=headers, json=data) as resp:
            if resp.status >= 400:
                text = await resp.text()
                raise RuntimeError(f"GitHub API error {resp.status}: {text}")
            return await resp.json()


async def _claistore_get_file_sha(path: str) -> Optional[str]:
    """Get the SHA of an existing file, or None if it doesn't exist."""
    try:
        result = await _claistore_github_request("GET", path)
        return result.get("sha")
    except RuntimeError as e:
        if "404" in str(e):
            return None
        raise


async def write_file(path: str, content: str, message: str) -> Dict:
    """Create or update a file in the Claistore GitHub repo."""
    sha = await _claistore_get_file_sha(path)
    data = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": CLAISTORE_GITHUB_BRANCH,
    }
    if sha:
        data["sha"] = sha
    return await _claistore_github_request("PUT", path, data)


async def read_file(path: str) -> Optional[str]:
    """Read a file from the Claistore GitHub repo."""
    try:
        result = await _claistore_github_request("GET", path)
        content_b64 = result.get("content", "")
        return base64.b64decode(content_b64).decode("utf-8")
    except RuntimeError as e:
        if "404" in str(e):
            return None
        raise


async def fetch_index() -> List[Dict]:
    """Fetch the skill index from GitHub."""
    content = await read_file(CLAISTORE_INDEX_FILE)
    if content is None:
        return []
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        logger.exception("Failed to parse Claistore index from GitHub")
        return []


async def write_index(items: List[Dict]) -> None:
    """Write the skill index to GitHub."""
    await write_file(
        CLAISTORE_INDEX_FILE,
        json.dumps(items, indent=2),
        "Update Claistore index"
    )


async def publish_skill(skill_id: str, skill_data: Dict[str, Any], content: str) -> Dict:
    """Publish a new skill to Claistore (GitHub)."""
    # Sanitize skill_id for use as filename
    safe_id = re.sub(r'[^a-zA-Z0-9_-]', '_', skill_id)
    skill_file = f"{safe_id}.md"
    meta_file = f"{safe_id}.json"

    # Write skill content file
    await write_file(skill_file, content, f"Add skill: {skill_data.get('name', skill_id)}")

    # Write skill metadata file
    await write_file(meta_file, json.dumps(skill_data, indent=2), f"Add skill metadata: {skill_data.get('name', skill_id)}")

    # Update index
    index = await fetch_index()
    index.append(skill_data)
    await write_index(index)

    return {"ok": True, "skill_id": skill_id}


async def read_skill_file(skill_id: str) -> Tuple[Optional[str], Optional[Dict]]:
    """Read a skill file and its metadata from Claistore (GitHub).
    Returns (content, metadata) or (None, None)."""
    safe_id = re.sub(r'[^a-zA-Z0-9_-]', '_', skill_id)
    skill_file = f"{safe_id}.md"
    meta_file = f"{safe_id}.json"

    try:
        content = await read_file(skill_file)
        meta_content = await read_file(meta_file)
        metadata = json.loads(meta_content) if meta_content else None
        return content, metadata
    except (RuntimeError, json.JSONDecodeError) as e:
        if "404" in str(e):
            return None, None
        logger.exception("Failed to read skill from Claistore")
        raise


async def test_connection() -> Dict[str, Any]:
    """Test the Claistore GitHub connection."""
    if not is_configured():
        return {"ok": False, "error": "Claistore not configured (missing repo or token)"}
    try:
        # Try to fetch the index as a connectivity test
        await fetch_index()
        return {"ok": True, "repo": CLAISTORE_GITHUB_REPO, "branch": CLAISTORE_GITHUB_BRANCH}
    except Exception as e:
        return {"ok": False, "error": str(e)}
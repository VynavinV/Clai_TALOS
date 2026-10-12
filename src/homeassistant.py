"""Home Assistant bridge for the Panel page.

Talks to a Home Assistant server's REST API on behalf of the web UI. The
access token lives in .env (written by the settings page) and never leaves
this process, so the browser (and anyone holding the iPad) only ever sees
entity states.

Every response is shaped for the Panel page: {"ok": ...} plus either data
or a plain-language error a person can act on.
"""

import asyncio
import logging
import os
import re

import aiohttp

logger = logging.getLogger("talos.homeassistant")

REQUEST_TIMEOUT_S = 8
_ALLOWED_NAME = re.compile(r"^[a-z0-9_]+$")

# Entity domains the Panel page shows as tappable controls. Everything else
# (sensors, automations, helpers we can't safely poke) is display-only.
_TOGGLE_DOMAINS = {"light", "switch", "fan", "input_boolean", "humidifier", "siren", "climate"}
_RUN_DOMAINS = {"scene", "script"}

# Attributes worth forwarding; the rest would only bloat the payload the
# iPad polls every few seconds.
_KEEP_ATTRIBUTES = (
    "friendly_name",
    "unit_of_measurement",
    "device_class",
    "temperature",
    "current_temperature",
    "humidity",
    "hvac_mode",
    "brightness",
    "hs_color",
    "rgb_color",
    "color_temp",
    "min_mireds",
    "max_mireds",
    "supported_features",
    "battery_level",
)


def _base_url() -> str:
    return os.getenv("HOMEASSISTANT_URL", "").strip().rstrip("/")


def _token() -> str:
    return os.getenv("HOMEASSISTANT_TOKEN", "").strip()


def is_enabled() -> bool:
    return os.getenv("HOMEASSISTANT_ENABLED", "1") != "0"


def is_configured() -> bool:
    return bool(_base_url() and _token())


def _not_ready() -> dict:
    if not is_enabled():
        return {"ok": False, "configured": False, "enabled": False, "error": "Home Assistant is switched off in Settings."}
    if not _base_url():
        return {"ok": False, "configured": False, "enabled": True, "error": "No Home Assistant address saved yet."}
    if not _token():
        return {"ok": False, "configured": False, "enabled": True, "error": "No Home Assistant access token saved yet."}
    return {"ok": False, "configured": True, "enabled": True, "error": "Home Assistant is not reachable right now."}


async def _request(method: str, path: str, json_body: dict | None = None) -> tuple[int, object, str | None]:
    """Single-shot request. Returns (http_status, parsed_body, friendly_error)."""
    url = _base_url() + path
    headers = {
        "Authorization": f"Bearer {_token()}",
        "Content-Type": "application/json",
    }
    try:
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(method, url, headers=headers, json=json_body) as resp:
                body = None
                if (resp.headers.get("Content-Type") or "").startswith("application/json"):
                    body = await resp.json()
                return resp.status, body, None
    except asyncio.TimeoutError:
        return 0, None, f"Home Assistant at {_base_url()} took too long to answer."
    except aiohttp.ClientConnectorError:
        return 0, None, (
            f"Can't reach Home Assistant at {_base_url()}. "
            "Check that Home Assistant is running and on the same network as this computer."
        )
    except aiohttp.ClientError as exc:
        return 0, None, f"Problem talking to Home Assistant: {exc}"


def _friendly_http_error(status: int) -> str | None:
    if status in (401, 403):
        return (
            "Home Assistant rejected the access token. "
            "Create a new long-lived access token (Home Assistant → Profile → Security) and paste it into Settings."
        )
    if status == 404:
        return "That address answered, but it is not a Home Assistant API. Double-check the address in Settings."
    if status >= 500:
        return "Home Assistant had an internal error. Check its logs."
    return None


async def get_status() -> dict:
    """Connection check for the Panel and Settings pages."""
    if not is_configured() or not is_enabled():
        result = _not_ready()
        return result

    status, body, error = await _request("GET", "/api/config")
    if error:
        return {"ok": False, "configured": True, "enabled": True, "error": error}
    http_error = _friendly_http_error(status)
    if http_error:
        return {"ok": False, "configured": True, "enabled": True, "error": http_error}
    if status == 200 and isinstance(body, dict):
        return {
            "ok": True,
            "configured": True,
            "enabled": True,
            "version": str(body.get("version", "")),
            "location": str(body.get("location_name", "")),
        }
    return {"ok": False, "configured": True, "enabled": True, "error": f"Unexpected answer from Home Assistant (HTTP {status})."}


def _trim_state(entity: dict) -> dict:
    attributes = entity.get("attributes") or {}
    kept = {k: attributes[k] for k in _KEEP_ATTRIBUTES if k in attributes}
    entity_id = str(entity.get("entity_id", ""))
    domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
    return {
        "entity_id": entity_id,
        "domain": domain,
        "state": str(entity.get("state", "")),
        "name": str(attributes.get("friendly_name") or entity_id),
        "attributes": kept,
        "controllable": domain in _TOGGLE_DOMAINS or domain in _RUN_DOMAINS,
        "action": "toggle" if domain in _TOGGLE_DOMAINS else ("run" if domain in _RUN_DOMAINS else None),
    }


async def get_states() -> dict:
    """All entity states, trimmed to what the Panel page displays."""
    if not is_configured() or not is_enabled():
        return {**_not_ready(), "entities": []}

    status, body, error = await _request("GET", "/api/states")
    if error:
        return {"ok": False, "error": error, "entities": []}
    http_error = _friendly_http_error(status)
    if http_error:
        return {"ok": False, "error": http_error, "entities": []}
    if status == 200 and isinstance(body, list):
        entities = [_trim_state(e) for e in body if isinstance(e, dict) and e.get("entity_id")]
        entities.sort(key=lambda e: e["entity_id"])
        return {"ok": True, "entities": entities, "count": len(entities)}
    return {"ok": False, "error": f"Unexpected answer from Home Assistant (HTTP {status}).", "entities": []}


async def call_service(domain: str, service: str, entity_id: str, data: dict | None = None) -> dict:
    """Run one service call, e.g. light/toggle on light.living_room."""
    if not is_configured() or not is_enabled():
        return _not_ready()

    domain = str(domain or "").strip()
    service = str(service or "").strip()
    entity_id = str(entity_id or "").strip()
    if not _ALLOWED_NAME.match(domain) or not _ALLOWED_NAME.match(service):
        return {"ok": False, "error": "That is not a valid Home Assistant command."}
    if not entity_id or not _ALLOWED_NAME.match(entity_id.replace(".", "_")):
        return {"ok": False, "error": "That is not a valid Home Assistant device."}

    payload = {"entity_id": entity_id}
    if isinstance(data, dict):
        payload.update(data)

    path = f"/api/services/{domain}/{service}"
    status, _body, error = await _request("POST", path, json_body=payload)
    if error:
        return {"ok": False, "error": error}
    http_error = _friendly_http_error(status)
    if http_error:
        return {"ok": False, "error": http_error}
    if status == 200:
        logger.info("homeassistant service %s/%s on %s", domain, service, entity_id)
        return {"ok": True}
    return {"ok": False, "error": f"Home Assistant refused the command (HTTP {status})."}


# ---------------------------------------------------------------------------
# Agent-facing helpers (used by the homeassistant_execute tool)
#
# These return deliberately compact payloads: one line per entity, no JSON
# nesting, so a model can scan hundreds of entities without burning context.
# ---------------------------------------------------------------------------

_MAX_LIST_LINES = 250


def _entity_line(entity: dict) -> str:
    entity_id = str(entity.get("entity_id", ""))
    state = str(entity.get("state", ""))
    name = str((entity.get("attributes") or {}).get("friendly_name") or "")
    unit = str((entity.get("attributes") or {}).get("unit_of_measurement") or "")
    line = f"{entity_id} = {state}"
    if unit:
        line += f" {unit}"
    if name and name.lower() != entity_id:
        line += f" ({name})"
    return line


async def list_entities_compact(domain: str = "", search: str = "") -> dict:
    """Compact entity listing for the agent, optionally filtered.

    Output is one line per entity: "entity_id = state unit (friendly name)".
    Filters keep the response small even on big installs.
    """
    if not is_configured() or not is_enabled():
        return _not_ready()

    status, body, error = await _request("GET", "/api/states")
    if error:
        return {"ok": False, "error": error}
    http_error = _friendly_http_error(status)
    if http_error:
        return {"ok": False, "error": http_error}
    if status != 200 or not isinstance(body, list):
        return {"ok": False, "error": f"Unexpected answer from Home Assistant (HTTP {status})."}

    domain = str(domain or "").strip().lower().rstrip(".")
    search = str(search or "").strip().lower()
    lines: list[str] = []
    total = 0
    for entity in body:
        if not isinstance(entity, dict) or not entity.get("entity_id"):
            continue
        entity_id = str(entity["entity_id"])
        if domain and not entity_id.startswith(domain + "."):
            continue
        if search and search not in entity_id and search not in str((entity.get("attributes") or {}).get("friendly_name") or "").lower():
            continue
        total += 1
        if len(lines) < _MAX_LIST_LINES:
            lines.append(_entity_line(entity))

    result = {"ok": True, "total_matches": total, "returned": len(lines)}
    if total > len(lines):
        result["note"] = f"Only the first {len(lines)} matches are shown. Narrow with the 'domain' or 'search' filter."
    result["entities"] = "\n".join(lines)
    return result


async def get_entity_state(entity_id: str) -> dict:
    """Full state of one entity, for when a compact line is not enough."""
    if not is_configured() or not is_enabled():
        return _not_ready()

    entity_id = str(entity_id or "").strip()
    if not entity_id or not _ALLOWED_NAME.match(entity_id.replace(".", "_")):
        return {"ok": False, "error": "That is not a valid Home Assistant entity_id."}

    status, body, error = await _request("GET", f"/api/states/{entity_id}")
    if error:
        return {"ok": False, "error": error}
    http_error = _friendly_http_error(status)
    if http_error:
        return {"ok": False, "error": http_error}
    if status == 200 and isinstance(body, dict):
        attributes = body.get("attributes") or {}
        return {
            "ok": True,
            "entity_id": str(body.get("entity_id", entity_id)),
            "state": str(body.get("state", "")),
            "attributes": attributes,
            "last_changed": str(body.get("last_changed", "")),
            "last_updated": str(body.get("last_updated", "")),
        }
    if status == 404:
        return {"ok": False, "error": f"Entity '{entity_id}' does not exist. Use list_entities with a search filter to find the right entity_id."}
    return {"ok": False, "error": f"Unexpected answer from Home Assistant (HTTP {status})."}

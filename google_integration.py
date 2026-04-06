import base64
import hashlib
import asyncio
import json
import os
import secrets
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone

import app_paths


GOOGLE_TOKENS_FILE = app_paths.oauth_tokens_path()

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
TOKENINFO_ENDPOINT = "https://oauth2.googleapis.com/tokeninfo"

DEFAULT_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]


def _now_ts() -> int:
    return int(time.time())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_json_load(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _safe_json_save(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _parse_json_or_raw(raw: str) -> dict:
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except json.JSONDecodeError:
        return {"raw": raw}


def _http_request_sync(
    method: str,
    url: str,
    timeout_s: int,
    headers: dict | None = None,
    data: bytes | None = None,
) -> dict:
    req = urllib.request.Request(
        url,
        data=data,
        method=str(method or "GET").upper(),
        headers=headers or {},
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = int(getattr(resp, "status", resp.getcode()))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        status = int(exc.code)
    except Exception as exc:
        return {
            "status": 0,
            "body": {"error": str(exc)},
            "raw": str(exc),
        }

    return {
        "status": status,
        "body": _parse_json_or_raw(raw),
        "raw": raw,
    }


def _parse_scopes() -> list[str]:
    raw = os.getenv("GOOGLE_OAUTH_SCOPES", "").strip()
    if not raw:
        return list(DEFAULT_SCOPES)

    parts = [part.strip() for part in raw.replace(",", " ").split() if part.strip()]
    if not parts:
        return list(DEFAULT_SCOPES)
    return parts


def _build_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(72)
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def _token_is_usable(token_data: dict, min_seconds: int = 90) -> bool:
    access = str(token_data.get("access_token", "")).strip()
    expiry = int(token_data.get("expiry_epoch", 0) or 0)
    if not access or not expiry:
        return False
    return (_now_ts() + min_seconds) < expiry


def _load_tokens() -> dict:
    return _safe_json_load(GOOGLE_TOKENS_FILE)


def _save_tokens(token_data: dict) -> None:
    token_data = dict(token_data)
    token_data["updated_at"] = _now_iso()
    _safe_json_save(GOOGLE_TOKENS_FILE, token_data)


def _oauth_config() -> dict:
    return {
        "client_id": os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip(),
        "client_secret": os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip(),
        "redirect_uri": os.getenv("GOOGLE_OAUTH_REDIRECT_URI", "").strip(),
        "apps_script_url": os.getenv("GOOGLE_APPS_SCRIPT_URL", "").strip(),
        "api_key": os.getenv("GOOGLE_API_KEY", "").strip(),
    }


def get_status() -> dict:
    cfg = _oauth_config()
    token_data = _load_tokens()

    connected = bool(token_data.get("refresh_token") or _token_is_usable(token_data, min_seconds=0))
    expiry_epoch = int(token_data.get("expiry_epoch", 0) or 0)
    expires_in = max(0, expiry_epoch - _now_ts()) if expiry_epoch else 0

    return {
        "ok": True,
        "has_api_key": bool(cfg["api_key"]),
        "has_oauth_client": bool(cfg["client_id"]),
        "has_oauth_secret": bool(cfg["client_secret"]),
        "has_apps_script_url": bool(cfg["apps_script_url"]),
        "connected": connected,
        "expires_in_seconds": expires_in,
        "scopes": token_data.get("scope", ""),
        "connected_at": token_data.get("created_at", ""),
        "updated_at": token_data.get("updated_at", ""),
    }


def start_oauth_flow(redirect_uri: str) -> dict:
    cfg = _oauth_config()
    client_id = cfg["client_id"]
    if not client_id:
        return {
            "error": "Missing GOOGLE_OAUTH_CLIENT_ID in settings.",
            "hint": "Set Google OAuth Client ID in Dashboard -> Settings -> Google Ecosystem.",
        }

    if not redirect_uri:
        return {
            "error": "Missing redirect URI.",
            "hint": "Provide GOOGLE_OAUTH_REDIRECT_URI or use the dashboard connect endpoint.",
        }

    scopes = _parse_scopes()
    code_verifier, code_challenge = _build_pkce_pair()
    state = secrets.token_urlsafe(24)

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    auth_url = AUTH_ENDPOINT + "?" + urllib.parse.urlencode(params)

    return {
        "ok": True,
        "state": state,
        "code_verifier": code_verifier,
        "auth_url": auth_url,
        "scopes": scopes,
    }


async def exchange_code_for_tokens(code: str, code_verifier: str, redirect_uri: str) -> dict:
    cfg = _oauth_config()
    client_id = cfg["client_id"]
    client_secret = cfg["client_secret"]

    if not client_id:
        return {"error": "GOOGLE_OAUTH_CLIENT_ID is not configured."}
    if not code or not code_verifier or not redirect_uri:
        return {"error": "code, code_verifier, and redirect_uri are required."}

    payload = {
        "code": code,
        "client_id": client_id,
        "code_verifier": code_verifier,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    if client_secret:
        payload["client_secret"] = client_secret

    encoded = urllib.parse.urlencode(payload).encode("utf-8")
    response = await asyncio.to_thread(
        _http_request_sync,
        "POST",
        TOKEN_ENDPOINT,
        30,
        {"Content-Type": "application/x-www-form-urlencoded"},
        encoded,
    )
    data = response["body"]
    if response["status"] != 200:
        return {
            "error": "Google token exchange failed.",
            "status": response["status"],
            "detail": data,
        }

    existing = _load_tokens()
    refresh_token = data.get("refresh_token") or existing.get("refresh_token", "")
    expires_in = int(data.get("expires_in", 3600) or 3600)

    token_data = {
        "access_token": data.get("access_token", ""),
        "refresh_token": refresh_token,
        "token_type": data.get("token_type", "Bearer"),
        "scope": data.get("scope", " ".join(_parse_scopes())),
        "id_token": data.get("id_token", ""),
        "created_at": existing.get("created_at") or _now_iso(),
        "expiry_epoch": _now_ts() + max(30, expires_in),
    }

    _save_tokens(token_data)
    return {"ok": True, "connected": True, "status": get_status()}


async def ensure_access_token(force_refresh: bool = False) -> dict:
    cfg = _oauth_config()
    client_id = cfg["client_id"]
    client_secret = cfg["client_secret"]

    token_data = _load_tokens()
    if not token_data:
        return {
            "error": "Google account is not linked.",
            "hint": "Open Dashboard -> Settings -> Google Ecosystem -> Connect Google.",
        }

    if not force_refresh and _token_is_usable(token_data):
        return {
            "ok": True,
            "access_token": token_data.get("access_token", ""),
            "token_type": token_data.get("token_type", "Bearer"),
            "expiry_epoch": int(token_data.get("expiry_epoch", 0) or 0),
        }

    refresh_token = str(token_data.get("refresh_token", "")).strip()
    if not refresh_token:
        return {
            "error": "Missing refresh token.",
            "hint": "Reconnect Google from Dashboard -> Settings.",
        }

    if not client_id:
        return {"error": "GOOGLE_OAUTH_CLIENT_ID is not configured."}

    payload = {
        "client_id": client_id,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    if client_secret:
        payload["client_secret"] = client_secret

    encoded = urllib.parse.urlencode(payload).encode("utf-8")
    response = await asyncio.to_thread(
        _http_request_sync,
        "POST",
        TOKEN_ENDPOINT,
        30,
        {"Content-Type": "application/x-www-form-urlencoded"},
        encoded,
    )
    data = response["body"]
    if response["status"] != 200:
        return {
            "error": "Google token refresh failed.",
            "status": response["status"],
            "detail": data,
        }

    expires_in = int(data.get("expires_in", 3600) or 3600)
    token_data["access_token"] = data.get("access_token", "")
    token_data["token_type"] = data.get("token_type", token_data.get("token_type", "Bearer"))
    token_data["scope"] = data.get("scope", token_data.get("scope", ""))
    token_data["expiry_epoch"] = _now_ts() + max(30, expires_in)

    _save_tokens(token_data)
    return {
        "ok": True,
        "access_token": token_data.get("access_token", ""),
        "token_type": token_data.get("token_type", "Bearer"),
        "expiry_epoch": int(token_data.get("expiry_epoch", 0) or 0),
    }


def disconnect() -> dict:
    if os.path.isfile(GOOGLE_TOKENS_FILE):
        os.remove(GOOGLE_TOKENS_FILE)
    return {"ok": True, "connected": False}


async def test_connection() -> dict:
    auth = await ensure_access_token(force_refresh=False)
    if "error" in auth:
        return auth

    token = auth.get("access_token", "")
    if not token:
        return {"error": "Google access token unavailable."}

    tokeninfo_url = TOKENINFO_ENDPOINT + "?" + urllib.parse.urlencode({"access_token": token})
    response = await asyncio.to_thread(
        _http_request_sync,
        "GET",
        tokeninfo_url,
        20,
        {},
        None,
    )
    data = response["body"]
    if response["status"] != 200:
        return {
            "error": "Google token validation failed.",
            "status": response["status"],
            "detail": data,
        }

    return {
        "ok": True,
        "google_token": data,
        "status": get_status(),
    }


async def execute_apps_script(action: str, payload: dict | None = None) -> dict:
    cfg = _oauth_config()
    url = cfg["apps_script_url"]

    if not url:
        return await _direct_api_fallback(action, payload)

    action = str(action or "").strip()
    if not action:
        return {"error": "action is required."}

    auth = await ensure_access_token(force_refresh=False)
    if "error" in auth:
        return auth

    token = auth.get("access_token", "")
    body = {
        "action": action,
        "payload": payload if isinstance(payload, dict) else {},
        "source": "clai_talos",
        "timestamp": _now_iso(),
    }

    async def _call(access_token: str) -> dict:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        payload_bytes = json.dumps(body).encode("utf-8")
        response = await asyncio.to_thread(
            _http_request_sync,
            "POST",
            url,
            45,
            headers,
            payload_bytes,
        )
        return {
            "status": response["status"],
            "body": response["body"],
        }

    first = await _call(token)
    if first["status"] in (401, 403):
        refreshed = await ensure_access_token(force_refresh=True)
        if "error" in refreshed:
            return refreshed
        second = await _call(refreshed.get("access_token", ""))
        first = second

    if first["status"] >= 400:
        return {
            "error": "Apps Script execution failed.",
            "status": first["status"],
            "detail": first["body"],
        }

    return {
        "ok": True,
        "action": action,
        "result": first["body"],
        "status": first["status"],
    }


async def _google_api_call(method: str, path: str, body: dict | None = None, params: str = "") -> dict:
    auth = await ensure_access_token(force_refresh=False)
    if "error" in auth:
        return auth

    token = auth.get("access_token", "")
    url = f"https://www.googleapis.com{path}"
    if params:
        url += f"?{params}"

    headers = {
        "Authorization": f"Bearer {token}",
    }
    data_bytes = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data_bytes = json.dumps(body).encode("utf-8")

    async def _call(access_token: str) -> dict:
        h = dict(headers)
        h["Authorization"] = f"Bearer {access_token}"
        response = await asyncio.to_thread(
            _http_request_sync, method, url, 30, h, data_bytes,
        )
        return response

    first = await _call(token)
    if first["status"] in (401, 403):
        refreshed = await ensure_access_token(force_refresh=True)
        if "error" in refreshed:
            return refreshed
        first = await _call(refreshed.get("access_token", ""))

    if first["status"] >= 400:
        return {
            "error": f"Google API call failed ({first['status']}).",
            "detail": first["body"],
        }

    return {"ok": True, "data": first["body"]}


async def _direct_api_fallback(action: str, payload: dict | None = None) -> dict:
    payload = payload or {}

    if action == "calendar.list_events":
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        time_min = payload.get("time_min", now_iso)
        time_max = payload.get("time_max", "")
        max_results = payload.get("max_results", 10)
        calendar_id = payload.get("calendar_id", "primary")
        params = f"maxResults={max_results}&singleEvents=true&orderBy=startTime"
        if time_min:
            params += f"&timeMin={urllib.parse.quote(time_min)}"
        if time_max:
            params += f"&timeMax={urllib.parse.quote(time_max)}"
        safe_id = urllib.parse.quote(calendar_id, safe="")
        return await _google_api_call("GET", f"/calendar/v3/calendars/{safe_id}/events", params=params)

    if action == "calendar.create_event":
        calendar_id = payload.get("calendar_id", "primary")
        event_body = payload.get("event", {})
        safe_id = urllib.parse.quote(calendar_id, safe="")
        return await _google_api_call("POST", f"/calendar/v3/calendars/{safe_id}/events", body=event_body)

    if action == "calendar.list_calendars":
        return await _google_api_call("GET", "/calendar/v3/users/me/calendarList")

    if action == "drive.list_files":
        max_results = payload.get("max_results", 20)
        query = payload.get("query", "")
        params = f"pageSize={max_results}&fields=files(id,name,mimeType,modifiedTime,size,ownedByMe)"
        if query:
            params += f"&q={urllib.parse.quote(query)}"
        return await _google_api_call("GET", "/drive/v3/files", params=params)

    if action == "drive.get_file":
        file_id = payload.get("file_id", "")
        if not file_id:
            return {"error": "file_id is required."}
        return await _google_api_call("GET", f"/drive/v3/files/{file_id}", params="fields=id,name,mimeType,size")

    if action == "drive.export_file":
        file_id = payload.get("file_id", "")
        mime_type = payload.get("mime_type", "text/plain")
        if not file_id:
            return {"error": "file_id is required."}
        return await _google_api_call("GET", f"/drive/v3/files/{file_id}/export", params=f"mimeType={urllib.parse.quote(mime_type)}")

    if action == "sheets.get_values":
        spreadsheet_id = payload.get("spreadsheet_id", "")
        range_str = payload.get("range", "Sheet1")
        if not spreadsheet_id:
            return {"error": "spreadsheet_id is required."}
        return await _google_api_call("GET", f"/sheets/v4/spreadsheets/{spreadsheet_id}/values/{urllib.parse.quote(range_str, safe=':!')}")

    if action == "sheets.append_row":
        spreadsheet_id = payload.get("spreadsheet_id", "")
        range_str = payload.get("range", "Sheet1")
        values = payload.get("values", [])
        if not spreadsheet_id:
            return {"error": "spreadsheet_id is required."}
        body = {"values": [values] if isinstance(values, list) and not isinstance(values[0] if values else None, list) else values}
        return await _google_api_call("POST", f"/sheets/v4/spreadsheets/{spreadsheet_id}/values/{urllib.parse.quote(range_str, safe=':!')}:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS", body=body)

    return {
        "error": f"Unknown action: {action}. Set GOOGLE_APPS_SCRIPT_URL for custom actions, or use: calendar.list_events, calendar.create_event, calendar.list_calendars, drive.list_files, drive.get_file, drive.export_file, sheets.get_values, sheets.append_row",
        "available_actions": [
            "calendar.list_events", "calendar.create_event", "calendar.list_calendars",
            "drive.list_files", "drive.get_file", "drive.export_file",
            "sheets.get_values", "sheets.append_row",
        ],
    }

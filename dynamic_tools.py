import json
import logging
import os
import re
import shlex
from datetime import datetime, timezone

logger = logging.getLogger("talos.dynamic_tools")

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REGISTRY_DIR = os.path.join(_SCRIPT_DIR, "projects")
_REGISTRY_PATH = os.path.join(_REGISTRY_DIR, "dynamic_tools.json")
_TOOLS_DOCS_DIR = os.path.join(_SCRIPT_DIR, "tools")

_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_PARAM_NAME_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_PLACEHOLDER_PATTERN = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_ALLOWED_PARAM_TYPES = {"string", "number", "integer", "boolean"}

_RESERVED_TOOL_NAMES = {
    "execute_command",
    "execute_workflow",
    "schedule_cron",
    "list_cron",
    "remove_cron",
    "save_memory",
    "search_memories",
    "list_memories",
    "delete_memory",
    "update_memory",
    "set_model_prefs",
    "web_search",
    "scrape_url",
    "google_execute",
    "email_execute",
    "browser_start_chrome_debug",
    "browser_connect",
    "browser_run",
    "browser_state",
    "browser_disconnect",
    "read_file",
    "write_file",
    "edit_file",
    "spreadsheet_execute",
    "docx_execute",
    "create_project",
    "list_projects",
    "spawn_subagent",
    "send_telegram_message",
    "send_voice_message",
    "send_telegram_photo",
    "send_telegram_screenshot",
    "create_tool",
    "list_dynamic_tools",
    "delete_tool",
}


def _normalize_tool_name(name: str) -> str:
    return str(name or "").strip().lower()


def _ensure_storage_dirs() -> None:
    os.makedirs(_REGISTRY_DIR, exist_ok=True)
    os.makedirs(_TOOLS_DOCS_DIR, exist_ok=True)


def _load_registry() -> dict:
    if not os.path.isfile(_REGISTRY_PATH):
        return {}

    try:
        with open(_REGISTRY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        logger.exception("Failed to read dynamic tool registry")
        return {}

    if not isinstance(data, dict):
        logger.warning("dynamic_tools.json has invalid format; expected object")
        return {}

    cleaned = {}
    for name, spec in data.items():
        if isinstance(spec, dict):
            cleaned[str(name)] = spec
    return cleaned


def _save_registry(registry: dict) -> None:
    _ensure_storage_dirs()
    tmp_path = f"{_REGISTRY_PATH}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2, ensure_ascii=True, sort_keys=True)
        f.write("\n")
    os.replace(tmp_path, _REGISTRY_PATH)


def _normalize_properties(raw_parameters: dict | None) -> dict:
    if raw_parameters is None:
        return {}
    if not isinstance(raw_parameters, dict):
        raise ValueError("parameters must be an object mapping argument names to descriptors")

    properties = {}
    for key, descriptor in raw_parameters.items():
        name = str(key).strip()
        if not _PARAM_NAME_PATTERN.match(name):
            raise ValueError(f"Invalid parameter name: {name}")

        if isinstance(descriptor, str):
            param_type = "string"
            description = descriptor.strip()
        elif isinstance(descriptor, dict):
            param_type = str(descriptor.get("type", "string")).strip().lower() or "string"
            if param_type not in _ALLOWED_PARAM_TYPES:
                allowed = ", ".join(sorted(_ALLOWED_PARAM_TYPES))
                raise ValueError(
                    f"Invalid type for parameter '{name}': {param_type}. Allowed: {allowed}"
                )
            description = str(descriptor.get("description", "")).strip()
        else:
            raise ValueError(
                f"Parameter '{name}' descriptor must be a string or object"
            )

        prop = {"type": param_type}
        if description:
            prop["description"] = description
        properties[name] = prop

    return properties


def _normalize_required(raw_required: list | None, properties: dict) -> list[str]:
    if raw_required is None:
        return []
    if not isinstance(raw_required, list):
        raise ValueError("required must be an array of parameter names")

    required: list[str] = []
    for item in raw_required:
        name = str(item).strip()
        if not name:
            continue
        if name not in properties:
            raise ValueError(f"required parameter '{name}' is not defined in parameters")
        if name not in required:
            required.append(name)
    return required


def _extract_placeholders(command_template: str) -> list[str]:
    return sorted(set(_PLACEHOLDER_PATTERN.findall(command_template or "")))


def _tool_doc_path(name: str) -> str:
    return os.path.join(_TOOLS_DOCS_DIR, f"{name}.md")


def _build_tool_doc(spec: dict) -> str:
    name = str(spec.get("name", "")).strip()
    description = str(spec.get("description", "")).strip()
    command_template = str(spec.get("command_template", "")).strip()
    timeout = int(spec.get("timeout", 30))

    parameters = spec.get("parameters", {})
    properties = parameters.get("properties", {}) if isinstance(parameters, dict) else {}
    required = set(parameters.get("required", [])) if isinstance(parameters, dict) else set()
    guide = str(spec.get("guide", "")).strip()

    lines = [
        f"# {name}",
        "",
        description or "Dynamically generated tool.",
        "",
        "Generated by `create_tool`.",
        "",
        "## Command Template",
        "```bash",
        command_template,
        "```",
        "",
        f"Default timeout: {timeout}s",
    ]

    if properties:
        lines.extend(["", "## Parameters"])
        for param_name in sorted(properties):
            prop = properties[param_name]
            param_type = str(prop.get("type", "string"))
            label = "required" if param_name in required else "optional"
            desc = str(prop.get("description", "")).strip()
            if desc:
                lines.append(f"- `{param_name}` ({param_type}, {label}): {desc}")
            else:
                lines.append(f"- `{param_name}` ({param_type}, {label})")

    if guide:
        lines.extend(["", "## Notes", guide])

    return "\n".join(lines).rstrip() + "\n"


def _write_tool_doc(spec: dict) -> str:
    _ensure_storage_dirs()
    path = _tool_doc_path(spec["name"])
    with open(path, "w", encoding="utf-8") as f:
        f.write(_build_tool_doc(spec))
    return path


def _to_public_tool(spec: dict) -> dict:
    params = spec.get("parameters")
    if not isinstance(params, dict):
        params = {"type": "object", "properties": {}, "required": []}

    properties = params.get("properties")
    if not isinstance(properties, dict):
        properties = {}

    required = params.get("required")
    if not isinstance(required, list):
        required = []

    return {
        "type": "function",
        "function": {
            "name": str(spec.get("name", "")).strip(),
            "description": str(spec.get("description", "")).strip() or "Dynamic custom tool",
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def get_tool_definitions() -> list[dict]:
    registry = _load_registry()
    tools = []
    for name in sorted(registry):
        spec = registry[name]
        if not isinstance(spec, dict):
            continue
        if _normalize_tool_name(name) in _RESERVED_TOOL_NAMES:
            continue
        tools.append(_to_public_tool(spec))
    return tools


def list_tools() -> list[dict]:
    registry = _load_registry()
    out = []
    for name in sorted(registry):
        spec = registry[name]
        if not isinstance(spec, dict):
            continue
        params = spec.get("parameters", {})
        props = params.get("properties", {}) if isinstance(params, dict) else {}
        required = params.get("required", []) if isinstance(params, dict) else []
        out.append(
            {
                "name": str(spec.get("name", name)),
                "description": str(spec.get("description", "")),
                "command_template": str(spec.get("command_template", "")),
                "timeout": int(spec.get("timeout", 30)),
                "parameters": sorted(props.keys()) if isinstance(props, dict) else [],
                "required": required if isinstance(required, list) else [],
                "created_at": spec.get("created_at"),
                "updated_at": spec.get("updated_at"),
            }
        )
    return out


def get_tool_spec(name: str) -> dict | None:
    normalized = _normalize_tool_name(name)
    if not normalized:
        return None
    return _load_registry().get(normalized)


def create_tool(
    name: str,
    description: str,
    command_template: str,
    parameters: dict | None = None,
    required: list | None = None,
    timeout: int | float | str | None = 30,
    guide: str | None = "",
    overwrite: bool = False,
) -> dict:
    normalized_name = _normalize_tool_name(name)
    description_text = str(description or "").strip()
    command_template_text = str(command_template or "").strip()

    if not normalized_name:
        return {"error": "name is required"}
    if not _NAME_PATTERN.match(normalized_name):
        return {
            "error": (
                "Invalid tool name. Use lowercase letters, numbers, and underscores, "
                "start with a letter, min 3 chars"
            )
        }
    if normalized_name in _RESERVED_TOOL_NAMES:
        return {"error": f"'{normalized_name}' is reserved and cannot be overridden"}
    if not description_text:
        return {"error": "description is required"}
    if not command_template_text:
        return {"error": "command_template is required"}

    try:
        properties = _normalize_properties(parameters)
        required_names = _normalize_required(required, properties)
    except ValueError as e:
        return {"error": str(e)}

    placeholders = _extract_placeholders(command_template_text)
    for placeholder in placeholders:
        if placeholder not in properties:
            properties[placeholder] = {
                "type": "string",
                "description": f"Value for command placeholder {{{placeholder}}}",
            }
        if placeholder not in required_names:
            required_names.append(placeholder)

    try:
        timeout_value = int(float(timeout if timeout is not None else 30))
    except (TypeError, ValueError):
        timeout_value = 30
    timeout_value = max(1, min(timeout_value, 600))

    registry = _load_registry()
    exists = normalized_name in registry
    if exists and not overwrite:
        return {
            "error": f"Tool '{normalized_name}' already exists",
            "hint": "Use overwrite=true to replace it",
        }

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    prev_created = None
    if exists and isinstance(registry.get(normalized_name), dict):
        prev_created = registry[normalized_name].get("created_at")

    spec = {
        "name": normalized_name,
        "description": description_text,
        "command_template": command_template_text,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required_names,
        },
        "timeout": timeout_value,
        "guide": str(guide or "").strip(),
        "created_at": prev_created or now,
        "updated_at": now,
    }

    registry[normalized_name] = spec

    try:
        _save_registry(registry)
        doc_path = _write_tool_doc(spec)
    except Exception as e:
        logger.exception("Failed to persist dynamic tool")
        return {"error": f"Failed to persist tool: {e}"}

    return {
        "ok": True,
        "created": not exists,
        "tool": {
            "name": normalized_name,
            "description": description_text,
            "timeout": timeout_value,
            "parameters": sorted(properties.keys()),
            "required": required_names,
            "command_template": command_template_text,
        },
        "registry_path": _REGISTRY_PATH,
        "doc_path": doc_path,
    }


def delete_tool(name: str) -> dict:
    normalized_name = _normalize_tool_name(name)
    if not normalized_name:
        return {"error": "name is required"}
    if normalized_name in _RESERVED_TOOL_NAMES:
        return {"error": f"'{normalized_name}' is reserved and cannot be deleted"}

    registry = _load_registry()
    spec = registry.get(normalized_name)
    if not isinstance(spec, dict):
        return {"error": f"Dynamic tool not found: {normalized_name}"}

    del registry[normalized_name]

    try:
        _save_registry(registry)
    except Exception as e:
        logger.exception("Failed to update dynamic tool registry after delete")
        return {"error": f"Failed to delete tool: {e}"}

    doc_path = _tool_doc_path(normalized_name)
    doc_removed = False
    if os.path.isfile(doc_path):
        try:
            os.remove(doc_path)
            doc_removed = True
        except Exception:
            logger.exception("Failed to remove dynamic tool guide: %s", doc_path)

    return {
        "ok": True,
        "deleted": normalized_name,
        "doc_removed": doc_removed,
        "registry_path": _REGISTRY_PATH,
    }


def build_command(name: str, args: dict | None) -> dict:
    normalized_name = _normalize_tool_name(name)
    spec = get_tool_spec(normalized_name)
    if not isinstance(spec, dict):
        return {"error": f"Unknown dynamic tool: {name}"}

    if args is None:
        args = {}
    if not isinstance(args, dict):
        return {"error": "Tool arguments must be an object"}

    schema = spec.get("parameters", {})
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    required = schema.get("required", []) if isinstance(schema, dict) else []

    if not isinstance(properties, dict):
        properties = {}
    if not isinstance(required, list):
        required = []

    unknown_args = sorted(k for k in args.keys() if k not in properties)
    if unknown_args:
        return {
            "error": "Unknown arguments for dynamic tool",
            "unknown_arguments": unknown_args,
            "known_arguments": sorted(properties.keys()),
        }

    missing = sorted(
        key for key in required
        if key not in args or args.get(key) is None or str(args.get(key)).strip() == ""
    )
    if missing:
        return {
            "error": "Missing required arguments for dynamic tool",
            "missing_arguments": missing,
        }

    unresolved: list[str] = []

    def _replace(match: re.Match) -> str:
        key = match.group(1)
        value = args.get(key)
        if value is None or (isinstance(value, str) and value.strip() == ""):
            unresolved.append(key)
            return ""

        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, (dict, list)):
            rendered = json.dumps(value, ensure_ascii=False)
        else:
            rendered = str(value)

        return shlex.quote(rendered)

    template = str(spec.get("command_template", "")).strip()
    command = _PLACEHOLDER_PATTERN.sub(_replace, template).strip()

    unresolved_required = sorted({k for k in unresolved if k in required})
    if unresolved_required:
        return {
            "error": "Missing required placeholder values after rendering",
            "missing_arguments": unresolved_required,
        }
    if not command:
        return {"error": f"Dynamic tool '{normalized_name}' rendered an empty command"}

    try:
        timeout = int(float(spec.get("timeout", 30)))
    except (TypeError, ValueError):
        timeout = 30
    timeout = max(1, min(timeout, 600))

    return {
        "ok": True,
        "tool": normalized_name,
        "command": command,
        "timeout": timeout,
    }

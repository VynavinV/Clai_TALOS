import asyncio
import json
import os
import mimetypes
import shutil
from datetime import datetime, timezone
from email.message import EmailMessage
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import app_paths


_DEFAULT_TIMEOUT_S = 90
_DEFAULT_EXPORT_DIR = app_paths.data_path("email_exports")
_BLOCKED_ATTACHMENT_HINT = "Attachment path points to a protected file and is blocked for safety."


def _is_protected_attachment(path: str) -> bool:
    normalized = os.path.realpath(path).replace("\\", "/").lower()
    blocked_tokens = [
        "/.env",
        "/.credentials",
        "/.google_oauth.json",
        "/.himalaya/",
        "/.ssh/",
        "/id_rsa",
        "/id_ed25519",
        "/.gnupg/",
        "/.pem",
        "/.key",
    ]
    return any(token in normalized for token in blocked_tokens)


def _resolve_existing_file(path: str) -> str:
    raw = str(path or "").strip()
    if not raw:
        return ""

    expanded = os.path.expanduser(raw)
    candidates: list[str] = []

    if os.path.isabs(expanded):
        candidates.append(expanded)
    else:
        candidates.extend([
            os.path.join(os.getcwd(), expanded),
            os.path.join(app_paths.data_root(), expanded),
            os.path.join(app_paths.source_root(), expanded),
        ])

    for candidate in candidates:
        resolved = os.path.realpath(candidate)
        if os.path.isfile(resolved):
            return resolved

    return ""


def _normalize_attachment_paths(raw_attachments: Any) -> tuple[list[str] | None, list[str]]:
    if raw_attachments is None:
        return None, []

    if not isinstance(raw_attachments, list):
        return None, ["attachments must be an array of file paths"]

    resolved_paths: list[str] = []
    errors: list[str] = []

    for item in raw_attachments:
        if isinstance(item, dict):
            path_str = str(item.get("path", "")).strip()
        else:
            path_str = str(item).strip()

        if not path_str:
            continue

        resolved = _resolve_existing_file(path_str)
        if not resolved:
            errors.append(f"Attachment not found: {path_str}")
            continue

        if _is_protected_attachment(resolved):
            errors.append(f"{_BLOCKED_ATTACHMENT_HINT} ({path_str})")
            continue

        if resolved not in resolved_paths:
            resolved_paths.append(resolved)

    return (resolved_paths or None), errors


def _resolve_output_file(raw_output_path: str, default_filename: str) -> str:
    expanded = os.path.expanduser(str(raw_output_path or "").strip())

    if os.path.isabs(expanded):
        target = expanded
    else:
        target = os.path.join(_DEFAULT_EXPORT_DIR, expanded)

    # Allow passing a directory path by trailing separator or existing directory.
    if expanded.endswith(("/", "\\")) or os.path.isdir(target):
        target = os.path.join(target, default_filename)

    target = os.path.realpath(target)
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return target


def _resolve_output_dir(raw_output_dir: str, default_subdir: str) -> str:
    expanded = os.path.expanduser(str(raw_output_dir or "").strip())

    if os.path.isabs(expanded):
        target = expanded
    elif expanded:
        target = os.path.join(_DEFAULT_EXPORT_DIR, expanded)
    else:
        target = os.path.join(_DEFAULT_EXPORT_DIR, default_subdir)

    target = os.path.realpath(target)
    os.makedirs(target, exist_ok=True)
    return target


def _save_text_output(raw_output_path: str, content: str, default_filename: str) -> dict:
    if not raw_output_path.strip():
        return {"ok": True}

    try:
        target = _resolve_output_file(raw_output_path, default_filename)
        with open(target, "w", encoding="utf-8", errors="replace") as f:
            f.write(content)
        return {
            "ok": True,
            "saved_to": target,
            "saved_bytes": len(content.encode("utf-8")),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Failed to write email output file: {exc}",
        }


def _extract_downloaded_paths(output_text: str) -> list[str]:
    out: list[str] = []
    marker = "downloading "
    for line in str(output_text or "").splitlines():
        stripped = line.strip()
        if not stripped.lower().startswith(marker):
            continue

        path_text = stripped[len(marker):].strip()
        if path_text.endswith("..."):
            path_text = path_text[:-3].rstrip()
        if path_text.endswith("\u2026"):
            path_text = path_text[:-1].rstrip()
        if path_text.startswith('"') and path_text.endswith('"') and len(path_text) >= 2:
            path_text = path_text[1:-1]
        if path_text.startswith("'") and path_text.endswith("'") and len(path_text) >= 2:
            path_text = path_text[1:-1]

        if path_text:
            out.append(path_text)

    return out


def _himalaya_bin() -> str:
    configured = os.getenv("HIMALAYA_BIN", "").strip()
    return configured or "himalaya"


def _himalaya_run_root() -> str:
    root = app_paths.data_root()
    os.makedirs(root, exist_ok=True)
    return root


def _normalize_himalaya_config_path(config_path: str, run_root: str) -> str:
    raw = str(config_path or "").strip().strip('"')
    if not raw:
        return ""

    expanded = os.path.expanduser(raw)
    if os.path.isabs(expanded) and os.name == "nt":
        try:
            rel = os.path.relpath(os.path.realpath(expanded), os.path.realpath(run_root))
            if rel and not rel.startswith("..") and not os.path.isabs(rel):
                return rel.replace("\\", "/")
        except Exception:
            pass

    return expanded


def _himalaya_env(run_root: str) -> dict[str, str]:
    env = os.environ.copy()
    config_path = os.getenv("HIMALAYA_CONFIG", "").strip()
    if config_path:
        env["HIMALAYA_CONFIG"] = _normalize_himalaya_config_path(config_path, run_root)
    return env


def _default_account() -> str:
    return os.getenv("HIMALAYA_DEFAULT_ACCOUNT", "").strip()


def _add_account_arg(args: list[str], account: str | None) -> list[str]:
    effective = str(account or "").strip() or _default_account()
    if effective:
        args.extend(["--account", effective])
    return args


def _normalize_ids(raw_ids: Any) -> list[str]:
    if raw_ids is None:
        return []
    if isinstance(raw_ids, (int, float)):
        return [str(int(raw_ids))]
    if isinstance(raw_ids, str):
        parts = [part.strip() for part in raw_ids.replace(";", ",").split(",")]
        return [part for part in parts if part]
    if isinstance(raw_ids, list):
        out: list[str] = []
        for item in raw_ids:
            if isinstance(item, (int, float)):
                out.append(str(int(item)))
            elif isinstance(item, str) and item.strip():
                out.append(item.strip())
        return out
    return []


def _normalize_recipients(raw_value: Any) -> list[str]:
    if raw_value is None:
        return []
    if isinstance(raw_value, str):
        parts = [part.strip() for part in raw_value.replace(";", ",").split(",")]
        return [part for part in parts if part]
    if isinstance(raw_value, list):
        out: list[str] = []
        for item in raw_value:
            item_s = str(item).strip()
            if item_s:
                out.append(item_s)
        return out
    return []


def _coerce_headers(raw_headers: Any) -> dict[str, str]:
    if not isinstance(raw_headers, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw_headers.items():
        key_s = str(key).strip()
        val_s = str(value).strip()
        if key_s and val_s:
            out[key_s] = val_s
    return out


def _friendly_error(stderr: str, stdout: str) -> str:
    detail = (stderr or stdout or "").strip()
    lowered = detail.lower()

    if "not found" in lowered and "himalaya" in lowered:
        return "Himalaya CLI is not installed or not on PATH. Install it or set HIMALAYA_BIN in settings."
    if "no such file" in lowered and "config" in lowered:
        return "Himalaya config file was not found. Set HIMALAYA_CONFIG or create a default Himalaya config."
    if "account" in lowered and ("not found" in lowered or "unknown" in lowered):
        return "Himalaya account is not configured. Set HIMALAYA_DEFAULT_ACCOUNT or pass account explicitly."
    if "authentication" in lowered or "auth" in lowered:
        return "Email backend authentication failed. Check your Himalaya account credentials."
    if detail:
        return detail
    return "Himalaya command failed."


async def _run_himalaya(
    args: list[str],
    *,
    output_format: str | None = None,
    stdin_text: str | None = None,
    timeout_s: int = _DEFAULT_TIMEOUT_S,
) -> dict:
    binary = _himalaya_bin()
    run_root = _himalaya_run_root()

    if not os.path.isabs(binary) and shutil.which(binary) is None:
        return {
            "ok": False,
            "error": "Himalaya CLI not found. Install it or set HIMALAYA_BIN in settings.",
            "command": [binary] + list(args),
        }

    cmd = [binary]
    config_path = os.getenv("HIMALAYA_CONFIG", "").strip()
    if config_path:
        cmd.extend(["--config", _normalize_himalaya_config_path(config_path, run_root)])
    if output_format:
        cmd.extend(["--output", output_format])
    cmd.extend(args)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if stdin_text is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_himalaya_env(run_root),
            cwd=run_root,
        )
    except FileNotFoundError:
        return {
            "ok": False,
            "error": "Himalaya CLI not found. Install it or set HIMALAYA_BIN in settings.",
            "command": cmd,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Failed to start Himalaya command: {exc}",
            "command": cmd,
        }

    payload = stdin_text.encode("utf-8") if stdin_text is not None else None
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(payload), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {
            "ok": False,
            "error": f"Himalaya command timed out after {timeout_s}s.",
            "command": cmd,
        }

    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")

    result = {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "command": cmd,
    }

    if output_format == "json" and stdout.strip():
        try:
            result["data"] = json.loads(stdout)
        except json.JSONDecodeError:
            result["parse_error"] = "Command returned non-JSON output"

    if proc.returncode != 0:
        result["error"] = _friendly_error(stderr, stdout)

    return result


def _build_raw_message(
    to_values: list[str],
    subject: str,
    body: str,
    cc_values: list[str],
    bcc_values: list[str],
    headers: dict[str, str],
    attachments: list[str] | None = None,
) -> str:
    has_attachments = attachments and len(attachments) > 0

    if has_attachments:
        msg = MIMEMultipart()
        msg["To"] = ", ".join(to_values)
        if subject.strip():
            msg["Subject"] = subject.strip()
        if cc_values:
            msg["Cc"] = ", ".join(cc_values)
        if bcc_values:
            msg["Bcc"] = ", ".join(bcc_values)

        for key, value in headers.items():
            if key.lower() in {"to", "subject", "cc", "bcc"}:
                continue
            msg[key] = value

        msg.attach(MIMEText(body, "plain"))

        for filepath in attachments:
            filepath = filepath.strip()
            if not filepath or not os.path.isfile(filepath):
                continue
            mime_type, _ = mimetypes.guess_type(filepath)
            if mime_type is None:
                mime_type = "application/octet-stream"
            main_type, sub_type = mime_type.split("/", 1)
            with open(filepath, "rb") as f:
                part = MIMEBase(main_type, sub_type)
                part.set_payload(f.read())
                encoders.encode_base64(part)
            filename = os.path.basename(filepath)
            part.add_header("Content-Disposition", "attachment", filename=filename)
            msg.attach(part)
    else:
        msg = EmailMessage()
        msg["To"] = ", ".join(to_values)
        if subject.strip():
            msg["Subject"] = subject.strip()
        if cc_values:
            msg["Cc"] = ", ".join(cc_values)
        if bcc_values:
            msg["Bcc"] = ", ".join(bcc_values)

        for key, value in headers.items():
            if key.lower() in {"to", "subject", "cc", "bcc"}:
                continue
            msg[key] = value

        msg.set_content(body)

    return msg.as_string()


async def execute(action: str, **kwargs) -> dict:
    action_name = str(action or "").strip().lower()
    if not action_name:
        return {"ok": False, "error": "action is required"}

    account = str(kwargs.get("account", "")).strip() or None
    folder = str(kwargs.get("folder", "")).strip() or None

    if action_name == "list_accounts":
        res = await _run_himalaya(["account", "list"], output_format="json")
        if res.get("ok") and "data" not in res:
            fallback = await _run_himalaya(["account", "list"], output_format="plain")
            res["text"] = fallback.get("stdout", "")
        return res

    if action_name == "list_folders":
        args = ["folder", "list"]
        _add_account_arg(args, account)
        res = await _run_himalaya(args, output_format="json")
        if res.get("ok") and "data" not in res:
            fallback = await _run_himalaya(args, output_format="plain")
            res["text"] = fallback.get("stdout", "")
        return res

    if action_name == "list_messages":
        args = ["envelope", "list"]
        if folder:
            args.extend(["--folder", folder])

        page = kwargs.get("page")
        if isinstance(page, (int, float)) and int(page) > 0:
            args.extend(["--page", str(int(page))])

        _add_account_arg(args, account)
        res = await _run_himalaya(args, output_format="json")
        if res.get("ok") and "data" not in res:
            fallback = await _run_himalaya(args, output_format="plain")
            res["text"] = fallback.get("stdout", "")
        return res

    if action_name == "read_message":
        message_id = kwargs.get("message_id")
        ids = _normalize_ids(message_id)
        if not ids:
            return {"ok": False, "error": "message_id is required"}

        output_path = str(kwargs.get("output_path", "")).strip()

        args = ["message", "read"]
        if folder:
            args.extend(["--folder", folder])

        preview = kwargs.get("preview")
        if preview is None or bool(preview):
            args.append("--preview")

        args.extend(ids)
        _add_account_arg(args, account)
        result = await _run_himalaya(args, output_format="plain")
        if result.get("ok") and output_path:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            save_result = _save_text_output(
                output_path,
                result.get("stdout", ""),
                f"message_{ids[0]}_{timestamp}.txt",
            )
            if not save_result.get("ok"):
                return save_result
            result.update({
                "saved_to": save_result.get("saved_to"),
                "saved_bytes": save_result.get("saved_bytes"),
            })
        return result

    if action_name == "thread_message":
        message_id = kwargs.get("message_id")
        ids = _normalize_ids(message_id)
        if not ids:
            return {"ok": False, "error": "message_id is required"}

        output_path = str(kwargs.get("output_path", "")).strip()

        args = ["message", "thread"]
        if folder:
            args.extend(["--folder", folder])

        preview = kwargs.get("preview")
        if preview is None or bool(preview):
            args.append("--preview")

        args.append(ids[0])
        _add_account_arg(args, account)
        result = await _run_himalaya(args, output_format="plain")
        if result.get("ok") and output_path:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            save_result = _save_text_output(
                output_path,
                result.get("stdout", ""),
                f"thread_{ids[0]}_{timestamp}.txt",
            )
            if not save_result.get("ok"):
                return save_result
            result.update({
                "saved_to": save_result.get("saved_to"),
                "saved_bytes": save_result.get("saved_bytes"),
            })
        return result

    if action_name == "send_message":
        to_values = _normalize_recipients(kwargs.get("to"))
        cc_values = _normalize_recipients(kwargs.get("cc"))
        bcc_values = _normalize_recipients(kwargs.get("bcc"))
        subject = str(kwargs.get("subject", ""))
        body = str(kwargs.get("body", ""))
        headers = _coerce_headers(kwargs.get("headers"))
        attachments, attachment_errors = _normalize_attachment_paths(kwargs.get("attachments"))

        if attachment_errors:
            return {
                "ok": False,
                "error": "One or more attachments are invalid.",
                "details": attachment_errors,
            }

        if not to_values:
            return {"ok": False, "error": "to is required"}

        raw = _build_raw_message(
            to_values=to_values,
            subject=subject,
            body=body,
            cc_values=cc_values,
            bcc_values=bcc_values,
            headers=headers,
            attachments=attachments,
        )

        args = ["message", "send"]
        _add_account_arg(args, account)
        return await _run_himalaya(args, output_format="plain", stdin_text=raw)

    if action_name == "download_attachments":
        ids = _normalize_ids(kwargs.get("message_ids"))
        if not ids:
            ids = _normalize_ids(kwargs.get("message_id"))
        if not ids:
            return {"ok": False, "error": "message_id or message_ids is required"}

        downloads_dir = _resolve_output_dir(str(kwargs.get("download_dir", "")), "attachments")

        args = ["attachment", "download"]
        if folder:
            args.extend(["--folder", folder])
        args.extend(["--downloads-dir", downloads_dir])
        args.extend(ids)
        _add_account_arg(args, account)

        result = await _run_himalaya(args, output_format="plain")
        result["downloads_dir"] = downloads_dir
        downloaded_files = _extract_downloaded_paths(
            f"{result.get('stdout', '')}\n{result.get('stderr', '')}"
        )
        if downloaded_files:
            result["downloaded_files"] = downloaded_files
        return result

    if action_name in {"extract_message", "export_message"}:
        ids = _normalize_ids(kwargs.get("message_id"))
        if not ids:
            return {"ok": False, "error": "message_id is required"}

        message_id = ids[0]
        full = bool(kwargs.get("full", False))
        destination_raw = str(
            kwargs.get("destination_path")
            or kwargs.get("download_dir")
            or kwargs.get("output_path")
            or ""
        ).strip()

        if full:
            destination = _resolve_output_file(
                destination_raw,
                f"message_{message_id}.eml",
            )
        else:
            destination = _resolve_output_dir(destination_raw, f"message_{message_id}")

        args = ["message", "export"]
        if folder:
            args.extend(["--folder", folder])
        if full:
            args.append("--full")
        args.extend(["--destination", destination])
        args.append(message_id)
        _add_account_arg(args, account)

        result = await _run_himalaya(args, output_format="plain")
        result["destination"] = destination
        result["full"] = full
        return result

    if action_name == "reply_message":
        message_id = kwargs.get("message_id")
        ids = _normalize_ids(message_id)
        if not ids:
            return {"ok": False, "error": "message_id is required"}

        body = str(kwargs.get("body", "")).strip()
        reply_all = bool(kwargs.get("reply_all", False))

        tpl_args = ["template", "reply"]
        if folder:
            tpl_args.extend(["--folder", folder])
        if reply_all:
            tpl_args.append("--all")

        headers = _coerce_headers(kwargs.get("headers"))
        for key, value in headers.items():
            tpl_args.extend(["--header", f"{key}:{value}"])

        tpl_args.append(ids[0])
        if body:
            tpl_args.append(body)
        _add_account_arg(tpl_args, account)

        tpl_res = await _run_himalaya(tpl_args, output_format="plain")
        if not tpl_res.get("ok"):
            return tpl_res

        template_raw = tpl_res.get("stdout", "")
        if not template_raw.strip():
            return {"ok": False, "error": "Failed to generate reply template."}

        send_args = ["template", "send"]
        _add_account_arg(send_args, account)
        send_res = await _run_himalaya(send_args, output_format="plain", stdin_text=template_raw)
        send_res["template_preview"] = template_raw[:1200]
        return send_res

    if action_name == "forward_message":
        message_id = kwargs.get("message_id")
        ids = _normalize_ids(message_id)
        if not ids:
            return {"ok": False, "error": "message_id is required"}

        body = str(kwargs.get("body", "")).strip()

        tpl_args = ["template", "forward"]
        if folder:
            tpl_args.extend(["--folder", folder])

        headers = _coerce_headers(kwargs.get("headers"))
        for key, value in headers.items():
            tpl_args.extend(["--header", f"{key}:{value}"])

        tpl_args.append(ids[0])
        if body:
            tpl_args.append(body)
        _add_account_arg(tpl_args, account)

        tpl_res = await _run_himalaya(tpl_args, output_format="plain")
        if not tpl_res.get("ok"):
            return tpl_res

        template_raw = tpl_res.get("stdout", "")
        if not template_raw.strip():
            return {"ok": False, "error": "Failed to generate forward template."}

        send_args = ["template", "send"]
        _add_account_arg(send_args, account)
        send_res = await _run_himalaya(send_args, output_format="plain", stdin_text=template_raw)
        send_res["template_preview"] = template_raw[:1200]
        return send_res

    if action_name == "move_messages":
        ids = _normalize_ids(kwargs.get("message_ids"))
        if not ids:
            return {"ok": False, "error": "message_ids is required"}

        target_folder = str(kwargs.get("target_folder", "")).strip()
        if not target_folder:
            return {"ok": False, "error": "target_folder is required"}

        args = ["message", "move"]
        if folder:
            args.extend(["--folder", folder])
        args.append(target_folder)
        args.extend(ids)
        _add_account_arg(args, account)
        return await _run_himalaya(args, output_format="plain")

    if action_name == "copy_messages":
        ids = _normalize_ids(kwargs.get("message_ids"))
        if not ids:
            return {"ok": False, "error": "message_ids is required"}

        target_folder = str(kwargs.get("target_folder", "")).strip()
        if not target_folder:
            return {"ok": False, "error": "target_folder is required"}

        args = ["message", "copy"]
        if folder:
            args.extend(["--folder", folder])
        args.append(target_folder)
        args.extend(ids)
        _add_account_arg(args, account)
        return await _run_himalaya(args, output_format="plain")

    if action_name == "delete_messages":
        ids = _normalize_ids(kwargs.get("message_ids"))
        if not ids:
            return {"ok": False, "error": "message_ids is required"}

        args = ["message", "delete"]
        if folder:
            args.extend(["--folder", folder])
        args.extend(ids)
        _add_account_arg(args, account)
        return await _run_himalaya(args, output_format="plain")

    return {
        "ok": False,
        "error": f"Unsupported action: {action_name}",
        "supported_actions": [
            "list_accounts",
            "list_folders",
            "list_messages",
            "read_message",
            "thread_message",
            "send_message",
            "download_attachments",
            "extract_message",
            "export_message",
            "reply_message",
            "forward_message",
            "move_messages",
            "copy_messages",
            "delete_messages",
        ],
    }

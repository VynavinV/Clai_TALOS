import os
import re
import sys
import time
import difflib
import logging
import platform
import tempfile
import shutil
from enum import Enum

logger = logging.getLogger("talos.file_tools")

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

MAX_FILE_SIZE = 2 * 1024 * 1024
MAX_READ_LINES = 2000
DEFAULT_READ_LINES = 500

WRITE_RETRIES = 3
WRITE_RETRY_DELAY = 0.15

BLOCKED_DIRS = {
    "/etc", "/var", "/usr", "/bin", "/sbin", "/boot", "/dev",
    "/proc", "/sys", "/lib", "/System", "/Library",
}

if sys.platform == "win32":
    _system_drive = os.environ.get("SystemDrive", "C:")
    BLOCKED_DIRS.update({
        os.path.normpath(f"{_system_drive}\\Windows"),
        os.path.normpath(f"{_system_drive}\\Program Files"),
        os.path.normpath(f"{_system_drive}\\Program Files (x86)"),
        os.path.normpath(f"{_system_drive}\\ProgramData"),
    })

BLOCKED_PATTERNS = re.compile(
    r"(\.env|\.credentials|\.ssh|\.gnupg|id_rsa|id_ed25519|\.pem|\.key)"
    r"|(\.git/|\.svn/|\.hg/)"
    r"|(__pycache__|\.pyc|node_modules/)"
)

ENCODING_PRIORITY = ["utf-8", "utf-8-sig", "latin-1", "cp1252", "ascii"]


class FileError(Enum):
    ACCESS_DENIED = "access_denied"
    NOT_FOUND = "not_found"
    TOO_LARGE = "too_large"
    ENCODING = "encoding_error"
    PERMISSION = "permission_denied"
    DISK_FULL = "disk_full"
    LOCKED = "file_locked"
    NO_MATCH = "no_match"
    AMBIGUOUS = "ambiguous_match"
    IDENTICAL = "identical_content"
    MISSING_PARAM = "missing_parameter"
    DIR_NOT_FOUND = "directory_not_found"
    UNKNOWN = "unknown_error"


def _make_error(code: FileError, message: str, path: str = "", hint: str = "", recoverable: bool = False) -> dict:
    return {
        "error": message,
        "error_code": code.value,
        "path": path,
        "hint": hint,
        "recoverable": recoverable,
    }


def _resolve_path(path: str) -> str:
    if not os.path.isabs(path):
        path = os.path.join(_SCRIPT_DIR, path)
    return os.path.realpath(path)


def _validate_path(path: str) -> tuple[str, str | None]:
    resolved = _resolve_path(path)
    normalized = resolved.replace("\\", "/")

    parts = normalized.split("/")
    for blocked in BLOCKED_DIRS:
        blocked_parts = blocked.replace("\\", "/").strip("/").split("/")
        for i in range(len(parts) - len(blocked_parts) + 1):
            if parts[i:i + len(blocked_parts)] == blocked_parts:
                return "", f"Access denied: {blocked} is a protected system directory"

    if BLOCKED_PATTERNS.search(normalized):
        return "", "Access denied: path matches a blocked pattern (secrets/vcs/cache)"

    return resolved, None


def _detect_encoding(path: str) -> tuple[str, str | None]:
    raw = b""
    with open(path, "rb") as f:
        raw = f.read(min(8192, MAX_FILE_SIZE))

    if raw[:3] == b"\xef\xbb\xbf":
        return "utf-8-sig", None
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return "utf-16", None

    for enc in ENCODING_PRIORITY:
        try:
            raw.decode(enc)
            return enc, None
        except (UnicodeDecodeError, LookupError):
            continue

    return "utf-8", "Could not detect encoding, falling back to utf-8 with replacement"


def _read_content(path: str) -> tuple[str, str | None, str]:
    encoding, enc_hint = _detect_encoding(path)
    with open(path, "r", encoding=encoding, errors="replace") as f:
        content = f.read()
    return content, enc_hint, encoding


def _atomic_write(path: str, content: str, encoding: str = "utf-8") -> None:
    parent = os.path.dirname(path)
    fd, tmp_path = tempfile.mkstemp(dir=parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding, errors="replace") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        shutil.move(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _retry_write(path: str, content: str, encoding: str = "utf-8") -> tuple[bool, str]:
    last_err = ""
    for attempt in range(1, WRITE_RETRIES + 1):
        try:
            _atomic_write(path, content, encoding)
            return True, ""
        except PermissionError as e:
            last_err = str(e)
            logger.warning(f"write_file permission error (attempt {attempt}/{WRITE_RETRIES}): {path}: {e}")
            if attempt < WRITE_RETRIES:
                time.sleep(WRITE_RETRY_DELAY * attempt)
        except OSError as e:
            if "No space left" in str(e) or "ENOSPC" in str(e):
                return False, f"Disk full: {e}"
            last_err = str(e)
            logger.warning(f"write_file OS error (attempt {attempt}/{WRITE_RETRIES}): {path}: {e}")
            if attempt < WRITE_RETRIES:
                time.sleep(WRITE_RETRY_DELAY * attempt)
        except Exception as e:
            last_err = str(e)
            logger.exception(f"write_file unexpected error (attempt {attempt}/{WRITE_RETRIES}): {path}")
            if attempt < WRITE_RETRIES:
                time.sleep(WRITE_RETRY_DELAY * attempt)
    return False, last_err


def _build_diff(old: str, new: str, max_lines: int = 100) -> dict:
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    diff = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile="before", tofile="after", lineterm=""
    ))
    truncated = len(diff) > max_lines
    return {
        "diff": diff[:max_lines],
        "diff_lines_added": sum(1 for d in diff if d.startswith("+") and not d.startswith("+++")),
        "diff_lines_removed": sum(1 for d in diff if d.startswith("-") and not d.startswith("---")),
        "diff_truncated": truncated,
    }


def _fuzzy_find(old_string: str, content: str) -> str:
    old_stripped = old_string.strip()
    if not old_stripped:
        return ""

    content_lines = content.splitlines()
    old_lines = old_string.splitlines()

    best_ratio = 0.0
    best_start = -1

    for i in range(len(content_lines) - len(old_lines) + 1):
        window = "\n".join(content_lines[i:i + len(old_lines)])
        ratio = difflib.SequenceMatcher(None, old_string, window).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_start = i

    if best_ratio < 0.6 or best_start < 0:
        return ""

    snippet_lines = content_lines[max(0, best_start - 1):best_start + len(old_lines) + 2]
    snippet = "\n".join(f"  {j + best_start}:{line}" for j, line in enumerate(snippet_lines))

    return (
        f"Possible fuzzy match at line {best_start + 1} (similarity: {best_ratio:.0%}):\n"
        f"{snippet}\n\n"
        f"The content at that location differs from your old_string. "
        f"Read the file around that line and try again with the exact content."
    )


def read_file(path: str, offset: int = 0, limit: int = DEFAULT_READ_LINES) -> dict:
    resolved, err = _validate_path(path)
    if err:
        return _make_error(FileError.ACCESS_DENIED, err, path)

    if not os.path.isfile(resolved):
        return _make_error(FileError.NOT_FOUND, f"File not found: {path}", path,
                           hint="Check the file path. Use execute_command with 'ls' to find files.")

    try:
        size = os.path.getsize(resolved)
        if size > MAX_FILE_SIZE:
            return _make_error(FileError.TOO_LARGE,
                               f"File too large: {size} bytes (max {MAX_FILE_SIZE})",
                               resolved,
                               hint=f"Use offset/limit to read specific sections, or use execute_command with 'head -n 100 {path}'",
                               recoverable=True)

        content, enc_hint, encoding = _read_content(resolved)
        lines = content.splitlines(True)
        total_lines = len(lines)

        if offset < 0:
            offset = max(0, total_lines + offset)

        if offset >= total_lines:
            return {
                "path": resolved,
                "total_lines": total_lines,
                "lines": [],
                "offset": offset,
                "encoding": encoding,
                "message": "Offset beyond end of file",
            }

        end = min(offset + limit, total_lines)
        selected = lines[offset:end]

        numbered = []
        for i, line in enumerate(selected):
            line_num = offset + i + 1
            content_str = line.rstrip("\n").rstrip("\r")
            if len(content_str) > 2000:
                content_str = content_str[:2000] + "... [truncated]"
            numbered.append({"line": line_num, "content": content_str})

        result = {
            "path": resolved,
            "total_lines": total_lines,
            "offset": offset,
            "limit": limit,
            "lines_shown": len(numbered),
            "truncated": end < total_lines,
            "encoding": encoding,
            "bytes": size,
            "lines": numbered,
        }

        if enc_hint:
            result["encoding_hint"] = enc_hint

        return result

    except PermissionError as e:
        return _make_error(FileError.PERMISSION, f"Permission denied: {e}", resolved,
                           hint="Check file permissions with execute_command: 'ls -la <path>'",
                           recoverable=True)
    except Exception as e:
        logger.exception(f"read_file error: {path}")
        return _make_error(FileError.UNKNOWN, str(e), resolved, recoverable=True)


def write_file(path: str, content: str, create_dirs: bool = False) -> dict:
    if not path:
        return _make_error(FileError.MISSING_PARAM, "path is required")

    resolved, err = _validate_path(path)
    if err:
        return _make_error(FileError.ACCESS_DENIED, err, path)

    parent = os.path.dirname(resolved)
    if not os.path.isdir(parent):
        if create_dirs:
            try:
                os.makedirs(parent, exist_ok=True)
            except Exception as e:
                return _make_error(FileError.DIR_NOT_FOUND, f"Failed to create directories: {e}", parent,
                                   recoverable=True)
        else:
            return _make_error(FileError.DIR_NOT_FOUND,
                               f"Directory does not exist: {parent}",
                               parent,
                               hint="Set create_dirs=true to create parent directories automatically.",
                               recoverable=True)

    if len(content) > MAX_FILE_SIZE:
        return _make_error(FileError.TOO_LARGE,
                           f"Content too large: {len(content)} bytes (max {MAX_FILE_SIZE})",
                           resolved)

    try:
        existing = None
        encoding = "utf-8"
        if os.path.isfile(resolved):
            existing, _, encoding = _read_content(resolved)

        success, write_err = _retry_write(resolved, content, encoding)
        if not success:
            code = FileError.DISK_FULL if "full" in write_err.lower() else FileError.LOCKED
            return _make_error(code, f"Write failed after {WRITE_RETRIES} retries: {write_err}", resolved,
                               hint="The file may be locked by another process. Close it and try again.",
                               recoverable=True)

        lines_written = content.count("\n") + (1 if content and not content.endswith("\n") else 0)

        result = {
            "path": resolved,
            "status": "created" if existing is None else "overwritten",
            "lines": lines_written,
            "bytes": len(content),
            "encoding": encoding,
        }

        if existing is not None:
            diff_info = _build_diff(existing, content)
            result.update(diff_info)
            if existing == content:
                result["warning"] = "File content is identical — no actual changes made"

        return result

    except PermissionError as e:
        return _make_error(FileError.PERMISSION, f"Permission denied: {e}", resolved,
                           hint="Check directory permissions: 'ls -la <parent_dir>'",
                           recoverable=True)
    except Exception as e:
        logger.exception(f"write_file error: {path}")
        return _make_error(FileError.UNKNOWN, str(e), resolved, recoverable=True)


def edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False) -> dict:
    if not old_string:
        return _make_error(FileError.MISSING_PARAM, "old_string is required and cannot be empty",
                           hint="Provide the exact text you want to replace.")
    if old_string == new_string:
        return _make_error(FileError.IDENTICAL, "old_string and new_string are identical — nothing to change",
                           path,
                           hint="If you intended to make a change, verify the new_string differs from old_string.")

    resolved, err = _validate_path(path)
    if err:
        return _make_error(FileError.ACCESS_DENIED, err, path)

    if not os.path.isfile(resolved):
        return _make_error(FileError.NOT_FOUND, f"File not found: {path}", path,
                           hint="Check the file path. The file may have been moved or deleted.")

    try:
        size = os.path.getsize(resolved)
        if size > MAX_FILE_SIZE:
            return _make_error(FileError.TOO_LARGE,
                               f"File too large: {size} bytes (max {MAX_FILE_SIZE})",
                               resolved,
                               hint="For large files, use write_file to rewrite the entire file.",
                               recoverable=True)

        content, enc_hint, encoding = _read_content(resolved)

        if old_string not in content:
            fuzzy = _fuzzy_find(old_string, content)

            lines_preview = []
            for i, line in enumerate(content.splitlines(), 1):
                stripped = line.strip()
                if stripped:
                    lines_preview.append(f"  {i}: {stripped[:120]}")
                    if len(lines_preview) >= 20:
                        break

            hint_parts = [
                "Make sure old_string matches the file content EXACTLY:",
                "- Indentation (spaces vs tabs, exact count)",
                "- Trailing/leading whitespace",
                "- Line endings (\\n vs \\r\\n)",
                "- The file encoding may differ from what you expect",
            ]
            if enc_hint:
                hint_parts.append(f"- Encoding detected: {encoding} ({enc_hint})")

            if fuzzy:
                hint_parts.append(f"\n{fuzzy}")

            hint_parts.append(f"\nFile preview (first 20 non-empty lines of {resolved}):\n" + "\n".join(lines_preview))

            return _make_error(
                FileError.NO_MATCH,
                f"old_string not found in {path}",
                resolved,
                hint="\n".join(hint_parts),
                recoverable=True,
            )

        count = content.count(old_string)
        if count > 1 and not replace_all:
            lines = content.splitlines()
            occurrences = []
            found = 0

            for i in range(len(lines)):
                if found >= count:
                    break
                window = "\n".join(lines[i:i + len(old_string.splitlines())])
                if old_string in window or (i < len(lines) and old_string in lines[i]):
                    start = max(0, i - 2)
                    end = min(len(lines), i + len(old_string.splitlines()) + 3)
                    context = "\n".join(f"  {j + 1}: {lines[j]}" for j in range(start, end))
                    occurrences.append(f"Occurrence #{found + 1} near line {i + 1}:\n{context}")
                    found += 1

            return _make_error(
                FileError.AMBIGUOUS,
                f"old_string found {count} times in {path}",
                resolved,
                hint=(
                    f"Include more surrounding context (3-5 lines before/after) to make the match unique, "
                    f"or set replace_all=true to replace all {count} occurrences.\n\n"
                    + "\n\n".join(occurrences[:5])
                ),
                recoverable=True,
            )

        new_content = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)

        success, write_err = _retry_write(resolved, new_content, encoding)
        if not success:
            code = FileError.DISK_FULL if "full" in write_err.lower() else FileError.LOCKED
            return _make_error(code, f"Write failed after {WRITE_RETRIES} retries: {write_err}", resolved,
                               recoverable=True)

        diff_info = _build_diff(content, new_content)

        return {
            "path": resolved,
            "replacements": count if replace_all else 1,
            "total_occurrences": count,
            "encoding": encoding,
            **diff_info,
        }

    except PermissionError as e:
        return _make_error(FileError.PERMISSION, f"Permission denied: {e}", resolved,
                           hint="Check file permissions: 'ls -la <path>'",
                           recoverable=True)
    except Exception as e:
        logger.exception(f"edit_file error: {path}")
        return _make_error(FileError.UNKNOWN, str(e), resolved, recoverable=True)

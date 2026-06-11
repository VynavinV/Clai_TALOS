import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from typing import Any
from xml.sax.saxutils import escape
import xml.etree.ElementTree as ET

import file_tools

logger = logging.getLogger("talos.docx_tools")

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XMLNS = {"w": W_NS}

UNICODE_BULLETS = ["\u2022", "\u25e6", "\u25aa", "\u25cf", "\u25a0", "\u2023"]
TEXT_NODE_RE = re.compile(r"(<w:t[^>]*>)(.*?)(</w:t>)", flags=re.DOTALL)


def _error(message: str, hint: str = "") -> dict:
    payload = {"ok": False, "error": message}
    if hint:
        payload["hint"] = hint
    return payload


def _resolve_path(path: str, must_exist: bool = True) -> tuple[str | None, dict | None]:
    resolved, err = file_tools._validate_path(path)
    if err:
        return None, _error(err)

    if must_exist and not os.path.isfile(resolved):
        return None, _error(f"DOCX file not found: {path}")

    return resolved, None


def _extract_docx(path: str) -> tuple[str | None, dict | None]:
    temp_dir = tempfile.mkdtemp(prefix="talos_docx_")
    try:
        with zipfile.ZipFile(path, "r") as zf:
            zf.extractall(temp_dir)
        return temp_dir, None
    except Exception as e:
        shutil.rmtree(temp_dir, ignore_errors=True)
        return None, _error(f"Failed to unpack docx: {e}")


def _repack_docx(extracted_dir: str, output_path: str) -> tuple[bool, dict | None]:
    try:
        fd, tmp_zip = tempfile.mkstemp(suffix=".docx")
        os.close(fd)
        with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(extracted_dir):
                for name in files:
                    full = os.path.join(root, name)
                    rel = os.path.relpath(full, extracted_dir)
                    zf.write(full, rel)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        shutil.move(tmp_zip, output_path)
        return True, None
    except Exception as e:
        return False, _error(f"Failed to repack docx: {e}")


def _replace_bullets(text: str) -> str:
    out = text
    for bullet in UNICODE_BULLETS:
        out = out.replace(bullet, "-")
    return out


def _replace_straight_double_quotes(text: str) -> str:
    out = []
    is_open = True
    for ch in text:
        if ch == '"':
            out.append("&#x201C;" if is_open else "&#x201D;")
            is_open = not is_open
        else:
            out.append(ch)
    return "".join(out)


def _smart_quotes_to_entities(text: str) -> str:
    text = text.replace("\u2018", "&#x2018;").replace("\u2019", "&#x2019;")
    text = text.replace("\u201C", "&#x201C;").replace("\u201D", "&#x201D;")
    # Convert apostrophes between word characters
    text = re.sub(r"(?<=\w)'(?=\w)", "&#x2019;", text)
    text = _replace_straight_double_quotes(text)
    return text


def _normalize_text_nodes(xml_content: str, no_unicode_bullets: bool, smart_quotes_entities: bool) -> tuple[str, int]:
    replacements = 0

    def repl(match: re.Match) -> str:
        nonlocal replacements
        open_tag, content, close_tag = match.group(1), match.group(2), match.group(3)
        updated = content

        if no_unicode_bullets:
            updated = _replace_bullets(updated)
        if smart_quotes_entities:
            updated = _smart_quotes_to_entities(updated)

        if updated != content:
            replacements += 1
        return open_tag + updated + close_tag

    normalized = TEXT_NODE_RE.sub(repl, xml_content)
    return normalized, replacements


def _list_word_xml_files(extracted_dir: str) -> list[str]:
    word_dir = os.path.join(extracted_dir, "word")
    if not os.path.isdir(word_dir):
        return []
    xml_files = []
    for root, _, files in os.walk(word_dir):
        for name in files:
            if name.lower().endswith(".xml"):
                xml_files.append(os.path.join(root, name))
    return sorted(xml_files)


def validate_xml(path: str) -> dict:
    resolved, err = _resolve_path(path, must_exist=True)
    if err:
        return err

    try:
        with zipfile.ZipFile(resolved, "r") as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
            errors = []
            for name in names:
                raw = zf.read(name)
                try:
                    ET.fromstring(raw)
                except Exception as e:
                    errors.append({"xml": name, "error": str(e)})

        return {
            "ok": True,
            "path": resolved,
            "xml_files_checked": len(names),
            "xml_errors": errors,
            "is_valid": len(errors) == 0,
        }

    except Exception as e:
        logger.exception("validate_xml failed: %s", path)
        return _error(str(e))


def create_with_docx_js(
    path: str,
    title: str = "",
    paragraphs: list[str] | None = None,
    page_width_dxa: int = 12240,
    page_height_dxa: int = 15840,
    table_rows: list[list[str]] | None = None,
) -> dict:
    resolved, err = _resolve_path(path, must_exist=False)
    if err:
        return err

    parent = os.path.dirname(resolved)
    if not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)

    payload = {
        "title": str(title or ""),
        "paragraphs": [str(p) for p in (paragraphs or [])],
        "pageWidth": int(page_width_dxa),
        "pageHeight": int(page_height_dxa),
        "tableRows": [[str(cell) for cell in row] for row in (table_rows or [])],
    }

    payload["title"] = _replace_bullets(payload["title"])
    payload["paragraphs"] = [_replace_bullets(p) for p in payload["paragraphs"]]
    payload["tableRows"] = [[_replace_bullets(cell) for cell in row] for row in payload["tableRows"]]

    js_code = r'''
const fs = require("fs");
const { Document, Packer, Paragraph, Table, TableRow, TableCell, WidthType } = require("docx");

async function main() {
  const configPath = process.argv[2];
  const outputPath = process.argv[3];
  const raw = fs.readFileSync(configPath, "utf8");
  const cfg = JSON.parse(raw);

  const children = [];
  if (cfg.title) {
    children.push(new Paragraph({ text: cfg.title, heading: "Title" }));
  }

  for (const p of (cfg.paragraphs || [])) {
    children.push(new Paragraph({ text: p }));
  }

  if ((cfg.tableRows || []).length > 0) {
    const rows = cfg.tableRows.map((row) => new TableRow({
      children: row.map((cellText) => new TableCell({
        width: { size: 2400, type: WidthType.DXA },
        children: [new Paragraph({ text: String(cellText || "") })],
      })),
    }));

    children.push(new Table({
      rows,
      width: { size: 9600, type: WidthType.DXA },
    }));
  }

  const doc = new Document({
    sections: [{
      properties: {
        page: {
          size: {
            width: Number(cfg.pageWidth || 12240),
            height: Number(cfg.pageHeight || 15840),
          },
        },
      },
      children,
    }],
  });

  const buffer = await Packer.toBuffer(doc);
  fs.writeFileSync(outputPath, buffer);
}

main().catch((err) => {
  console.error(String(err && err.stack ? err.stack : err));
  process.exit(1);
});
'''

    temp_dir = tempfile.mkdtemp(prefix="talos_docx_js_")
    try:
        js_path = os.path.join(temp_dir, "build_docx.js")
        cfg_path = os.path.join(temp_dir, "payload.json")

        with open(js_path, "w", encoding="utf-8") as f:
            f.write(js_code)
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=True)

        cmd = ["node", js_path, cfg_path, resolved]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            if "Cannot find module 'docx'" in stderr:
                return _error(
                    "Node module 'docx' is missing",
                    "Install it in the project root: npm install docx",
                )
            return _error("Failed to create DOCX via JavaScript", hint=stderr or (proc.stdout or "").strip())

        normalize = normalize_text(
            path=resolved,
            no_unicode_bullets=True,
            smart_quotes_entities=True,
            xml_paths=[],
        )
        if not normalize.get("ok"):
            return normalize

        validation = validate_xml(resolved)
        if not validation.get("ok"):
            return validation

        return {
            "ok": True,
            "path": resolved,
            "created_with": "javascript-docx",
            "normalized": normalize,
            "validation": validation,
        }

    except subprocess.TimeoutExpired:
        return _error("DOCX JavaScript generation timed out")
    except FileNotFoundError:
        return _error(
            "Node.js is not available",
            "Install Node.js and npm, then run: npm install docx",
        )
    except Exception as e:
        logger.exception("create_with_docx_js failed: %s", path)
        return _error(str(e))
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def edit_xml(path: str, edits: list[dict], validate_after: bool = True) -> dict:
    if not isinstance(edits, list) or not edits:
        return _error("edits must be a non-empty list")

    resolved, err = _resolve_path(path, must_exist=True)
    if err:
        return err

    extracted_dir, unpack_err = _extract_docx(resolved)
    if unpack_err:
        return unpack_err

    replacements = []
    try:
        for index, edit in enumerate(edits, start=1):
            if not isinstance(edit, dict):
                return _error(f"edits[{index}] must be an object")

            rel = str(edit.get("xml_path", "word/document.xml")).strip()
            if not rel:
                rel = "word/document.xml"
            target = os.path.join(extracted_dir, rel)
            if not os.path.isfile(target):
                return _error(f"XML file not found in DOCX: {rel}")

            find = str(edit.get("find", ""))
            replace = str(edit.get("replace", ""))
            replace_all = bool(edit.get("replace_all", False))

            with open(target, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()

            count = content.count(find) if find else 0
            if find and count == 0:
                return _error(f"No match for edit in {rel}")

            if find:
                new_content = content.replace(find, replace) if replace_all else content.replace(find, replace, 1)
            else:
                new_content = content

            with open(target, "w", encoding="utf-8") as f:
                f.write(new_content)

            replacements.append({
                "index": index,
                "xml_path": rel,
                "matches": count,
                "applied": count if replace_all else (1 if count > 0 else 0),
            })

        ok, repack_err = _repack_docx(extracted_dir, resolved)
        if not ok:
            return repack_err or _error("Failed to repack DOCX")

        validation = validate_xml(resolved) if validate_after else {"ok": True, "is_valid": True}

        return {
            "ok": True,
            "path": resolved,
            "edits_applied": replacements,
            "validation": validation,
        }

    finally:
        shutil.rmtree(extracted_dir, ignore_errors=True)


def track_replace(path: str, old_text: str, new_text: str, author: str = "TALOS") -> dict:
    if not old_text:
        return _error("old_text is required")

    resolved, err = _resolve_path(path, must_exist=True)
    if err:
        return err

    extracted_dir, unpack_err = _extract_docx(resolved)
    if unpack_err:
        return unpack_err

    try:
        target = os.path.join(extracted_dir, "word", "document.xml")
        if not os.path.isfile(target):
            return _error("word/document.xml not found")

        with open(target, "r", encoding="utf-8", errors="replace") as f:
            xml = f.read()

        escaped_old = escape(old_text)
        escaped_new = escape(new_text)
        escaped_new = _smart_quotes_to_entities(escaped_new)

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        replacement_block = (
            f"<w:del w:id=\"1\" w:author=\"{escape(author)}\" w:date=\"{timestamp}\">"
            f"<w:r><w:delText xml:space=\"preserve\">{escaped_old}</w:delText></w:r></w:del>"
            f"<w:ins w:id=\"2\" w:author=\"{escape(author)}\" w:date=\"{timestamp}\">"
            f"<w:r><w:t xml:space=\"preserve\">{escaped_new}</w:t></w:r></w:ins>"
        )

        candidate = f"<w:t>{escaped_old}</w:t>"
        if candidate not in xml:
            candidate = f"<w:t xml:space=\"preserve\">{escaped_old}</w:t>"
            if candidate not in xml:
                return _error("Could not locate old_text in a single text run for tracked replace")

        xml_new = xml.replace(candidate, replacement_block, 1)
        with open(target, "w", encoding="utf-8") as f:
            f.write(xml_new)

        ok, repack_err = _repack_docx(extracted_dir, resolved)
        if not ok:
            return repack_err or _error("Failed to repack DOCX")

        validation = validate_xml(resolved)
        return {
            "ok": True,
            "path": resolved,
            "tracked_change": True,
            "validation": validation,
        }

    finally:
        shutil.rmtree(extracted_dir, ignore_errors=True)


def set_page_size_dxa(path: str, width_dxa: int, height_dxa: int) -> dict:
    resolved, err = _resolve_path(path, must_exist=True)
    if err:
        return err

    extracted_dir, unpack_err = _extract_docx(resolved)
    if unpack_err:
        return unpack_err

    try:
        target = os.path.join(extracted_dir, "word", "document.xml")
        if not os.path.isfile(target):
            return _error("word/document.xml not found")

        ET.register_namespace("w", W_NS)
        tree = ET.parse(target)
        root = tree.getroot()

        count = 0
        for sect_pr in root.findall(".//w:sectPr", XMLNS):
            pg_sz = sect_pr.find("w:pgSz", XMLNS)
            if pg_sz is None:
                pg_sz = ET.SubElement(sect_pr, f"{{{W_NS}}}pgSz")
            pg_sz.set(f"{{{W_NS}}}w", str(int(width_dxa)))
            pg_sz.set(f"{{{W_NS}}}h", str(int(height_dxa)))
            count += 1

        if count == 0:
            body = root.find(".//w:body", XMLNS)
            if body is None:
                return _error("Could not locate document body")
            sect_pr = ET.SubElement(body, f"{{{W_NS}}}sectPr")
            pg_sz = ET.SubElement(sect_pr, f"{{{W_NS}}}pgSz")
            pg_sz.set(f"{{{W_NS}}}w", str(int(width_dxa)))
            pg_sz.set(f"{{{W_NS}}}h", str(int(height_dxa)))
            count = 1

        tree.write(target, encoding="utf-8", xml_declaration=True)

        ok, repack_err = _repack_docx(extracted_dir, resolved)
        if not ok:
            return repack_err or _error("Failed to repack DOCX")

        validation = validate_xml(resolved)
        return {
            "ok": True,
            "path": resolved,
            "sections_updated": count,
            "page_width_dxa": int(width_dxa),
            "page_height_dxa": int(height_dxa),
            "validation": validation,
        }

    finally:
        shutil.rmtree(extracted_dir, ignore_errors=True)


def set_table_widths_dxa(path: str, width_dxa: int) -> dict:
    resolved, err = _resolve_path(path, must_exist=True)
    if err:
        return err

    extracted_dir, unpack_err = _extract_docx(resolved)
    if unpack_err:
        return unpack_err

    try:
        target = os.path.join(extracted_dir, "word", "document.xml")
        if not os.path.isfile(target):
            return _error("word/document.xml not found")

        ET.register_namespace("w", W_NS)
        tree = ET.parse(target)
        root = tree.getroot()

        table_count = 0
        cell_width_updates = 0

        for tbl in root.findall(".//w:tbl", XMLNS):
            table_count += 1
            tbl_pr = tbl.find("w:tblPr", XMLNS)
            if tbl_pr is None:
                tbl_pr = ET.SubElement(tbl, f"{{{W_NS}}}tblPr")
            tbl_w = tbl_pr.find("w:tblW", XMLNS)
            if tbl_w is None:
                tbl_w = ET.SubElement(tbl_pr, f"{{{W_NS}}}tblW")
            tbl_w.set(f"{{{W_NS}}}type", "dxa")
            tbl_w.set(f"{{{W_NS}}}w", str(int(width_dxa)))

            first_row = tbl.find("w:tr", XMLNS)
            col_count = len(first_row.findall("w:tc", XMLNS)) if first_row is not None else 0
            per_cell = int(width_dxa / col_count) if col_count > 0 else int(width_dxa)

            for tc in tbl.findall(".//w:tc", XMLNS):
                tc_pr = tc.find("w:tcPr", XMLNS)
                if tc_pr is None:
                    tc_pr = ET.SubElement(tc, f"{{{W_NS}}}tcPr")
                tc_w = tc_pr.find("w:tcW", XMLNS)
                if tc_w is None:
                    tc_w = ET.SubElement(tc_pr, f"{{{W_NS}}}tcW")
                tc_w.set(f"{{{W_NS}}}type", "dxa")
                tc_w.set(f"{{{W_NS}}}w", str(per_cell))
                cell_width_updates += 1

        tree.write(target, encoding="utf-8", xml_declaration=True)

        ok, repack_err = _repack_docx(extracted_dir, resolved)
        if not ok:
            return repack_err or _error("Failed to repack DOCX")

        validation = validate_xml(resolved)
        return {
            "ok": True,
            "path": resolved,
            "tables_updated": table_count,
            "cells_updated": cell_width_updates,
            "table_width_dxa": int(width_dxa),
            "validation": validation,
        }

    finally:
        shutil.rmtree(extracted_dir, ignore_errors=True)


def normalize_text(
    path: str,
    no_unicode_bullets: bool = True,
    smart_quotes_entities: bool = True,
    xml_paths: list[str] | None = None,
) -> dict:
    resolved, err = _resolve_path(path, must_exist=True)
    if err:
        return err

    extracted_dir, unpack_err = _extract_docx(resolved)
    if unpack_err:
        return unpack_err

    try:
        if xml_paths and isinstance(xml_paths, list):
            files = [os.path.join(extracted_dir, str(p)) for p in xml_paths]
        else:
            files = _list_word_xml_files(extracted_dir)

        touched = []
        for xml_file in files:
            if not os.path.isfile(xml_file):
                continue
            with open(xml_file, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()

            updated, replacements = _normalize_text_nodes(
                content,
                no_unicode_bullets=bool(no_unicode_bullets),
                smart_quotes_entities=bool(smart_quotes_entities),
            )

            if replacements > 0:
                with open(xml_file, "w", encoding="utf-8") as f:
                    f.write(updated)
                touched.append({
                    "xml": os.path.relpath(xml_file, extracted_dir).replace("\\", "/"),
                    "replacements": replacements,
                })

        ok, repack_err = _repack_docx(extracted_dir, resolved)
        if not ok:
            return repack_err or _error("Failed to repack DOCX")

        validation = validate_xml(resolved)
        return {
            "ok": True,
            "path": resolved,
            "files_touched": touched,
            "validation": validation,
        }

    finally:
        shutil.rmtree(extracted_dir, ignore_errors=True)


def execute(action: str, **kwargs) -> dict:
    normalized = str(action or "").strip().lower()

    if normalized in {"create_with_docx_js", "create", "create_js"}:
        return create_with_docx_js(
            path=str(kwargs.get("path", "")).strip(),
            title=str(kwargs.get("title", "")).strip(),
            paragraphs=kwargs.get("paragraphs") if isinstance(kwargs.get("paragraphs"), list) else [],
            page_width_dxa=kwargs.get("page_width_dxa", 12240),
            page_height_dxa=kwargs.get("page_height_dxa", 15840),
            table_rows=kwargs.get("table_rows") if isinstance(kwargs.get("table_rows"), list) else [],
        )

    if normalized in {"edit_xml", "edit"}:
        return edit_xml(
            path=str(kwargs.get("path", "")).strip(),
            edits=kwargs.get("edits", []),
            validate_after=bool(kwargs.get("validate_after", True)),
        )

    if normalized in {"track_replace", "tracked_replace", "track_changes"}:
        return track_replace(
            path=str(kwargs.get("path", "")).strip(),
            old_text=str(kwargs.get("old_text", "")),
            new_text=str(kwargs.get("new_text", "")),
            author=str(kwargs.get("author", "TALOS")).strip() or "TALOS",
        )

    if normalized in {"set_page_size_dxa", "page_size"}:
        return set_page_size_dxa(
            path=str(kwargs.get("path", "")).strip(),
            width_dxa=kwargs.get("width_dxa", 12240),
            height_dxa=kwargs.get("height_dxa", 15840),
        )

    if normalized in {"set_table_widths_dxa", "table_widths"}:
        return set_table_widths_dxa(
            path=str(kwargs.get("path", "")).strip(),
            width_dxa=kwargs.get("width_dxa", 9600),
        )

    if normalized in {"normalize_text", "normalize"}:
        return normalize_text(
            path=str(kwargs.get("path", "")).strip(),
            no_unicode_bullets=bool(kwargs.get("no_unicode_bullets", True)),
            smart_quotes_entities=bool(kwargs.get("smart_quotes_entities", True)),
            xml_paths=kwargs.get("xml_paths") if isinstance(kwargs.get("xml_paths"), list) else None,
        )

    if normalized in {"validate_xml", "validate"}:
        return validate_xml(path=str(kwargs.get("path", "")).strip())

    return _error(
        f"Unsupported DOCX action: {action}",
        hint=(
            "Supported actions: create_with_docx_js, edit_xml, track_replace, set_page_size_dxa, "
            "set_table_widths_dxa, normalize_text, validate_xml"
        ),
    )

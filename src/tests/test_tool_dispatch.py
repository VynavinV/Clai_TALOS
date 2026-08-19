"""Regression tests for bugs the system-check probes uncovered.

The tool dispatcher in AI.py forwards every optional argument explicitly:

    docx_tools.execute(action=..., page_width_dxa=tool_args.get("page_width_dxa"), ...)

so an argument the model omitted arrives as an explicit None. `kwargs.get(key,
default)` then returns None rather than the default, and the None reaches code
that expects a number. Both document tools crashed with a raw TypeError on
perfectly ordinary calls.
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import docx_tools
import spreadsheet_tools


def _writable_root() -> str:
    """The file tools refuse to touch system directories, and on macOS the
    default temp dir resolves under /private/var, which is one of them."""
    return str(Path(__file__).resolve().parents[1])


def test_opt_prefers_default_over_explicit_none():
    assert spreadsheet_tools._opt({"max_rows": None}, "max_rows", 200) == 200
    assert docx_tools._opt({"page_width_dxa": None}, "page_width_dxa", 12240) == 12240


def test_opt_keeps_a_real_value():
    assert spreadsheet_tools._opt({"max_rows": 5}, "max_rows", 200) == 5
    assert docx_tools._opt({"page_width_dxa": 100}, "page_width_dxa", 12240) == 100


def test_opt_falls_back_when_key_is_absent():
    assert spreadsheet_tools._opt({}, "max_rows", 200) == 200


def test_opt_preserves_falsy_values_that_are_not_none():
    # 0 and "" are real choices, not "unset".
    assert spreadsheet_tools._opt({"max_rows": 0}, "max_rows", 200) == 0
    assert docx_tools._opt({"title": ""}, "title", "fallback") == ""


def test_spreadsheet_read_survives_the_dispatchers_none_arguments():
    """Reproduces the exact call AI.py makes when the model omits optional args."""
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        return  # nothing to assert without the dependency

    with tempfile.TemporaryDirectory(dir=_writable_root()) as tmp:
        path = os.path.join(tmp, "sheet.xlsx")
        created = spreadsheet_tools.execute(
            action="edit", path=path, create_if_missing=True,
            operations=[{"action": "append_row", "sheet_name": "Sheet1", "values": ["probe", 42]}],
            sheet_name=None, max_rows=None, max_cols=None,
        )
        assert created.get("ok"), created

        result = spreadsheet_tools.execute(
            action="read", path=path, sheet_name=None, max_rows=None, max_cols=None,
        )
        # Previously: TypeError: int() argument must be ... not 'NoneType'
        assert result.get("ok"), result


def test_docx_create_survives_the_dispatchers_none_arguments():
    with tempfile.TemporaryDirectory(dir=_writable_root()) as tmp:
        path = os.path.join(tmp, "doc.docx")
        result = docx_tools.execute(
            action="create", path=path, title="Probe", paragraphs=["body"],
            page_width_dxa=None, page_height_dxa=None, table_rows=None,
        )
        # The call must not raise. Node may legitimately be missing, in which
        # case a structured error is the correct outcome.
        assert isinstance(result, dict)
        if not result.get("ok"):
            assert "docx" in str(result.get("error", "")).lower() or \
                   "node" in str(result.get("error", "")).lower(), result


def test_docx_resolves_node_modules_from_the_project_root():
    """The Node helper runs from a temp dir, so NODE_PATH must point home.

    Without this the `require("docx")` search starts in /tmp and walks up
    outside the project, so the module is unresolvable no matter where it is
    installed — `npm install docx` in the project root could never fix it.
    """
    root = docx_tools._project_root()
    assert os.path.isdir(root)
    assert os.path.isfile(os.path.join(root, "docx_tools.py"))

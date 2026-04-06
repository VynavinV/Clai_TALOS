# Spreadsheet Execute (XLSX)

Advanced spreadsheet operations for .xlsx files.

## Why this tool exists

- Reads full workbook data with pandas
- Creates/edits while preserving formulas and rich formatting with openpyxl
- Recalculates formulas using LibreOffice via scripts/recalc.py
- Verifies there are zero formula errors (#REF!, #DIV/0!, etc.)
- Applies financial model color coding

## Action: read_with_pandas

Loads workbook data using pandas and returns preview-limited output.

Parameters:
- path (required)
- sheet_name (optional)
- max_rows (optional)
- max_cols (optional)

## Action: edit_with_openpyxl

Edits workbook with openpyxl to preserve complex formatting/formulas.

Parameters:
- path (required)
- operations (required): list of operations
- create_if_missing (optional)

Supported operation entries:
- ensure_sheet: {"action":"ensure_sheet", "sheet":"Model"}
- set_cell: {"action":"set_cell", "sheet":"Model", "cell":"B4", "value":123}
- set_formula: {"action":"set_formula", "sheet":"Model", "cell":"C4", "value":"=B4*1.2"}
- append_row: {"action":"append_row", "sheet":"Data", "values":["A", 1, 2]}

## Action: recalculate_with_libreoffice

Runs scripts/recalc.py to force a LibreOffice headless open/save cycle.

Parameters:
- path (required)
- output_path (optional)
- timeout_s (optional)
- script_path (optional)

## Action: verify_formula_errors

Scans for formula error tokens and error cells.

Parameters:
- path (required)
- sheet_name (optional)
- max_errors (optional)

Returns has_errors=false when no errors are found.

## Action: apply_financial_color_coding

Color legend:
- inputs: blue
- formulas: black
- external links: red

Parameters:
- path (required)
- sheet_name (optional)
- input_ranges (optional, list like ["B3:D40"])
- header_rows (optional)

## Notes

- This tool targets .xlsx workflows.
- Use verify_formula_errors after recalculation to confirm a clean model.

# DOCX Execute

Advanced DOCX operations with XML-level control.

## Why this tool exists

- Creates new documents with JavaScript docx library
- Edits existing docs by unpacking -> XML edit -> repacking
- Supports tracked changes tags (<w:del> and <w:ins>)
- Applies DXA page/table sizing controls
- Normalizes smart quotes with XML entities
- Validates XML structure after edits

## Action: create_with_docx_js

Creates a DOCX using Node.js + docx library.

Parameters:
- path (required)
- title (optional)
- paragraphs (optional array)
- table_rows (optional array of arrays)
- page_width_dxa (optional)
- page_height_dxa (optional)

Requirements:
- Node.js installed
- npm package docx installed in project root: npm install docx

## Action: edit_xml

Performs direct XML string replacements inside DOCX package files.

Parameters:
- path (required)
- edits (required)
  - xml_path (optional, default word/document.xml)
  - find
  - replace
  - replace_all (optional)
- validate_after (optional)

## Action: track_replace

Applies tracked changes wrappers for one text replacement.

Parameters:
- path (required)
- old_text (required)
- new_text (required)
- author (optional)

Behavior:
- deletion wrapped in w:del
- insertion wrapped in w:ins

## Action: set_page_size_dxa

Sets page size using DXA units.

Parameters:
- path (required)
- width_dxa (required)
- height_dxa (required)

## Action: set_table_widths_dxa

Sets table widths and cell widths using DXA units.

Parameters:
- path (required)
- width_dxa (required)

## Action: normalize_text

Normalizes text nodes for:
- no unicode bullets (converted to hyphen)
- smart quote entities (apostrophes/quotes)

Parameters:
- path (required)
- no_unicode_bullets (optional)
- smart_quotes_entities (optional)
- xml_paths (optional)

## Action: validate_xml

Validates all XML files inside the DOCX zip package.

Parameters:
- path (required)

## Notes

- This tool is for .docx only.
- Always run validate_xml after structural edits.

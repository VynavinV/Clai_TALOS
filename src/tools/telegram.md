# telegram

Use Telegram delivery tools for user-facing updates and media.

## send_telegram_message

Send a plain text message to the user.

Parameters:
- `message` (string, required)

## send_voice_message

Send a voice message using text-to-speech.

Parameters:
- `text` (string, required, max 500 chars)

## send_telegram_photo

Send an existing local image file to Telegram.

Parameters:
- `path` (string, required): Absolute or project-relative image path
- `caption` (string, optional)

## send_telegram_document

Send any file (PDF, XLSX, DOCX, CSV, etc.) as a Telegram document attachment.

Parameters:
- `path` (string, required): Absolute or project-relative file path
- `caption` (string, optional)

Use this instead of `send_telegram_photo` for non-image files (spreadsheets, documents, archives, etc.). Telegram will show the file as a downloadable document.

## send_telegram_screenshot

Capture and send a screenshot in one step.

Parameters:
- `source` (string, optional): `browser` (default) or `screen`
- `caption` (string, optional)
- `path` (string, optional): Output image path
- `full_page` (boolean, browser only)
- `page_index` (number, browser only)

Notes:
- `source=browser` uses the active browser automation session.
- `source=screen` captures the desktop screen via OS-native tools.

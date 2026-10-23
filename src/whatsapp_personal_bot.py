"""
WhatsApp personal account integration for Clai-TALOS.
Uses Playwright to automate WhatsApp Web — the bot acts as the user,
reads their messages, and replies on their behalf.
Requires QR code authentication (like WhatsApp Web/Desktop).
"""
import os
import logging
import asyncio
import hashlib
import json
import base64
import core
import db
import app_paths

logger = logging.getLogger("talos/whatsapp_personal")

WHATSAPP_PERSONAL_ENABLED = os.getenv("WHATSAPP_PERSONAL_ENABLED", "false").lower() in (
    "true", "1", "yes"
)
WHATSAPP_HEADLESS = os.getenv("WHATSAPP_PERSONAL_HEADLESS", "true").lower() in (
    "true", "1", "yes"
)

_session_dir = None
_task = None
_qr_image_b64 = None  # base64-encoded PNG of the current QR code
_qr_status = "idle"  # idle | waiting_for_qr | waiting_for_scan | connected | error


def _session_dir_path() -> str:
    return app_paths.data_path("whatsapp_session")


def _storage_state_path() -> str:
    return os.path.join(_session_dir_path(), "storage_state.json")


def _phone_to_user_id(phone: str) -> int:
    h = hashlib.sha256(f"wa:{phone}".encode()).digest()
    return int.from_bytes(h[:8], "big")


def is_configured() -> bool:
    return WHATSAPP_PERSONAL_ENABLED


def get_qr_status() -> dict:
    """Return current QR status and image for the settings UI."""
    return {"status": _qr_status, "qr_image": _qr_image_b64}


def start():
    """Start WhatsApp personal mode as a background task."""
    global _task
    if _task is not None:
        return
    if not is_configured():
        logger.info("WhatsApp personal mode not enabled")
        return
    _task = asyncio.create_task(_run_loop())


async def _run_loop():
    """Main loop with auto-reconnect."""
    while True:
        try:
            await _run_session()
        except Exception:
            logger.exception("WhatsApp personal session crashed")
        logger.info("Reconnecting WhatsApp personal in 10s...")
        await asyncio.sleep(10)


async def _run_session():
    global _qr_image_b64, _qr_status
    from playwright.async_api import async_playwright

    session_dir = _session_dir_path()
    os.makedirs(session_dir, exist_ok=True)
    storage_path = _storage_state_path()
    has_saved_session = os.path.exists(storage_path)

    async with async_playwright() as pw:
        # First run needs headed for QR scan; subsequent runs can be headless
        headless = WHATSAPP_HEADLESS if has_saved_session else False

        browser = await pw.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )

        if has_saved_session:
            try:
                with open(storage_path, "r") as f:
                    state = json.load(f)
                await context.add_cookies(state.get("cookies", []))
                logger.info("Loaded saved WhatsApp session")
            except Exception:
                logger.warning("Could not load saved session, will need QR scan")

        page = await context.new_page()
        await page.goto("https://web.whatsapp.com", wait_until="domcontentloaded")

        # Wait for either QR code or main app (already logged in)
        try:
            await page.wait_for_selector(
                '[data-testid="qr-code"], [data-testid="main"], div[data-ref]',
                timeout=60_000,
            )
        except Exception:
            logger.error("WhatsApp Web did not load in time")
            _qr_status = "error"
            _qr_image_b64 = None
            await browser.close()
            return

        # Check if we're already logged in
        is_logged_in = await page.query_selector('[data-testid="main"]') is not None

        if not is_logged_in:
            logger.info("WhatsApp QR code displayed — waiting for scan...")
            _qr_status = "waiting_for_qr"
            # Capture the QR code element as a screenshot
            try:
                qr_el = await page.query_selector(
                    '[data-testid="qr-code"], div[data-ref]'
                )
                if qr_el:
                    png_bytes = await qr_el.screenshot()
                    _qr_image_b64 = base64.b64encode(png_bytes).decode("ascii")
                    _qr_status = "waiting_for_scan"
                else:
                    # Fallback: screenshot the whole page
                    png_bytes = await page.screenshot()
                    _qr_image_b64 = base64.b64encode(png_bytes).decode("ascii")
                    _qr_status = "waiting_for_scan"
            except Exception:
                logger.warning("Could not capture QR code screenshot", exc_info=True)
                _qr_status = "waiting_for_scan"

            try:
                await page.wait_for_selector('[data-testid="main"]', timeout=120_000)
                logger.info("WhatsApp QR scanned successfully, logged in!")
                _qr_status = "connected"
                _qr_image_b64 = None
            except Exception:
                logger.error("QR scan timed out (2 minutes)")
                _qr_status = "error"
                _qr_image_b64 = None
                await browser.close()
                return

        # Save session state for next time
        await _save_session_state(context, storage_path)

        # Inject message listener
        await _inject_message_listener(page)

        # Start polling for new messages
        await _listen_for_messages(page)


async def _save_session_state(context, path: str):
    """Save browser cookies/storage for session persistence."""
    try:
        state = await context.storage_state()
        with open(path, "w") as f:
            json.dump(state, f)
        logger.info("WhatsApp session state saved")
    except Exception:
        logger.warning("Could not save session state", exc_info=True)


async def _inject_message_listener(page):
    """Inject JS MutationObserver to detect incoming messages."""
    await page.evaluate("""() => {
        if (window.__talos_observer) return;
        window.__talos_observer = true;
        window.__talos_new_messages = [];

        // Track which message elements we've already seen
        window.__talos_seen = new Set();

        // Initial scan of existing messages
        document.querySelectorAll('div[data-id]').forEach(el => {
            window.__talos_seen.add(el.getAttribute('data-id'));
        });

        const observer = new MutationObserver((mutations) => {
            for (const mutation of mutations) {
                for (const node of mutation.addedNodes) {
                    if (node.nodeType !== 1) continue;
                    const msgEl = node.querySelector
                        ? node.querySelector('div[data-id]') || (node.getAttribute && node.getAttribute('data-id') ? node : null)
                        : null;
                    if (!msgEl) continue;
                    const msgId = msgEl.getAttribute('data-id');
                    if (!msgId || window.__talos_seen.has(msgId)) continue;
                    window.__talos_seen.add(msgId);

                    // Only capture incoming messages (not sent by us)
                    const isOutgoing = msgEl.classList.contains('message-out')
                        || msgEl.querySelector('.message-out');
                    if (isOutgoing) continue;

                    // Extract text
                    const textEl = msgEl.querySelector(
                        '.copyable-text span[data-pre-plaintext], '
                        + '.copyable-text span[data-whatsapp-content], '
                        + 'span.selectable-text'
                    );
                    const text = textEl ? textEl.textContent.trim() : '';
                    if (!text) continue;

                    window.__talos_new_messages.push({
                        id: msgId,
                        text: text,
                        timestamp: Date.now()
                    });
                }
            }
        });

        // Observe the main chat pane
        const chatPane = document.querySelector('#main') || document.querySelector('[data-testid="conversation-panel"]');
        if (chatPane) {
            observer.observe(chatPane, { childList: true, subtree: true });
        }
    }""")


async def _listen_for_messages(page):
    """Poll for new messages detected by the JS observer."""
    current_chat_id = None

    while True:
        try:
            # Get current chat phone number / chat ID from URL or DOM
            chat_id = await page.evaluate("""() => {
                // Try to get chat ID from the chat header
                const header = document.querySelector('#main header span[dir="auto"]');
                if (header) return header.textContent;
                // Fallback: from URL
                const url = window.location.href;
                const match = url.match(/#chat=(.+)/);
                return match ? match[1] : null;
            }""")

            # Check for new messages
            new_msgs = await page.evaluate("""() => {
                const msgs = window.__talos_new_messages || [];
                window.__talos_new_messages = [];
                return msgs;
            }""")

            for msg in new_msgs:
                text = msg.get("text", "")
                if not text:
                    continue

                # Use chat_id or phone as identifier
                identifier = chat_id or "unknown"
                user_id = _phone_to_user_id(identifier)

                # Ensure user exists
                if not db.has_user_profile(user_id):
                    db.upsert_user_profile(user_id, identifier, f"WhatsApp personal: {identifier}")

                send_func = _make_send_func(page, identifier)

                try:
                    await core.process_message(user_id, text, send_func)
                except Exception:
                    logger.exception("Error processing WhatsApp personal message")

        except Exception:
            logger.exception("Error in WhatsApp message listener")

        await asyncio.sleep(1)


def _make_send_func(page, chat_id: str):
    """Create send_func matching core.process_message interface."""
    async def send_func(
        msg: str = "",
        voice: bool = False,
        photo_path: str | None = None,
        caption: str = "",
        stream: bool = False,
    ) -> None:
        if msg:
            try:
                await _send_message(page, chat_id, msg)
            except Exception:
                logger.exception("Failed to send WhatsApp message")

    return send_func


async def _send_message(page, chat_id: str, text: str):
    """Send a message in the current WhatsApp Web chat."""
    # Find the message input box
    input_box = await page.query_selector(
        'div[contenteditable="true"][data-tab="10"], '
        + 'footer div[contenteditable="true"], '
        + 'div[title="Type a message"]'
    )
    if not input_box:
        logger.error("Could not find WhatsApp message input box")
        return

    await input_box.click()
    # Clear existing text
    await page.keyboard.press("Control+a")
    await page.keyboard.press("Backspace")
    # Type the message
    await input_box.type(text, delay=20)
    # Send
    await page.keyboard.press("Enter")
    logger.info(f"Sent WhatsApp message to {chat_id}")

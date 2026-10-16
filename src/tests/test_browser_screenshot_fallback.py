"""Regression test: screenshot fallback for pages with perpetual animations.

A page running non-stop CSS animations never settles, so Playwright's
page.screenshot() times out. The screenshot step now catches the error,
makes the page visually static (animations snapped to their END state,
never paused mid-frame), and retries the capture via raw CDP
Page.captureScreenshot.

History: the original fallback used `animation-play-state: paused`, which
parks elements at their current keyframe — entrance animations caught at
opacity:0 stayed invisible, producing BLANK screenshots, and the injected
style persisted so every later capture stayed white too. The current
implementation uses `animation: none` (end state), cleans up stale tags,
and flags blank-looking captures with a warning.

This test simulates a page whose screenshot() always fails and verifies:
1. The CDP fallback produces a valid PNG on disk.
2. The injected CSS snaps animations to their end state — never pauses.
3. Stale tags from previous fallbacks are removed (idempotency).
4. Blank-looking captures are flagged with a warning in the result.
"""

import base64
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import browser_automation


def _png_from_pixels(dark_ratio: float) -> bytes:
    """Build a small PNG whose dark-pixel ratio is exactly dark_ratio."""
    try:
        from PIL import Image
    except ImportError:
        return b""
    w = h = 100
    total = w * h
    dark_count = int(total * dark_ratio)
    pixels = [0] * dark_count + [255] * (total - dark_count)
    img = Image.new("L", (w, h))
    img.putdata(pixels)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class FakeCDPSession:
    def __init__(self, png_bytes: bytes):
        self.sent: list[tuple[str, dict]] = []
        self._png = png_bytes

    async def send(self, method: str, params: dict) -> dict:
        self.sent.append((method, params))
        return {"data": base64.b64encode(self._png).decode("ascii")}


class FakeContext:
    def __init__(self, cdp: FakeCDPSession):
        self.cdp = cdp

    async def new_cdp_session(self, page):
        return self.cdp


class FakeHandle:
    async def evaluate(self, script: str):
        pass


class FakePage:
    """Page whose screenshot() always raises, mimicking a never-settling page."""

    def __init__(self, png_bytes: bytes):
        self.context = FakeContext(FakeCDPSession(png_bytes))
        self.styles: list[str] = []
        self.eval_scripts: list[str] = []
        self.removed_styles: list[str] = []
        self.url = "https://example.test/animated"

    async def screenshot(self, path=None, full_page=False, **kwargs):
        raise browser_automation.PlaywrightError("Timeout 15000ms exceeded")

    async def evaluate(self, script: str, arg=None):
        self.eval_scripts.append(script)
        if "querySelectorAll('style')" in script and "remove()" in script:
            return len(self.removed_styles)
        return None

    async def add_style_tag(self, content=""):
        self.styles.append(content)
        return FakeHandle()


async def _run_step(page, step):
    session = browser_automation.BrowserSession(
        playwright=None, browser=None, context=page.context, page=page, endpoint="fake"
    )
    return await browser_automation._execute_step(session, page, step, 15000)


def test_screenshot_falls_back_to_cdp_capture(tmp_path):
    import asyncio

    page = FakePage(_png_from_pixels(0.02))  # 2% dark = content present
    out = str(tmp_path / "shot.png")

    async def run():
        _, result = await _run_step(page, {"action": "screenshot", "path": out})
        return result

    result = asyncio.run(run())

    # Fallback marker is reported
    assert result.get("fallback") == "animations_stilled_cdp_capture", result
    assert result.get("path") == out
    assert Path(out).exists() and Path(out).stat().st_size > 0
    # CDP capture was used
    assert page.context.cdp.sent[0][0] == "Page.captureScreenshot"
    # The still-style snaps animations to their end state — pausing is banned.
    css = page.styles[0]
    assert "animation: none" in css
    assert "animation-play-state: paused" not in css
    # Stale-tag cleanup ran (idempotency guard)
    assert any("data-talos-still" in s for s in page.eval_scripts)
    # Content present → no blank warning
    assert "warning" not in result


def test_screenshot_blank_capture_flagged(tmp_path):
    import asyncio

    page = FakePage(_png_from_pixels(0.0005))  # 0.05% dark = blank
    out = str(tmp_path / "blank.png")

    async def run():
        _, result = await _run_step(page, {"action": "screenshot", "path": out})
        return result

    result = asyncio.run(run())

    # Blank capture is still saved, but flagged loudly
    assert result.get("warning"), result
    assert "blank" in result["warning"]
    assert result.get("fallback") == "animations_stilled_cdp_capture"


def test_stale_freeze_tags_removed_before_injection(tmp_path):
    import asyncio

    page = FakePage(_png_from_pixels(0.02))
    page.removed_styles = ["old-paused-tag", "old-talos-tag"]
    out = str(tmp_path / "shot2.png")

    async def run():
        _, result = await _run_step(page, {"action": "screenshot", "path": out})
        return result

    result = asyncio.run(run())
    assert result.get("still_status") == {"stale_styles_removed": 2}, result

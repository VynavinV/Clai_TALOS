"""Regression test: screenshot fallback for pages with perpetual animations.

A page running non-stop CSS animations never settles, so Playwright's
page.screenshot() times out. The screenshot step now catches the error,
injects animation-freezing CSS, and retries the capture via a raw CDP
Page.captureScreenshot call. This test simulates a page whose screenshot()
always fails and verifies the CDP fallback produces a valid PNG on disk.
"""

import base64
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import browser_automation


class FakeCDPSession:
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []

    async def send(self, method: str, params: dict) -> dict:
        self.sent.append((method, params))
        # 1x1 transparent PNG
        data = base64.b64encode(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
            b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
            b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
            b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        ).decode("ascii")
        return {"data": data}


class FakeContext:
    def __init__(self):
        self.cdp = FakeCDPSession()
        self.sessions: list = []

    async def new_cdp_session(self, page):
        self.sessions.append(page)
        return self.cdp


class FakePage:
    """Page whose screenshot() always raises, mimicking a never-settling page."""

    def __init__(self):
        self.context = FakeContext()
        self.styles: list[str] = []
        self.url = "https://example.test/animated"

    async def screenshot(self, path=None, full_page=False, **kwargs):
        raise browser_automation.PlaywrightError("Timeout 15000ms exceeded")

    async def add_style_tag(self, content=""):
        self.styles.append(content)


async def _run_step(page, step):
    session = browser_automation.BrowserSession(
        playwright=None, browser=None, context=page.context, page=page, endpoint="fake"
    )
    return await browser_automation._execute_step(session, page, step, 15000)


def test_screenshot_falls_back_to_cdp_capture(tmp_path):
    import asyncio

    page = FakePage()
    out = str(tmp_path / "shot.png")

    async def run():
        _, result = await _run_step(page, {"action": "screenshot", "path": out})
        return result

    result = asyncio.run(run())

    # Fallback marker is reported
    assert result.get("fallback") == "animations_frozen_cdp_capture"
    # A valid PNG was written (magic bytes intact)
    data = Path(out).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    # Animation-freeze CSS was injected before capture
    assert page.styles, "expected freeze CSS injection"
    assert "animation-play-state: paused" in page.styles[0]
    # CDP screenshot was requested with PNG format
    assert page.context.cdp.sent
    method, params = page.context.cdp.sent[0]
    assert method == "Page.captureScreenshot"
    assert params.get("format") == "png"


def test_screenshot_fallback_full_page_sets_cdp_flag(tmp_path):
    import asyncio

    page = FakePage()
    out = str(tmp_path / "full.png")

    async def run():
        _, result = await _run_step(page, {"action": "screenshot", "path": out, "full_page": True})
        return result

    result = asyncio.run(run())

    assert result.get("fallback") == "animations_frozen_cdp_capture"
    _, params = page.context.cdp.sent[0]
    assert params.get("captureBeyondViewport") is True


if __name__ == "__main__":
    import asyncio
    import tempfile

    failures = 0
    for name, fn in [("fallback_capture", test_screenshot_falls_back_to_cdp_capture),
                     ("full_page_flag", test_screenshot_fallback_full_page_sets_cdp_flag)]:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                fn(Path(tmp))
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)

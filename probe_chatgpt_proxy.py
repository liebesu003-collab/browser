from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REDIRECT_URI = "http://localhost:1455/auth/callback"
DEFAULT_LOGIN_URL = "https://auth.openai.com/oauth/authorize?" + urlencode(
    {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": "openid email profile offline_access",
        "state": "probe-state",
        "code_challenge": "A" * 43,
        "code_challenge_method": "S256",
        "prompt": "login",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
    }
)
DEFAULT_TIMEOUT_MS = 20000
DEFAULT_READY_TIMEOUT_MS = 30000
DEFAULT_CHALLENGE_GRACE_MS = 20000
DEFAULT_WAIT_AFTER_MS = 3000
LOGIN_READY_SELECTORS = [
    'input[type="email"]',
    'input[name="email"]',
    'button:has-text("Continue")',
    'button:has-text("Sign up")',
    'a:has-text("Sign up")',
]


def sanitize_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return cleaned.strip("._") or "proxy"


def classify_human_verification(signals: list[str]) -> str | None:
    normalized = " ".join(part.strip().lower() for part in signals if part and part.strip())
    if not normalized:
        return None

    verify_markers = (
        "verify you are human",
        "checking if the site connection is secure",
        "please stand by, while we are checking your browser",
        "security check to access",
        "complete the security check to access",
        "just a moment",
        "checking your browser",
    )
    cloudflare_markers = (
        "cloudflare",
        "turnstile",
        "challenges.cloudflare.com",
        "challenge-platform",
        "__cf_chl",
        "cf-challenge",
        "/cdn-cgi/challenge-platform/",
    )

    has_verify_marker = any(marker in normalized for marker in verify_markers)
    has_cloudflare_marker = any(marker in normalized for marker in cloudflare_markers)

    if has_cloudflare_marker and has_verify_marker:
        return "Cloudflare human verification page is blocking the login form"
    if "turnstile" in normalized or "challenges.cloudflare.com" in normalized:
        return "Cloudflare Turnstile challenge is blocking the login form"
    if "captcha" in normalized and "human" in normalized:
        return "A human verification challenge is blocking the login form"
    return None


async def page_body_safe(page, limit: int = 2000) -> str:
    try:
        body = await page.locator("body").inner_text(timeout=1500)
    except Exception:
        return ""
    return body[:limit].strip()


async def page_title_safe(page) -> str:
    try:
        return (await page.title()).strip()
    except Exception:
        return ""


async def gather_probe_result(page, tag: str) -> dict[str, Any]:
    title = await page_title_safe(page)
    body = await page_body_safe(page)
    frame_urls: list[str] = []
    iframe_sources: list[str] = []
    selector_counts: dict[str, int] = {}

    try:
        frame_urls = [frame.url for frame in page.frames if frame.url]
    except Exception:
        frame_urls = []

    try:
        iframe_sources = await page.locator("iframe").evaluate_all(
            """els => els
                .map((el) => el.getAttribute("src") || el.src || "")
                .filter(Boolean)"""
        )
    except Exception:
        iframe_sources = []

    for selector in LOGIN_READY_SELECTORS:
        try:
            selector_counts[selector] = await page.locator(selector).count()
        except Exception:
            selector_counts[selector] = 0

    signals = [page.url, title, body, *frame_urls, *iframe_sources]
    blocked_reason = classify_human_verification(signals)
    if "/api/auth/error" in page.url:
        blocked_reason = blocked_reason or "ChatGPT redirected to /api/auth/error"

    return {
        "tag": tag,
        "url": page.url,
        "title": title,
        "body_excerpt": body,
        "frame_urls": frame_urls,
        "iframe_sources": iframe_sources,
        "selector_counts": selector_counts,
        "blocked_reason": blocked_reason,
    }


async def wait_for_login_ready(
    page,
    timeout_ms: int,
    tag: str,
    challenge_grace_ms: int,
) -> dict[str, Any]:
    end = time.monotonic() + timeout_ms / 1000
    challenge_deadline: float | None = None
    transient_blocked_reason: str | None = None
    last_result = await gather_probe_result(page, tag)

    while time.monotonic() < end:
        last_result = await gather_probe_result(page, tag)

        if any(count > 0 for count in last_result["selector_counts"].values()):
            last_result["ready"] = True
            last_result["ready_reason"] = "login controls detected"
            if transient_blocked_reason:
                last_result["transient_blocked_reason"] = transient_blocked_reason
            return last_result

        blocked_reason = last_result.get("blocked_reason")
        if blocked_reason:
            transient_blocked_reason = transient_blocked_reason or blocked_reason
            if challenge_deadline is None:
                challenge_deadline = time.monotonic() + challenge_grace_ms / 1000
            elif time.monotonic() >= challenge_deadline:
                last_result["ready"] = False
                last_result["ready_reason"] = blocked_reason
                last_result["transient_blocked_reason"] = transient_blocked_reason
                return last_result
        else:
            challenge_deadline = None

        await page.wait_for_timeout(500)

    last_result["ready"] = False
    if last_result.get("blocked_reason"):
        last_result["ready_reason"] = last_result["blocked_reason"]
    elif transient_blocked_reason:
        last_result["ready_reason"] = "login controls not detected before timeout after transient human verification"
        last_result["transient_blocked_reason"] = transient_blocked_reason
    else:
        last_result["ready_reason"] = "login controls not detected before timeout"
    return last_result


async def run_probe(args: argparse.Namespace) -> int:
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright.async_api import async_playwright

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_tag = sanitize_filename(args.tag)
    result_path = output_dir / f"proxy_probe_{safe_tag}.json"
    screenshot_path = output_dir / f"proxy_probe_{safe_tag}.png"

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            proxy={"server": args.proxy} if args.proxy else None,
            args=["--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            ignore_https_errors=True,
            locale="en-US",
            timezone_id="America/New_York",
            viewport={"width": 1365, "height": 900},
        )
        page = await context.new_page()

        try:
            try:
                await page.goto(args.url, wait_until="domcontentloaded", timeout=args.timeout_ms)
            except PlaywrightTimeoutError:
                pass

            await page.wait_for_timeout(args.wait_after_ms)
            result = await wait_for_login_ready(
                page,
                args.ready_timeout_ms,
                args.tag,
                args.challenge_grace_ms,
            )
            await page.screenshot(path=str(screenshot_path), full_page=True)
        finally:
            await context.close()
            await browser.close()

    result["screenshot"] = str(screenshot_path)
    result["ok"] = bool(result.get("ready")) and not result.get("blocked_reason")
    result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"probe tag={args.tag}")
    print(f"probe url={result['url']}")
    print(f"probe title={result['title']}")
    print(f"probe ready={result['ready']}")
    print(f"probe ready_reason={result['ready_reason']}")
    if result.get("blocked_reason"):
        print(f"probe blocked_reason={result['blocked_reason']}")
    if result.get("transient_blocked_reason"):
        print(f"probe transient_blocked_reason={result['transient_blocked_reason']}")
    print(f"probe result_json={result_path}")
    print(f"probe screenshot={screenshot_path}")

    return 0 if result["ok"] else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy", default="")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--url", default=DEFAULT_LOGIN_URL)
    parser.add_argument("--output-dir", default="logs")
    parser.add_argument("--timeout-ms", type=int, default=DEFAULT_TIMEOUT_MS)
    parser.add_argument("--ready-timeout-ms", type=int, default=DEFAULT_READY_TIMEOUT_MS)
    parser.add_argument("--challenge-grace-ms", type=int, default=DEFAULT_CHALLENGE_GRACE_MS)
    parser.add_argument("--wait-after-ms", type=int, default=DEFAULT_WAIT_AFTER_MS)
    args = parser.parse_args()
    return asyncio.run(run_probe(args))


if __name__ == "__main__":
    raise SystemExit(main())

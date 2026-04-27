import logging
import shutil
from dataclasses import dataclass
from urllib.parse import urlparse

import requests


logger = logging.getLogger("http_fetcher")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)


@dataclass
class FetchedPage:
    url: str
    content: bytes
    text: str
    status_code: int | None
    source: str


def build_browser_headers(url: str, referer: str | None = None) -> dict[str, str]:
    """Return browser-like headers for sites that block bare requests clients."""
    parsed_url = urlparse(url)
    origin = f"{parsed_url.scheme}://{parsed_url.netloc}/" if parsed_url.scheme and parsed_url.netloc else None

    return {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer or origin or url,
        "Upgrade-Insecure-Requests": "1",
    }


def _requests_fetch(url: str, timeout: int, referer: str | None = None) -> FetchedPage:
    response = requests.get(
        url,
        headers=build_browser_headers(url, referer=referer),
        timeout=timeout,
        allow_redirects=True,
    )
    return FetchedPage(
        url=response.url,
        content=response.content,
        text=response.text,
        status_code=response.status_code,
        source="requests",
    )


def _looks_like_block_page(text: str) -> bool:
    sample = text[:5000].lower()
    return (
        ("just a moment" in sample and "cloudflare" in sample)
        or "cf-browser-verification" in sample
        or "cf-challenge" in sample
        or "challenge-platform" in sample
    )


def _raise_http_error(url: str, status_code: int, content: bytes = b"") -> None:
    response = requests.Response()
    response.status_code = status_code
    response.url = url
    response._content = content
    response.raise_for_status()


def _playwright_fetch(url: str, timeout: int, referer: str | None = None) -> FetchedPage:
    from playwright.sync_api import sync_playwright

    timeout_ms = timeout * 1000
    with sync_playwright() as playwright:
        launch_kwargs = {
            "headless": True,
            "timeout": timeout_ms,
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        }
        try:
            browser = playwright.chromium.launch(**launch_kwargs)
        except Exception:
            chromium_path = next(
                (
                    path
                    for path in [
                        shutil.which("chromium"),
                        shutil.which("chromium-browser"),
                        shutil.which("google-chrome"),
                        shutil.which("google-chrome-stable"),
                    ]
                    if path
                ),
                None,
            )
            if not chromium_path:
                raise
            logger.info("Using system Chromium executable for Playwright fallback: %s", chromium_path)
            browser = playwright.chromium.launch(executable_path=chromium_path, **launch_kwargs)
        try:
            context = browser.new_context(
                user_agent=DEFAULT_USER_AGENT,
                locale="en-US",
                viewport={"width": 1440, "height": 1200},
                extra_http_headers=build_browser_headers(url, referer=referer),
            )
            page = context.new_page()
            page.set_default_timeout(timeout_ms)
            response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 5000))
            except Exception:
                pass

            html = page.content()
            status_code = response.status if response else None
            final_url = page.url
            context.close()
            return FetchedPage(
                url=final_url,
                content=html.encode("utf-8"),
                text=html,
                status_code=status_code,
                source="playwright",
            )
        finally:
            browser.close()


def fetch_page(
    url: str,
    timeout: int,
    referer: str | None = None,
    fallback_statuses: set[int] | None = None,
    force_browser: bool = False,
) -> FetchedPage:
    """
    Fetch a page with requests first, falling back to Playwright for anti-bot blocks.

    The fallback keeps normal sites cheap while giving WordPress/student newspaper
    sites a second chance when they reject datacenter-style HTTP clients.
    """
    fallback_statuses = fallback_statuses or {403, 429}
    original_error: Exception | None = None

    if not force_browser:
        try:
            fetched_page = _requests_fetch(url, timeout=timeout, referer=referer)
            is_block_page = _looks_like_block_page(fetched_page.text)
            if fetched_page.status_code not in fallback_statuses and not is_block_page:
                _raise_http_error(
                    fetched_page.url,
                    fetched_page.status_code or 0,
                    fetched_page.content,
                )
                return fetched_page

            logger.info(
                "Requests fetch returned HTTP %s for %s; trying Playwright fallback",
                fetched_page.status_code,
                url,
            )
            blocked_status_code = (
                fetched_page.status_code
                if fetched_page.status_code in fallback_statuses
                else 403
            )
            try:
                _raise_http_error(
                    fetched_page.url,
                    blocked_status_code or 403,
                    fetched_page.content,
                )
            except requests.exceptions.RequestException as exc:
                original_error = exc
        except requests.exceptions.RequestException as exc:
            original_error = exc
            logger.info("Requests fetch failed for %s; trying Playwright fallback: %s", url, exc)

    try:
        fetched_page = _playwright_fetch(url, timeout=timeout, referer=referer)
        if _looks_like_block_page(fetched_page.text):
            _raise_http_error(fetched_page.url, 403, fetched_page.content)
        if fetched_page.status_code and fetched_page.status_code >= 400:
            _raise_http_error(fetched_page.url, fetched_page.status_code, fetched_page.content)
        return fetched_page
    except Exception as exc:
        logger.info("Playwright fallback failed for %s: %s", url, exc)
        if original_error:
            raise original_error
        raise

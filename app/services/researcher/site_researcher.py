from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover - optional runtime dependency in local dev
    sync_playwright = None


TARGET_PAGES: list[tuple[str, str]] = [
    ("homepage", ""),
    ("contact", "contact"),
    ("contact_lt", "kontaktai"),
    ("contact_lt2", "susisiekti"),
    ("contact_lv", "kontakti"),
    ("contact_ee", "kontakt"),
    ("contact_pl", "kontakt"),
    ("about", "about"),
    ("about_lt", "apie-mus"),
    ("about_lt2", "apie"),
    ("services", "services"),
    ("services_lt", "paslaugos"),
    ("careers", "careers"),
    ("jobs", "jobs"),
    ("jobs_lt", "darbas"),
    ("jobs_lt2", "karjera"),
]


@dataclass
class PageSnapshot:
    url: str
    page_type: str
    text: str


@dataclass
class ResearchResult:
    domain: str
    pages: list[PageSnapshot]
    languages_found: list[str]
    has_careers_page: bool
    text_summary: str


def _summarize_text(chunks: list[str], max_chars: int = 5000) -> str:
    joined = "\n".join(x.strip() for x in chunks if x and x.strip())
    return joined[:max_chars]


def _research_with_playwright(base_url: str) -> list[PageSnapshot]:
    pages: list[PageSnapshot] = []
    assert sync_playwright is not None
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()

        for page_type, path in TARGET_PAGES:
            url = urljoin(base_url, path)
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=15000)
                if resp is None:
                    continue
                if resp.status >= 400:
                    continue
                text = page.locator("body").inner_text(timeout=5000)
                if text and text.strip():
                    pages.append(PageSnapshot(url=url, page_type=page_type, text=text[:12000]))
            except Exception:
                continue

        context.close()
        browser.close()
    return pages


def _extract_text_from_html(html: str) -> str:
    """Strip HTML tags and decode common entities to get readable text."""
    import re as _re
    # preserve mailto: links before stripping tags
    text = _re.sub(r'<a[^>]+href=["\']mailto:([^"\']+)["\'][^>]*>', r' \1 ', html)
    text = _re.sub(r'<[^>]+>', ' ', text)
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&nbsp;', ' ')
    text = _re.sub(r'\s+', ' ', text).strip()
    return text


def _research_with_httpx(base_url: str) -> list[PageSnapshot]:
    pages: list[PageSnapshot] = []
    seen_urls: set[str] = set()
    for page_type, path in TARGET_PAGES:
        url = urljoin(base_url, path)
        if url in seen_urls:
            continue
        try:
            r = httpx.get(url, timeout=12, follow_redirects=True)
            if r.status_code >= 400:
                continue
            final_url = str(r.url)
            if final_url in seen_urls:
                continue
            seen_urls.add(url)
            seen_urls.add(final_url)
            html = r.text
            text = _extract_text_from_html(html)
            if text and text.strip():
                pages.append(PageSnapshot(url=final_url, page_type=page_type, text=text[:12000]))
        except Exception:
            continue
    return pages


def research_company_site(domain: str) -> ResearchResult:
    base_url = f"https://{domain}/"
    pages = _research_with_playwright(base_url) if sync_playwright else _research_with_httpx(base_url)

    if not pages:
        fallback_url = f"http://{domain}/"
        pages = _research_with_playwright(fallback_url) if sync_playwright else _research_with_httpx(fallback_url)

    text_summary = _summarize_text([p.text for p in pages])
    low = text_summary.lower()
    langs: list[str] = []
    for marker in ["english", "deutsch", "lietuvi", "latvie", "eesti", "polski", "francais"]:
        if marker in low:
            langs.append(marker)

    has_careers = any(p.page_type in {"careers", "jobs"} for p in pages)

    return ResearchResult(
        domain=domain,
        pages=pages,
        languages_found=langs,
        has_careers_page=has_careers,
        text_summary=text_summary,
    )

"""Link unfurling (WP8): OpenGraph/Twitter-card metadata with a DB cache.

    await unfurl(url) -> {"url", "title", "description", "image_url"}

- Fetches at most UNFURL_MAX_BYTES (~256KB) of the page with a 10s timeout —
  OG tags live in <head>, no need for the whole document.
- Parses og:*/twitter:*/<title>/<meta name=description> via stdlib
  html.parser (no bs4 dependency).
- Successful fetches are cached in `unfurl_cache` (migration
  003_unfurl_cache.sql), TTL 7 days. Fetch failures are NOT cached, so a
  transient outage doesn't pin an empty card for a week.
- Never raises on garbage input/pages — degrades to {"url": url, ...Nones}.

Tests monkeypatch `fetch_head` to avoid real network I/O.
"""

import datetime
import logging
from html.parser import HTMLParser
from typing import Any, Optional
from urllib.parse import urljoin, urlsplit

import httpx

from .. import db

logger = logging.getLogger(__name__)

UNFURL_MAX_BYTES = 256 * 1024
FETCH_TIMEOUT = 10.0
CACHE_TTL = datetime.timedelta(days=7)
USER_AGENT = "Mozilla/5.0 (compatible; Disjorn/1.0; link unfurler)"


class _MetaParser(HTMLParser):
    """Collect og:/twitter: meta tags, <meta name=description>, and <title>."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title_parts: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            attr = dict(attrs)
            key = (attr.get("property") or attr.get("name") or "").strip().lower()
            content = (attr.get("content") or "").strip()
            if key and content and key not in self.meta:
                self.meta[key] = content

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)


def parse_meta(html: str, base_url: str) -> dict[str, Optional[str]]:
    """Extract title/description/image_url from HTML; never raises."""
    parser = _MetaParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 — garbage HTML must not break unfurling
        pass
    meta = parser.meta
    title = (
        meta.get("og:title")
        or meta.get("twitter:title")
        or " ".join("".join(parser.title_parts).split())
        or None
    )
    description = (
        meta.get("og:description")
        or meta.get("twitter:description")
        or meta.get("description")
        or None
    )
    image = meta.get("og:image") or meta.get("twitter:image") or None
    if image:
        image = urljoin(base_url, image)
    return {"title": title, "description": description, "image_url": image}


async def fetch_head(url: str) -> tuple[str, str]:
    """GET the first UNFURL_MAX_BYTES of a page. Returns (final_url, html).

    Raises httpx errors / ValueError on failure — unfurl() catches them.
    Monkeypatched in tests.
    """
    async with httpx.AsyncClient(
        timeout=FETCH_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                chunks.append(chunk)
                total += len(chunk)
                if total >= UNFURL_MAX_BYTES:
                    break
            body = b"".join(chunks)[:UNFURL_MAX_BYTES]
            encoding = resp.charset_encoding or "utf-8"
            return str(resp.url), body.decode(encoding, errors="replace")


def _minimal(url: str) -> dict[str, Any]:
    return {"url": url, "title": None, "description": None, "image_url": None}


def _cache_cutoff() -> str:
    """ISO timestamp CACHE_TTL ago; rows with fetched_at <= this are stale."""
    return (
        (datetime.datetime.now(datetime.timezone.utc) - CACHE_TTL)
        .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        + "Z"
    )


async def unfurl(url: str) -> dict[str, Any]:
    """Unfurl a URL, serving from the DB cache when fresh. Never raises."""
    if urlsplit(url).scheme not in ("http", "https"):
        return _minimal(url)

    row = await db.fetch_one("SELECT * FROM unfurl_cache WHERE url = ?", (url,))
    if row is not None and row["fetched_at"] > _cache_cutoff():
        return {
            "url": url,
            "title": row["title"],
            "description": row["description"],
            "image_url": row["image_url"],
        }

    try:
        final_url, html = await fetch_head(url)
        meta = parse_meta(html, final_url)
    except Exception as exc:  # noqa: BLE001 — unfurl must never throw
        logger.debug("unfurl failed for %s: %s", url, exc)
        return _minimal(url)

    await db.execute(
        """INSERT INTO unfurl_cache (url, title, description, image_url, fetched_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(url) DO UPDATE SET
               title = excluded.title,
               description = excluded.description,
               image_url = excluded.image_url,
               fetched_at = excluded.fetched_at""",
        (url, meta["title"], meta["description"], meta["image_url"], db.utc_now()),
    )
    return {"url": url, **meta}

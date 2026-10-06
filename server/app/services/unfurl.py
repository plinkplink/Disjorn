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
- YouTube video URLs read YouTube's oEmbed endpoint instead (the page itself
  carries no usable meta tags) and gain an `embed` field for the inline player.

Tests monkeypatch `fetch_head` to avoid real network I/O.
"""

import datetime
import json
import logging
import re
from html.parser import HTMLParser
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import httpx

from .. import db

logger = logging.getLogger(__name__)

UNFURL_MAX_BYTES = 256 * 1024
FETCH_TIMEOUT = 10.0
CACHE_TTL = datetime.timedelta(days=7)
USER_AGENT = "Mozilla/5.0 (compatible; Disjorn/1.0; link unfurler)"

YOUTUBE_HOSTS = frozenset(
    {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
)
YOUTUBE_PATH_PREFIXES = frozenset({"shorts", "live", "embed"})
YOUTUBE_OEMBED = "https://www.youtube.com/oembed"
_VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")
_START_RE = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?")


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


def _start_seconds(raw: str) -> Optional[int]:
    match = _START_RE.fullmatch(raw)
    if match is None:
        return None
    h, m, sec = (int(g or 0) for g in match.groups())
    return h * 3600 + m * 60 + sec or None


def youtube_embed(url: str) -> Optional[dict[str, Any]]:
    """The embed descriptor for a YouTube video URL, or None for anything else."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    segments = [seg for seg in parts.path.split("/") if seg]
    query = parse_qs(parts.query)
    video_id = ""
    if host == "youtu.be" and len(segments) == 1:
        video_id = segments[0]
    elif host in YOUTUBE_HOSTS:
        if segments == ["watch"]:
            video_id = query.get("v", [""])[0]
        elif len(segments) == 2 and segments[0] in YOUTUBE_PATH_PREFIXES:
            video_id = segments[1]
    if not _VIDEO_ID_RE.fullmatch(video_id):
        return None
    start = query.get("t") or query.get("start") or [""]
    return {
        "provider": "youtube",
        "video_id": video_id,
        "start_seconds": _start_seconds(start[0]),
    }


async def fetch_youtube_meta(video_id: str) -> dict[str, Optional[str]]:
    """Title, channel and thumbnail from oEmbed; raises like fetch_head on failure."""
    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    _, body = await fetch_head(
        f"{YOUTUBE_OEMBED}?{urlencode({'url': watch_url, 'format': 'json'})}"
    )
    data = json.loads(body)
    if not isinstance(data, dict) or not data.get("title"):
        raise ValueError("oEmbed answer has no title")
    return {
        "title": str(data["title"]),
        "description": str(data["author_name"]) if data.get("author_name") else None,
        "image_url": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
    }


def _minimal(url: str) -> dict[str, Any]:
    return {"url": url, "title": None, "description": None, "image_url": None}


def _cache_cutoff() -> str:
    """ISO timestamp CACHE_TTL ago; rows with fetched_at <= this are stale."""
    return (
        (datetime.datetime.now(datetime.timezone.utc) - CACHE_TTL)
        .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        + "Z"
    )


def _serves_from_cache(row: Any, embed: Optional[dict[str, Any]]) -> bool:
    if row is None or row["fetched_at"] <= _cache_cutoff():
        return False
    # An empty row for a recognised video is a past failure, not an answer.
    return embed is None or any(row[k] for k in ("title", "description", "image_url"))


async def unfurl(url: str) -> dict[str, Any]:
    """Unfurl a URL, serving from the DB cache when fresh. Never raises."""
    if urlsplit(url).scheme not in ("http", "https"):
        return _minimal(url)
    embed = youtube_embed(url)
    extra = {"embed": embed} if embed is not None else {}

    row = await db.fetch_one("SELECT * FROM unfurl_cache WHERE url = ?", (url,))
    if _serves_from_cache(row, embed):
        return {
            "url": url,
            "title": row["title"],
            "description": row["description"],
            "image_url": row["image_url"],
            **extra,
        }

    meta = None
    if embed is not None:
        try:
            meta = await fetch_youtube_meta(embed["video_id"])
        except Exception as exc:  # noqa: BLE001 — the generic scrape still gets a try
            logger.debug("oEmbed failed for %s: %s", url, exc)
    if meta is None:
        try:
            final_url, html = await fetch_head(url)
            meta = parse_meta(html, final_url)
        except Exception as exc:  # noqa: BLE001 — unfurl must never throw
            logger.debug("unfurl failed for %s: %s", url, exc)
            return {**_minimal(url), **extra}

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
    return {"url": url, **meta, **extra}

"""Page summarization (WP8) behind a pluggable Summarizer protocol.

`SUMMARIZE_ENGINE` keys into the ENGINES registry of zero-arg factories.
`get_summarizer()` returns None when no engine matches or the engine lacks its
credential; the router turns that into a 503.

Also hosts the page fetch (monkeypatched in tests), the main-text extraction
and the `summary_cache` table behind POST /summarize.
"""

import logging
import re
from html.parser import HTMLParser
from typing import Callable, Optional, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

from .. import db
from ..config import get_settings
from . import unfurl

logger = logging.getLogger(__name__)

PAGE_MAX_BYTES = 2 * 1024 * 1024
SUMMARIZE_TIMEOUT = 30.0
MAX_INPUT_CHARS = 16_000  # bounds per-summary cost and local-model context
ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
SUMMARY_MAX_TOKENS = 512

PROMPT_TEMPLATE = (
    "Summarize this page in 3-5 sentences. Reply with only the summary, "
    "no preamble.\n\nURL: {url}\n\nPage text:\n{text}"
)


class Summarizer(Protocol):
    async def summarize(self, text: str, url: str) -> str: ...


class OllamaSummarizer:
    """Non-streaming POST to {OLLAMA_URL}/api/generate with OLLAMA_MODEL.

    Raises httpx.HTTPError when Ollama is unreachable or errors — the router
    maps that to 503.
    """

    def __init__(self, base_url: Optional[str] = None, model: Optional[str] = None):
        settings = get_settings()
        self.base_url = (base_url or settings.OLLAMA_URL).rstrip("/")
        self.model = model or settings.OLLAMA_MODEL

    async def summarize(self, text: str, url: str) -> str:
        prompt = PROMPT_TEMPLATE.format(url=url, text=text[:MAX_INPUT_CHARS])
        async with httpx.AsyncClient(timeout=SUMMARIZE_TIMEOUT) as client:
            resp = await client.post(
                f"{self.base_url}/api/generate",
                json={"model": self.model, "prompt": prompt, "stream": False},
            )
            resp.raise_for_status()
            answer = str(resp.json().get("response", ""))
            # Thinking models (e.g. qwen3) prefix the answer with reasoning in
            # <think> tags — strip so summaries stay clean regardless of model.
            answer = re.sub(r"<think>.*?</think>", "", answer, flags=re.DOTALL)
            return answer.strip()


class AnthropicSummarizer:
    """Messages API call with ANTHROPIC_MODEL; raises httpx.HTTPError on failure."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        settings = get_settings()
        self.api_key = api_key or settings.ANTHROPIC_API_KEY
        self.model = model or settings.ANTHROPIC_MODEL

    async def summarize(self, text: str, url: str) -> str:
        prompt = PROMPT_TEMPLATE.format(url=url, text=text[:MAX_INPUT_CHARS])
        async with httpx.AsyncClient(timeout=SUMMARIZE_TIMEOUT) as client:
            resp = await client.post(
                ANTHROPIC_MESSAGES_URL,
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": ANTHROPIC_VERSION,
                },
                json={
                    "model": self.model,
                    "max_tokens": SUMMARY_MAX_TOKENS,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            if resp.is_error:
                logger.warning("anthropic summarize %s: %s",
                               resp.status_code, resp.text[:500])
            resp.raise_for_status()
            blocks = resp.json().get("content", [])
            return "".join(
                b.get("text", "") for b in blocks if b.get("type") == "text"
            ).strip()


def _anthropic() -> Optional[Summarizer]:
    return AnthropicSummarizer() if get_settings().ANTHROPIC_API_KEY else None


ENGINES: dict[str, Callable[[], Optional[Summarizer]]] = {
    "ollama": OllamaSummarizer,
    "anthropic": _anthropic,
}


def get_summarizer() -> Optional[Summarizer]:
    """Engine selected by SUMMARIZE_ENGINE; None if unknown/unconfigured."""
    factory = ENGINES.get(get_settings().SUMMARIZE_ENGINE)
    return factory() if factory is not None else None


# ---------------------------------------------------------------------------
# Page fetch + main-text extraction (used by POST /summarize)
# ---------------------------------------------------------------------------

class PageFetchError(Exception):
    """Page could not be fetched (network error, bad status, non-HTTP URL)."""


REFUSED = (
    "This link points at a private, loopback or tailnet address; "
    "only pages on the public internet are summarized."
)


class PageRefused(PageFetchError):
    def __init__(self) -> None:
        super().__init__(REFUSED)


async def fetch_page(url: str, transport: Optional[httpx.AsyncBaseTransport] = None) -> str:
    """Fetch up to PAGE_MAX_BYTES of a page as text. Raises PageFetchError."""
    if not url.startswith(("http://", "https://")):
        raise PageFetchError(f"unsupported URL scheme: {url!r}")
    try:
        _, html = await unfurl.fetch_head(url, transport, max_bytes=PAGE_MAX_BYTES)
    except unfurl.NonPublicAddress as exc:
        raise PageRefused() from exc
    except httpx.HTTPStatusError as exc:
        raise PageFetchError(f"the site answered HTTP {exc.response.status_code}") from exc
    except (httpx.HTTPError, httpx.InvalidURL, OSError, ValueError) as exc:
        raise PageFetchError(str(exc) or exc.__class__.__name__) from exc
    return html


def normalize_url(url: str) -> str:
    """Cache key: host case, default port and fragment never change the page."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    scheme = parts.scheme.lower()
    hostport = parts.netloc.rpartition("@")[2]
    hostport = hostport.lower().removesuffix({"http": ":80", "https": ":443"}.get(scheme, ""))
    return urlunsplit((scheme, hostport, parts.path or "/", parts.query, ""))


async def cached_summary(url: str, engine: str, model: str) -> Optional[str]:
    row = await db.fetch_one(
        "SELECT summary FROM summary_cache WHERE url = ? AND engine = ? AND model = ?"
        " AND created_at > strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)",
        (url, engine, model, f"-{get_settings().SUMMARIZE_CACHE_TTL} seconds"),
    )
    return row["summary"] if row is not None else None


async def store_summary(url: str, engine: str, model: str, summary: str) -> None:
    await db.execute(
        """INSERT INTO summary_cache (url, engine, model, summary, created_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(url, engine, model) DO UPDATE SET
               summary = excluded.summary, created_at = excluded.created_at""",
        (url, engine, model, summary, db.utc_now()),
    )


class _TextStripper(HTMLParser):
    """Crude fallback extractor: all text content minus script/style."""

    _SKIP = {"script", "style", "noscript", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self.parts.append(data.strip())


def extract_text(html: str) -> str:
    """Main text via trafilatura; fall back to a crude tag strip."""
    try:
        import trafilatura

        extracted = trafilatura.extract(html)
        if extracted and extracted.strip():
            return extracted.strip()
    except Exception:  # noqa: BLE001 — fall back below
        pass
    stripper = _TextStripper()
    try:
        stripper.feed(html)
        stripper.close()
    except Exception:  # noqa: BLE001
        pass
    return " ".join(stripper.parts)

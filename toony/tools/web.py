"""Web search and opening links."""

from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request

from ..log import get
from .proc import CommandError, any_of, launch, which
from .registry import ToolContext, tool

log = get("tools.web")

_UA = "Mozilla/5.0 (X11; Linux x86_64) Toony/0.1"
_RESULT = re.compile(
    r'<a rel="nofollow" class="result__a" href="([^"]+)">(.*?)</a>.*?'
    r'class="result__snippet"[^>]*>(.*?)</a>', re.S)


def _clean(markup: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", markup)).strip()


@tool(description="Search the web and return short result snippets. Use this for "
                  "current events, facts you are unsure about, prices, and news.",
      params={"query": {"type": "string"},
              "limit": {"type": "integer", "default": 5}},
      required=["query"])
def search_web(ctx: ToolContext, query: str, limit: int = 5) -> str:
    config = ctx.config
    if limit is None and config:
        limit = int(config.get("tools.web.max_results", 5))
    limit = max(1, min(10, int(limit or 5)))
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    request = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            body = response.read().decode("utf-8", "replace")
    except Exception as exc:
        log.warning("web search failed: %s", exc)
        return f"I could not reach the search engine ({exc.__class__.__name__})."

    results = []
    for href, title, snippet in _RESULT.findall(body)[:limit]:
        link = urllib.parse.unquote(href)
        # DuckDuckGo wraps results in a redirect; unwrap for readability.
        match = re.search(r"uddg=([^&]+)", link)
        if match:
            link = urllib.parse.unquote(match.group(1))
        results.append(f"{_clean(title)} — {_clean(snippet)} ({link})")
    if not results:
        return f"I found no results for {query}."
    return "\n".join(results)


@tool(description="Open a URL in the user's web browser.", risk="sensitive",
      params={"url": {"type": "string"}}, required=["url"])
def open_url(ctx: ToolContext, url: str) -> str:
    if not re.match(r"^https?://", url):
        url = "https://" + url
    host = urllib.parse.urlparse(url).netloc

    browser = ""
    if ctx.config:
        browser = str(ctx.config.get("tools.web.browser", "") or "")
    # Each opener in turn, because one being installed is no promise that it
    # works: xdg-open on a session with no default browser set exits non-zero
    # and opens nothing, and kde-open is right there.
    openers = [browser] if browser else []
    openers += [o for o in ("xdg-open", "kde-open6", "kde-open", "gio")
                if which(o)]
    if not openers:
        raise CommandError("nothing on this machine knows how to open a link")

    reasons = []
    for opener in openers:
        argv = [opener, "open", url] if opener.endswith("gio") else [opener, url]
        try:
            launch(argv, f"{host} in your browser")
            return f"Opened {host} in your browser."
        except CommandError as exc:
            log.info("%s could not open %s: %s", opener, host, exc)
            reasons.append(str(exc))
    raise CommandError(f"I could not open {host}. {reasons[-1]}")


@tool(description="Search the web in a browser window instead of reading results "
                  "aloud. Use when the user says 'search for X' and wants to look "
                  "at the page themselves.",
      risk="sensitive", params={"query": {"type": "string"}}, required=["query"])
def search_in_browser(ctx: ToolContext, query: str) -> str:
    return open_url(ctx, "https://duckduckgo.com/?q=" + urllib.parse.quote(query))

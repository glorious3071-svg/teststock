"""Lightweight web search for supplementing missing macro data."""

from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request

USER_AGENT = "Mozilla/5.0 (compatible; teststock-annual-direction/1.0)"


def web_search(query: str, *, max_results: int = 5) -> list[dict[str, str]]:
    """Search via DuckDuckGo Lite (no API key). Returns title/snippet/url."""
    data = urllib.parse.urlencode({"q": query, "b": "", "kl": "wt-wt"}).encode()
    req = urllib.request.Request(
        "https://lite.duckduckgo.com/lite/",
        data=data,
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return []

    return _parse_lite_html(body, max_results=max_results)


def _parse_lite_html(body: str, *, max_results: int) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    # Lite layout: result link in <a class="result-link"> or plain <a href="http...">
    rows = re.findall(
        r'<a[^>]+rel="nofollow"[^>]+href="(https?://[^"]+)"[^>]*>([^<]+)</a>',
        body,
    )
    snippets = re.findall(r'<td[^>]*class="result-snippet"[^>]*>(.*?)</td>', body, re.S)
    if not rows:
        rows = [(u, t) for u, t in re.findall(r'<a[^>]+href="(https?://[^"]+)"[^>]*>([^<]{4,120})</a>', body)]

    seen: set[str] = set()
    for i, (url, title) in enumerate(rows):
        if "duckduckgo.com" in url:
            continue
        title = _clean_html(title)
        if not title or url in seen:
            continue
        seen.add(url)
        snippet = _clean_html(snippets[i]) if i < len(snippets) else ""
        results.append({"title": title, "snippet": snippet, "url": url})
        if len(results) >= max_results:
            break
    return results


def _clean_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()

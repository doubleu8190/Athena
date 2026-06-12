"""Built-in web search tool.

Risk level: low (read-only).
Uses DuckDuckGo or configured search backend.
"""

from __future__ import annotations

import os
from typing import Any

from athena.logging_config import get_logger

logger = get_logger(__name__)


async def web_search(
    query: str,
    max_results: int = 5,
    preview: bool = False,
    **kwargs,
) -> dict[str, Any]:
    """Search the web for information.

    Risk level: low.

    Args:
        query: Search query string.
        max_results: Maximum number of results to return (default 5, max 10).
        preview: If True, return query analysis without executing.

    Returns:
        Dict with 'success', 'results', and 'query' fields.
    """
    if preview:
        return {
            "success": True,
            "preview": True,
            "query": query,
            "estimated_results": "N/A",
        }

    max_results = min(max_results, 10)

    # Use DuckDuckGo HTML search (no API key needed)
    try:
        import httpx

        # DuckDuckGo Lite search
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            resp = await client.get(
                "https://lite.duckduckgo.com/lite/",
                params={"q": query},
                headers={"User-Agent": "Athena/0.1.0"},
            )
            resp.raise_for_status()

            # Simple HTML extraction of results
            from html.parser import HTMLParser

            class DDGResultParser(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.results = []
                    self._current = {}
                    self._in_link = False
                    self._in_desc = False
                    self._link_href = ""

                def handle_starttag(self, tag, attrs):
                    attrs_dict = dict(attrs)
                    if tag == "a" and "class" in attrs_dict and "result-link" in attrs_dict.get("class", ""):
                        self._in_link = True
                        self._link_href = attrs_dict.get("href", "")
                    elif tag == "td" and "class" in attrs_dict and "result-snippet" in attrs_dict.get("class", ""):
                        self._in_desc = True

                def handle_data(self, data):
                    if self._in_link:
                        self._current["title"] = data.strip()
                    elif self._in_desc:
                        self._current["snippet"] = data.strip()

                def handle_endtag(self, tag):
                    if tag == "a" and self._in_link:
                        self._in_link = False
                        self._current["url"] = self._link_href
                    elif tag == "td" and self._in_desc:
                        self._in_desc = False
                        if self._current:
                            self.results.append(dict(self._current))
                            self._current = {}

            parser = DDGResultParser()
            parser.feed(resp.text)

            results = parser.results[:max_results]

            return {
                "success": True,
                "query": query,
                "results": results,
                "result_count": len(results),
                "source": "duckduckgo",
            }

    except Exception as e:
        logger.warning("web_search_error", query=query, error=str(e))
        # Fallback: return empty results
        return {
            "success": True,
            "query": query,
            "results": [],
            "result_count": 0,
            "source": "fallback",
            "warning": f"Search degraded: {e}",
        }

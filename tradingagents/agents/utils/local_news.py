"""Extractive local-news reports: publish only evidence present in the source."""

import json
import re


def prepare_news_sources(news: str) -> list[str]:
    text = news[:14000]
    articles = re.split(r"(?m)^### ", text)
    return articles[1:7] if len(articles) > 1 else [text]


def prepare_news_catalog(sources: list[str]) -> dict[int, dict[int, str]]:
    """Assign request-local IDs to literal excerpts, never to generated text."""
    catalog = {}
    for article, source in enumerate(sources, 1):
        excerpts = []
        for line in source.splitlines():
            line = line.strip()
            if not line or line.startswith(("Link:", "Source mode:", "## ", "http://", "https://")):
                continue
            # These are excerpts, not guaranteed full sentences (long text is chunked).
            for sentence in re.split(r"(?<=[.!?。！？])\s+", line):
                for offset in range(0, len(sentence), 800):
                    excerpt = sentence[offset:offset + 800].strip()
                    if len(excerpt) >= 10:
                        excerpts.append(excerpt)
        if excerpts:
            catalog[article] = dict(enumerate(excerpts[:24], 1))
    if not catalog:
        raise ValueError("Local news has no usable source excerpts.")
    return catalog


def render_selected_news(
    content: str, ticker: str, sources: list[str], catalog: dict[int, dict[int, str]],
) -> str:
    """Resolve IDs against the exact catalog sent in this call; ignore free text."""
    data = _parse_evidence(content, ticker)
    resolved = []
    seen = set()
    for item in data["items"]:
        if not isinstance(item, dict):
            raise ValueError("Invalid local news evidence item.")
        article, excerpt = item.get("article"), item.get("excerpt")
        if type(article) is not int or article not in catalog:
            raise ValueError("Local news referenced an unknown article.")
        if type(excerpt) is not int or excerpt not in catalog[article]:
            raise ValueError("Local news referenced an unknown excerpt in its article.")
        if (article, excerpt) in seen:
            raise ValueError("Local news selected a duplicate excerpt.")
        seen.add((article, excerpt))
        resolved.append({
            "article": article,
            "quote": catalog[article][excerpt],
            "signal": item.get("signal"),
        })
    # Retain the original source check as defense in depth. Only Python supplies quotes.
    return render_verified_news(json.dumps({"ticker": ticker, "items": resolved}), ticker, sources)


def _parse_evidence(content: str, ticker: str) -> dict:
    try:
        data = json.loads(content)
    except (ValueError, TypeError) as exc:
        raise ValueError("Local news must return valid evidence JSON.") from exc
    if not isinstance(data, dict) or data.get("ticker") != ticker:
        raise ValueError("Local news returned a different ticker.")
    items = data.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 6:
        raise ValueError("Local news must select 1 to 6 evidence items.")
    return data


def render_verified_news(content: str, ticker: str, sources: list[str]) -> str:
    """Legacy quote-format validator, also used for Python-resolved selections."""
    items = _parse_evidence(content, ticker)["items"]
    categories = {"catalyst": "Catalyst", "risk": "Risk", "uncertainty": "Uncertainty", "neutral": "Neutral"}
    lines = [f"## News evidence: {ticker}", "", "Local LLM classifications are interpretations; quoted excerpts below are source-verified, not independently fact-checked.", ""]
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Invalid local news evidence item.")
        index, quote, category = item.get("article"), item.get("quote"), item.get("signal")
        if type(index) is not int or not 1 <= index <= len(sources):
            raise ValueError("Local news referenced an unknown article.")
        if not isinstance(category, str) or category not in categories:
            raise ValueError("Local news returned an unknown classification.")
        if not isinstance(quote, str) or not 10 <= len(quote) <= 800:
            raise ValueError("Local news quote must contain 10 to 800 characters.")
        normalized = " ".join(quote.split())
        source = sources[index - 1]
        if normalized not in " ".join(source.split()):
            raise ValueError("Local news generated a claim absent from its cited excerpt.")
        # Source labels and URLs come from the input, never from model output.
        lines.extend([f"### {categories[category]} (article {index})", "> " + "\n> ".join(quote.splitlines())])
        heading = source.splitlines()[0] if source.splitlines() else ""
        lines.append(f"Source: {heading[:400]}")
        for link in re.findall(r"https?://[^\s<>]+", source)[:2]:
            lines.append(f"Link: {link}")
        lines.append("")
    return "\n".join(lines)

import json

import pytest

from tradingagents.agents.utils.local_news import (
    prepare_news_catalog,
    prepare_news_sources,
    render_selected_news,
)


NEWS = """## Naver news
Source mode: pre-collected Naver Search API snapshot.

### Earnings announcement (source: Naver, 2026-09-11)
Reported  earnings rose 3.5%. Management expects stable demand.
Link: https://example.org/earnings

### Production update (source: Naver, 2026-09-11)
Production remains unchanged.
Link: https://example.org/production
"""


def render(items, **extra):
    sources = prepare_news_sources(NEWS)
    return render_selected_news(
        json.dumps({"ticker": "005930.KS", "items": items, **extra}),
        "005930.KS", sources, prepare_news_catalog(sources),
    )


def test_catalog_contains_literal_excerpts_and_excludes_metadata():
    sources = prepare_news_sources(NEWS)
    catalog = prepare_news_catalog(sources)
    assert catalog[1][2] == "Reported  earnings rose 3.5%."
    assert catalog[1][3] == "Management expects stable demand."
    assert catalog[2][2] == "Production remains unchanged."
    for article, excerpts in catalog.items():
        for excerpt in excerpts.values():
            assert excerpt in sources[article - 1]
            assert "Link:" not in excerpt
            assert "Source mode:" not in excerpt


def test_selected_text_and_links_come_only_from_current_source():
    report = render([{
        "article": 1, "excerpt": 2, "signal": "catalyst",
        "quote": "Invented profit guarantee", "explanation": "Invented story",
        "link": "https://invented.invalid",
    }], company="Invented name")
    assert "> Reported  earnings rose 3.5%." in report
    assert "https://example.org/earnings" in report
    assert "Invented" not in report
    assert "invented.invalid" not in report
    assert "Production remains" not in report


def test_korean_punctuation_and_spacing_are_preserved():
    excerpt = "삼성전자는  실적 발표 일정을 공지했다."
    sources = prepare_news_sources(f"### 국내 주식 뉴스 자료\n{excerpt}\nLink: https://example.org/kr")
    catalog = prepare_news_catalog(sources)
    report = render_selected_news(
        '{"ticker":"005930.KS","items":[{"article":1,"excerpt":2,"signal":"neutral"}]}',
        "005930.KS", sources, catalog,
    )
    assert f"> {excerpt}" in report


@pytest.mark.parametrize("article,excerpt", [
    (0, 1), (3, 1), (True, 1), ("1", 1), (1.0, 1), (None, 1),
    (1, 0), (1, 99), (1, True), (1, "2"), (1, 2.0), (1, None),
    (2, 3),  # Excerpt 3 exists in article 1, but not article 2.
])
def test_invalid_or_cross_article_ids_are_rejected(article, excerpt):
    with pytest.raises(ValueError, match="unknown"):
        render([{"article": article, "excerpt": excerpt, "signal": "neutral"}])


@pytest.mark.parametrize("signal", ["buy", None, [], 1])
def test_unknown_classification_is_rejected(signal):
    with pytest.raises(ValueError, match="classification"):
        render([{"article": 1, "excerpt": 2, "signal": signal}])


def test_duplicate_selection_is_rejected_even_with_different_signal():
    with pytest.raises(ValueError, match="duplicate"):
        render([
            {"article": 1, "excerpt": 2, "signal": "risk"},
            {"article": 1, "excerpt": 2, "signal": "neutral"},
        ])


@pytest.mark.parametrize("items", [[], [{}] * 7, None, ["text"]])
def test_invalid_evidence_items_are_rejected(items):
    with pytest.raises(ValueError):
        render(items)


@pytest.mark.parametrize("content", ["not JSON", "[]", '{"ticker":"OTHER","items":[]}'])
def test_malformed_response_or_wrong_ticker_is_rejected(content):
    sources = prepare_news_sources(NEWS)
    with pytest.raises(ValueError):
        render_selected_news(content, "005930.KS", sources, prepare_news_catalog(sources))


def test_catalog_remains_bounded_and_chunks_stay_in_source():
    news = "".join(f"### Article number {n}\n" + "x" * 4000 + "\n" for n in range(8))
    sources = prepare_news_sources(news)
    assert len(sources) <= 6
    assert sum(map(len, sources)) <= 14000
    catalog = prepare_news_catalog(sources)
    for article, excerpts in catalog.items():
        assert len(excerpts) <= 24
        for excerpt in excerpts.values():
            assert 10 <= len(excerpt) <= 800
            assert excerpt in sources[article - 1]
    assert len(prepare_news_catalog(["Enough source text.\n" * 100])[1]) == 24


@pytest.mark.parametrize("source", ["", "   ", "Link: https://example.org/news"])
def test_no_usable_source_fails_explicitly(source):
    with pytest.raises(ValueError, match="no usable"):
        prepare_news_catalog([source])


def test_tampered_catalog_cannot_bypass_source_verification():
    sources = prepare_news_sources(NEWS)
    catalog = prepare_news_catalog(sources)
    catalog[1][2] = "Fabricated unsupported earnings claim."
    with pytest.raises(ValueError, match="absent"):
        render_selected_news(
            '{"ticker":"005930.KS","items":[{"article":1,"excerpt":2,"signal":"risk"}]}',
            "005930.KS", sources, catalog,
        )

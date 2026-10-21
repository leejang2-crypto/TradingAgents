import json
import threading
from unittest.mock import Mock

import httpx
import pytest

from tradingagents.monitoring import summary


@pytest.fixture
def content():
    return {"date": "2026-10-21", "executionMode": "dry-run", "ok": False,
            "analyses": [{"ticker": "005930", "name": "삼성전자", "ok": False,
                          "decision": "HOLD. 거래량 근거 부족으로 신규 매수를 보류한다.",
                          "toss": {"plan": {"action": "HOLD"}, "fillVerified": False},
                          "accountContext": "PRIVATE ACCOUNT", "reportPath": "/private/report"}]}


def mock_ollama(monkeypatch, items):
    client = Mock()
    client.post.return_value = httpx.Response(200, request=httpx.Request("POST", "http://localhost:11434/api/chat"),
        json={"message": {"content": json.dumps({"items": items}, ensure_ascii=False)}})
    factory = Mock()
    factory.return_value.__enter__ = Mock(return_value=client)
    factory.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(summary.httpx, "Client", factory)
    return factory, client


def test_local_summary_is_grounded_and_never_sends_account_or_paths(monkeypatch, content):
    factory, client = mock_ollama(monkeypatch, [{"id": "0", "summary": "거래량 근거가 부족해 매수를 보류했습니다.",
                                               "evidenceIds": ["0:0"]}])
    result = summary.generate_summary(summary.source_material(content), {})
    assert result[0]["plannedAction"] == "HOLD"
    assert result[0]["fillVerified"] is False
    assert result[0]["evidence"] == content["analyses"][0]["decision"]
    assert result[0]["summaryKind"] == "ai"
    assert factory.call_args.kwargs == {"timeout": 180, "trust_env": False, "follow_redirects": False}
    assert client.post.call_args.args[0] == "http://localhost:11434/api/chat"
    payload = json.dumps(client.post.call_args.kwargs["json"])
    assert "PRIVATE ACCOUNT" not in payload and "/private/report" not in payload
    assert client.post.call_args.kwargs["json"]["model"] == "qwen3:8b"
    assert client.post.call_args.kwargs["json"]["format"]["type"] == "object"


@pytest.mark.parametrize("items", [[], [{"id": "wrong", "summary": "test", "evidenceIds": ["0:0"]}],
    [{"id": "0", "summary": "매수", "evidenceIds": ["1:0"]}],
    [{"id": "0", "summary": "", "evidenceIds": ["0:0"]}],
    [{"id": "0", "summary": "test", "evidenceIds": ["0:0", "0:0"]}],
    [{"id": "0", "summary": "test", "evidenceIds": [{}]}],
    [{"id": "0", "summary": "test", "evidence": "바꿔 쓴 인용문"}],
    [{"id": "0", "summary": "test", "evidenceIds": ["0:0"]}] * 2])
def test_invalid_summary_is_discarded_and_original_remains_visible(monkeypatch, content, items):
    mock_ollama(monkeypatch, items)
    result = summary.generate_summary(summary.source_material(content), {})
    assert result[0]["summaryKind"] == "source-only"
    assert result[0]["evidence"] == content["analyses"][0]["decision"]
    assert result[0]["summary"] not in {"test", "매수"}
    assert result[0]["warning"]


def test_numeric_id_and_bad_sibling_are_handled_independently(monkeypatch, content):
    content["analyses"].append({"decision": "SELL. 위험 증가로 매도 의견."})
    mock_ollama(monkeypatch, [{"id": 0, "summary": "매수 보류.", "evidenceIds": ["0:0"]},
                             {"id": "1", "summary": "허위 내용", "evidenceIds": ["0:0"]}])
    items = summary.generate_summary(summary.source_material(content), {})
    assert items[0]["summaryKind"] == "ai"
    assert items[1]["summaryKind"] == "source-only"
    assert "허위 내용" not in items[1]["summary"]


def test_short_hold_is_not_sent_to_llm(monkeypatch, content):
    content["analyses"][0]["decision"] = "Hold"
    client = Mock()
    monkeypatch.setattr(summary.httpx, "Client", client)
    items = summary.generate_summary(summary.source_material(content), {})
    client.assert_not_called()
    assert items[0]["summaryKind"] == "source-only"
    assert items[0]["evidence"] == "Hold"
    assert "상세 판단 근거" in items[0]["summary"]


def test_evidence_segments_always_come_from_source():
    source = 'A' * 800 + '\n\n  HOLD\t위험 증가  \n'
    segments = summary.evidence_segments(source, '0')
    assert len({row['id'] for row in segments}) == len(segments)
    assert all(row['text'] in source and len(row['text']) <= 350 for row in segments)


def test_reads_final_report_only_and_tracks_changes(tmp_path, content):
    report = tmp_path / 'logs/dry-run/run/reports/ticker/complete_report.md'
    report.parent.mkdir(parents=True)
    report.write_text('Other report\n## V. Portfolio Manager Decision\n\nHOLD. 거래량 부족.\n')
    content['analyses'][0]['reportPath'] = str(report)
    enriched = summary.with_report_evidence(content, tmp_path)
    assert 'portfolioEvidence' not in content['analyses'][0]
    material = summary.source_material(enriched)
    assert material['rows'][0]['sourceKind'] == 'portfolio-report'
    assert 'HOLD. 거래량 부족.' in material['rows'][0]['source']
    assert 'Other report' not in material['rows'][0]['source']
    service = summary.LocalSummaryService()
    first = service.request(enriched, {})['sourceHash']
    report.write_text('Other report\n## V. Portfolio Manager Decision\n\nHOLD. 신규 근거.\n')
    assert service.request(summary.with_report_evidence(content, tmp_path), {})['sourceHash'] != first


@pytest.mark.parametrize('kind', ['outside', 'wrong-mode', 'symlink', 'directory-link', 'large', 'malformed', 'missing'])
def test_report_reader_rejects_unsafe_or_invalid_files(tmp_path, content, kind):
    report = tmp_path / 'logs/dry-run/run/complete_report.md'
    report.parent.mkdir(parents=True)
    body = '\n## V. Portfolio Manager Decision\nPRIVATE TEXT'
    report.write_text(body)
    if kind == 'outside':
        report = tmp_path / 'complete_report.md'
        report.write_text(body)
    elif kind == 'wrong-mode':
        content['executionMode'] = 'live'
    elif kind in {'symlink', 'directory-link'}:
        outside = tmp_path / 'outside'
        outside.mkdir()
        target = outside / 'complete_report.md'
        target.write_text(body)
        report.unlink()
        if kind == 'symlink':
            report.symlink_to(target)
        else:
            report.parent.rmdir()
            report.parent.symlink_to(outside, target_is_directory=True)
    elif kind == 'large':
        report.write_text(body + 'x' * 512_001)
    elif kind == 'malformed':
        report.write_bytes(b'\xff')
    elif kind == 'missing':
        report.unlink()
    content['analyses'][0]['reportPath'] = str(report)
    content['analyses'][0]['portfolioEvidence'] = 'FORGED'
    row = summary.with_report_evidence(content, tmp_path)['analyses'][0]
    assert 'portfolioEvidence' not in row
    assert row['reportEvidenceStatus'] == 'unavailable'


@pytest.mark.parametrize("url", ["https://api.openai.com/v1", "http://example.com:11434/v1",
    "http://user:secret@localhost:11434/v1", "http://localhost:11434/v1?target=remote"])
def test_rejects_external_endpoint_before_request(monkeypatch, content, url):
    client = Mock()
    monkeypatch.setattr(summary.httpx, "Client", client)
    with pytest.raises(ValueError):
        summary.generate_summary(summary.source_material(content), {"ollama_base_url": url})
    client.assert_not_called()


def test_rejects_cloud_model(monkeypatch, content):
    client = Mock()
    monkeypatch.setattr(summary.httpx, "Client", client)
    with pytest.raises(ValueError):
        summary.generate_summary(summary.source_material(content), {"ollama_quick_model": "qwen:cloud"})
    client.assert_not_called()


def test_limits_are_explicit_and_empty_summary_skips_llm(monkeypatch, content):
    content["analyses"] *= 9
    content["analyses"][0]["decision"] = "a" * 2000
    material = summary.source_material(content)
    assert material["partial"] and material["total"] == 9 and len(material["rows"]) == 8
    assert len(material["rows"][0]["source"]) == 1600
    client = Mock()
    monkeypatch.setattr(summary.httpx, "Client", client)
    assert summary.generate_summary(summary.source_material({"analyses": []}), {}) == []
    client.assert_not_called()


def test_errors_are_preserved_alongside_decisions(content):
    content["error"] = "전체 실행 미완료"
    content["analyses"][0]["error"] = "주문 실행 실패"
    material = summary.source_material(content)
    assert material["error"] == "전체 실행 미완료"
    assert material["rows"][0]["error"] == "주문 실행 실패"
    assert "HOLD" in material["rows"][0]["source"]
    assert "주문 실행 실패" in material["rows"][0]["source"]


def test_cache_deduplication_invalidation_and_retry(monkeypatch, content):
    threads = []

    class DeferredThread:
        def __init__(self, target, args, **kwargs):
            self.target, self.args = target, args
            threads.append(self)

        def start(self):
            pass

        def finish(self):
            self.target(*self.args)

    monkeypatch.setattr(summary.threading, "Thread", DeferredThread)
    generate = Mock(return_value=[])
    monkeypatch.setattr(summary, "generate_summary", generate)
    service = summary.LocalSummaryService()
    assert service.request(content, {})["status"] == "idle"
    assert not threads
    first = service.request(content, {}, start=True)
    assert first["status"] == "running"
    assert service.request(content, {}, start=True) == first
    assert service.request(content, {"ollama_quick_model": "other"}, start=True)["status"] == "busy"
    assert len(threads) == 1
    threads[0].finish()
    cached = service.request(content, {}, start=True)
    assert cached["status"] == "completed" and len(threads) == 1
    cached["status"] = "tampered"
    assert service.request(content, {})["status"] == "completed"
    content["date"] = "changed"
    assert service.request(content, {})["status"] == "idle"
    generate.side_effect = RuntimeError("PRIVATE BACKEND ERROR")
    service.request(content, {}, start=True)
    threads[-1].finish()
    failed = service.request(content, {})
    assert failed["status"] == "error" and "PRIVATE" not in failed["error"]
    assert service.request(content, {}, start=True)["status"] == "running"
    threads[-1].finish()


def test_real_thread_completes_without_blocking(monkeypatch, content):
    entered, release = threading.Event(), threading.Event()

    def generate(*args):
        entered.set()
        assert release.wait(3)
        return []

    monkeypatch.setattr(summary, "generate_summary", generate)
    service = summary.LocalSummaryService()
    try:
        assert service.request(content, {}, start=True)["status"] == "running"
        assert entered.wait(3)
        assert service.request(content, {})["status"] == "running"
    finally:
        release.set()

"""KOSPI 100 constituents, never a market-cap or ETF-holdings approximation."""

import csv
import io
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

INDEX_NAME = "KOSPI100"
RESEARCH_TIMEOUT_SECONDS = 3600
SOURCE_URL = "https://data.krx.co.kr/contents/MDC/MAIN/main/index.cmd"


class UniverseError(ValueError):
    pass


def today_korea():
    return datetime.now(ZoneInfo("Asia/Seoul")).date()


def validate_snapshot(snapshot, *, today=None):
    today = today or today_korea()
    if not isinstance(snapshot, dict) or snapshot.get("index") != INDEX_NAME:
        raise UniverseError("코스피100 구성종목 목록이 필요합니다.")
    try:
        as_of = date.fromisoformat(snapshot["asOf"])
        checked = date.fromisoformat(snapshot["checkedOn"])
    except (KeyError, ValueError, TypeError):
        raise UniverseError("구성종목 기준일을 확인할 수 없습니다.") from None
    if not 0 <= (today - as_of).days <= 10 or checked != today:
        raise UniverseError("코스피100 목록을 오늘 다시 확인하거나 최근 기준일의 KRX CSV를 등록하세요.")
    members = snapshot.get("members")
    if not isinstance(members, dict) or len(members) != 100:
        raise UniverseError("코스피100의 중복 없는 100종목 전체 목록이 필요합니다.")
    if any(not isinstance(code, str) or not re.fullmatch(r"\d{6}\.KS", code)
           or not isinstance(name, str) or not name.strip() or len(name) > 100 for code, name in members.items()):
        raise UniverseError("구성종목 코드 또는 이름이 올바르지 않습니다.")
    if snapshot.get("source") not in ("KRX 자동 조회", "사용자 확인 KRX CSV"):
        raise UniverseError("구성종목 출처를 확인할 수 없습니다.")
    return snapshot


def fetch_krx(*, today=None):
    today = today or today_korea()
    for offset in range(10):
        day = today - timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        try:
            response = requests.post("https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd",
                data={"bld": "dbms/MDC/STAT/standard/MDCSTAT00601", "indIdx": "1", "indIdx2": "034", "trdDd": day.strftime("%Y%m%d")},
                headers={"Referer": "https://data.krx.co.kr/"}, timeout=15)
            response.raise_for_status()
            rows = response.json().get("output")
        except (requests.RequestException, ValueError, AttributeError):
            raise UniverseError("KRX 구성종목 조회에 실패했습니다. KRX에서 코스피100 CSV를 내려받아 등록하세요.") from None
        if rows == []:
            continue
        if not isinstance(rows, list):
            raise UniverseError("KRX 구성종목 응답 형식이 변경되었습니다. 코스피100 CSV를 등록하세요.")
        members = {}
        for row in rows:
            if not isinstance(row, dict):
                raise UniverseError("KRX 구성종목 응답을 확인할 수 없습니다.")
            code, name = row.get("ISU_SRT_CD"), row.get("ISU_ABBRV")
            if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code) or code + ".KS" in members:
                raise UniverseError("KRX 구성종목 코드가 잘못되었거나 중복되었습니다.")
            members[code + ".KS"] = name
        return validate_snapshot({"index": INDEX_NAME, "asOf": day.isoformat(), "checkedOn": today.isoformat(),
                                  "source": "KRX 자동 조회", "sourceUrl": SOURCE_URL, "members": members}, today=today)
    raise UniverseError("최근 10일의 코스피100 구성종목을 확인할 수 없습니다. KRX CSV를 등록하세요.")


def import_csv(payload, *, today=None):
    try:
        return _import_csv(payload, today=today)
    except csv.Error:
        raise UniverseError("CSV 형식을 읽을 수 없습니다. KRX 원본 CSV를 등록하세요.") from None


def _import_csv(payload, *, today=None):
    today = today or today_korea()
    if not isinstance(payload, dict):
        raise UniverseError("CSV 등록 요청 형식이 올바르지 않습니다.")
    if payload.get("confirmed") is not True:
        raise UniverseError("KRX 코스피100 구성종목 CSV인지 확인해야 합니다.")
    text = payload.get("csv")
    if not isinstance(text, str) or len(text.encode("utf-8")) > 120_000:
        raise UniverseError("CSV는 120 KB 이하의 텍스트여야 합니다.")
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")), strict=True)
    names = reader.fieldnames or []
    code_key = next((key for key in ("종목코드", "구성종목코드", "단축코드", "ISU_SRT_CD") if key in names), None)
    name_key = next((key for key in ("종목명", "구성종목명", "한글종목명", "ISU_ABBRV") if key in names), None)
    if not code_key or not name_key:
        raise UniverseError("CSV에 종목코드와 종목명 열이 필요합니다.")
    members = {}
    for row in reader:
        code = (row.get(code_key) or "").strip().lstrip("'")
        name = (row.get(name_key) or "").strip()
        if not re.fullmatch(r"\d{6}", code) or code + ".KS" in members:
            raise UniverseError("CSV 종목코드는 앞자리 0을 포함한 6자리이며 중복이 없어야 합니다.")
        members[code + ".KS"] = name
        if len(members) > 100:
            raise UniverseError("코스피100 전체 100종목 CSV가 필요합니다.")
    return validate_snapshot({"index": INDEX_NAME, "asOf": payload.get("asOf"), "checkedOn": today.isoformat(),
                              "source": "사용자 확인 KRX CSV", "sourceUrl": SOURCE_URL, "members": members}, today=today)


def resolve_universe(cached=None, *, fetch=None, today=None):
    today = today or today_korea()
    if cached:
        try:
            return validate_snapshot(cached, today=today)
        except UniverseError:
            pass
    # A stale snapshot is never silently substituted after a failed refresh.
    return validate_snapshot((fetch or fetch_krx)(today=today), today=today)


def select_members(requested, snapshot):
    members = validate_snapshot(snapshot)["members"]
    if requested:
        outsiders = [ticker for ticker in requested if ticker not in members]
        if outsiders:
            raise UniverseError("코스피100 구성종목이 아닙니다: " + ", ".join(outsiders))
    return list(requested or members)

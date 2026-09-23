"""KOSPI 200 constituents, never a market-cap or ETF-holdings approximation."""

import csv
import io
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

INDEX_NAME = "KOSPI200"
MEMBER_COUNT = 200
MAX_MEMBER_COUNT = 201
ISSUE_CODE_PATTERN = r"[0-9A-Z]{6}"
TICKER_PATTERN = ISSUE_CODE_PATTERN + r"\.KS"
RESEARCH_TIMEOUT_SECONDS = 7200
SOURCE_URL = "https://data.krx.co.kr/contents/MDC/MAIN/main/index.cmd"


class UniverseError(ValueError):
    pass


def today_korea():
    return datetime.now(ZoneInfo("Asia/Seoul")).date()


def validate_snapshot(snapshot, *, today=None):
    today = today or today_korea()
    if not isinstance(snapshot, dict) or snapshot.get("index") != INDEX_NAME:
        raise UniverseError("코스피200 구성종목 목록이 필요합니다.")
    try:
        as_of = date.fromisoformat(snapshot["asOf"])
        checked = date.fromisoformat(snapshot["checkedOn"])
    except (KeyError, ValueError, TypeError):
        raise UniverseError("구성종목 기준일을 확인할 수 없습니다.") from None
    if as_of > today or checked > today or checked < as_of:
        raise UniverseError("구성종목 조회 기준일 또는 확인일이 올바르지 않습니다.")
    # User-uploaded constituents stay pinned until another CSV is registered.
    if snapshot.get("source") != "사용자 확인 KRX CSV" and ((today - as_of).days > 10 or checked != today):
        raise UniverseError("코스피200 목록을 오늘 다시 확인하거나 최근 기준일의 KRX CSV를 등록하세요.")
    members = snapshot.get("members")
    if not isinstance(members, dict) or len(members) not in (MEMBER_COUNT, MAX_MEMBER_COUNT):
        count = len(members) if isinstance(members, dict) else 0
        raise UniverseError(f"코스피200 전체 구성목록이 필요합니다. 인식 {count}종목: 기본 200종목, 별도 확인 시 201종목을 지원합니다.")
    if len(members) != MEMBER_COUNT and snapshot.get("source") == "사용자 확인 KRX CSV" and snapshot.get("nonstandardCountConfirmed") is not True:
        raise UniverseError("201종목을 인식했습니다. KRX 원본도 201종목인지 확인하고 '201종목 원본 확인'에 체크하세요. 임의로 한 종목을 삭제하지 마세요.")
    if any(not isinstance(code, str) or not re.fullmatch(TICKER_PATTERN, code)
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
                data={"bld": "dbms/MDC/STAT/standard/MDCSTAT00601", "indIdx": "1", "indIdx2": "028", "trdDd": day.strftime("%Y%m%d")},
                headers={"Referer": "https://data.krx.co.kr/"}, timeout=15)
            response.raise_for_status()
            rows = response.json().get("output")
        except (requests.RequestException, ValueError, AttributeError):
            raise UniverseError("KRX 구성종목 조회에 실패했습니다. KRX에서 코스피200 CSV를 내려받아 등록하세요.") from None
        if rows == []:
            continue
        if not isinstance(rows, list):
            raise UniverseError("KRX 구성종목 응답 형식이 변경되었습니다. 코스피200 CSV를 등록하세요.")
        members = {}
        for row in rows:
            if not isinstance(row, dict):
                raise UniverseError("KRX 구성종목 응답을 확인할 수 없습니다.")
            code, name = row.get("ISU_SRT_CD"), row.get("ISU_ABBRV")
            if not isinstance(code, str) or not re.fullmatch(ISSUE_CODE_PATTERN, code) or code + ".KS" in members:
                raise UniverseError("KRX 구성종목 코드가 잘못되었거나 중복되었습니다.")
            members[code + ".KS"] = name
        return validate_snapshot({"index": INDEX_NAME, "asOf": day.isoformat(), "checkedOn": today.isoformat(),
                                  "source": "KRX 자동 조회", "sourceUrl": SOURCE_URL, "members": members}, today=today)
    raise UniverseError("최근 10일의 코스피200 구성종목을 확인할 수 없습니다. KRX CSV를 등록하세요.")


def import_csv(payload, *, today=None):
    try:
        return _import_csv(payload, today=today)
    except csv.Error:
        raise UniverseError("CSV 형식을 읽을 수 없습니다. KRX 원본 CSV를 등록하세요.") from None


def _csv_reader(text):
    aliases = (
        {"종목코드", "구성종목코드", "단축코드", "isu_srt_cd", "issuecode"},
        {"종목명", "구성종목명", "한글종목명", "isu_abbrv", "issuename"},
    )
    # Detect the delimiter from recognized header fields, not commas inside names.
    for delimiter in (",", "\t"):
        reader = csv.reader(io.StringIO(text.lstrip("\ufeff\r\n ")), delimiter=delimiter, strict=True, skipinitialspace=True)
        try:
            header = next(reader, [])
        except csv.Error:
            continue
        names = ["".join(name.split()).casefold() for name in header]
        matches = [[index for index, name in enumerate(names) if name in group] for group in aliases]
        if all(matches):
            if any(len(match) != 1 for match in matches):
                raise UniverseError("CSV의 종목코드 또는 종목명 열이 중복되어 구분할 수 없습니다.")
            return reader, header, matches[0][0], matches[1][0], delimiter
    raise UniverseError("CSV/TSV에 '종목코드·종목명' 또는 'Issue code·Issue name' 열이 필요합니다.")


def _csv_code(value, line):
    code = value.strip().removeprefix("'").upper()
    if re.fullmatch(r"[0-9]{1,6}", code):
        return code.zfill(6)
    if re.fullmatch(ISSUE_CODE_PATTERN, code):
        return code
    raise UniverseError(f"CSV {line}행 종목코드 형식 오류: {value[:30]!r}. 숫자 1~6자리 또는 0126Z0 같은 영숫자 6자리가 필요합니다.")


def _import_csv(payload, *, today=None):
    today = today or today_korea()
    if not isinstance(payload, dict):
        raise UniverseError("CSV 등록 요청 형식이 올바르지 않습니다.")
    if payload.get("confirmed") is not True:
        raise UniverseError("KRX 코스피200 구성종목 CSV인지 확인해야 합니다.")
    file_name = payload.get("fileName")
    if file_name is not None:
        if not isinstance(file_name, str):
            raise UniverseError("CSV 파일명이 올바르지 않습니다.")
        file_name = file_name.replace("\\", "/").rsplit("/", 1)[-1].strip()
        if not file_name or len(file_name) > 255 or any(ord(char) < 32 or ord(char) == 127 for char in file_name):
            raise UniverseError("CSV 파일명이 올바르지 않습니다.")
    text = payload.get("csv")
    if not isinstance(text, str) or len(text.encode("utf-8")) > 120_000:
        raise UniverseError("CSV는 120 KB 이하의 텍스트여야 합니다.")
    reader, header, code_index, name_index, delimiter = _csv_reader(text)
    members = {}
    padded = 0
    for row in reader:
        if not any(value.strip() for value in row):
            continue
        if len(row) <= max(code_index, name_index) or any(value.strip() for value in row[len(header):]):
            raise UniverseError(f"CSV {reader.line_num}행의 열 수가 맞지 않습니다. 쉼표가 있는 종목명은 따옴표로 감싸야 합니다.")
        raw_code = row[code_index]
        code = _csv_code(raw_code, reader.line_num)
        name = row[name_index].strip()
        if not name or len(name) > 100:
            raise UniverseError(f"CSV {reader.line_num}행 종목명이 없거나 100자를 초과합니다.")
        if code + ".KS" in members:
            raise UniverseError(f"CSV {reader.line_num}행의 종목코드 {code}가 중복되었습니다. 앞자리 0 보정 후에도 중복은 허용하지 않습니다.")
        padded += bool(re.fullmatch(r"[0-9]{1,5}", raw_code.strip().removeprefix("'")))
        members[code + ".KS"] = name
        if len(members) > MAX_MEMBER_COUNT:
            raise UniverseError("202종목 이상입니다. 코스피200 원본 구성목록인지 확인하세요. 종목을 임의로 잘라 저장하지 않습니다.")
    return validate_snapshot({"index": INDEX_NAME, "asOf": payload.get("asOf"), "checkedOn": today.isoformat(),
                              "fileName": file_name,
                              "registeredAt": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds"),
                              "nonstandardCountConfirmed": payload.get("nonstandardCountConfirmed") is True,
                              "importInfo": {"rowCount": len(members), "zeroPaddedCodes": padded,
                                             "alphanumericCodes": sum(bool(re.search(r"[A-Z]", code[:6])) for code in members),
                                             "format": "TSV" if delimiter == "\t" else "CSV"},
                              "source": "사용자 확인 KRX CSV", "sourceUrl": SOURCE_URL, "members": members}, today=today)


def resolve_universe(cached=None, *, fetch=None, today=None):
    today = today or today_korea()
    if isinstance(cached, dict) and cached.get("source") == "사용자 확인 KRX CSV":
        # Never replace an uploaded list automatically, even if it is malformed.
        return validate_snapshot(cached, today=today)
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
            raise UniverseError("코스피200 구성종목이 아닙니다: " + ", ".join(outsiders))
    return list(requested or members)

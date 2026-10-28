"""
앱 전용 구글 시트 연결 (Users · Channels · Channel Rules · Rule Requests 탭이 모두 같은 스프레드시트)

Secrets
  [users_sheet]
  spreadsheet_id = "..."      # 이 스프레드시트 하나에 모든 탭이 있음
  worksheet = "Users"         # 사용자 탭 이름 (생략하면 Users)

  [gcp_service_account]       # 서비스 계정 키 JSON 내용

읽기 횟수 줄이기 (Google Sheets API: 서비스 계정당 분당 읽기 60회 제한)
  - 스프레드시트 · 탭 정보는 앱이 켜질 때 한 번만 가져와서 재사용
  - 여러 탭을 읽을 때는 read_tables()로 한 번에 (요청 1회)
  - 한도에 걸리면(429) 잠깐 기다렸다가 자동으로 다시 시도

탭이 없으면 자동으로 만들고, 비어 있으면 1행에 양식(헤더)을 채웁니다.
※ gspread 5.x / 6.x 모두 동작하도록 values_batch_get · batch_update · append_row만 사용
"""
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread
import streamlit as st
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
TIMEZONE = ZoneInfo("America/Los_Angeles")      # 시트에 남는 시간 기준
RETRY_WAITS = [2, 5, 10]                        # 429(한도 초과) 때 기다릴 시간(초)


@st.cache_resource(show_spinner=False)
def _spreadsheet():
    creds = Credentials.from_service_account_info(dict(st.secrets["gcp_service_account"]), scopes=SCOPES)
    return gspread.authorize(creds).open_by_key(st.secrets["users_sheet"]["spreadsheet_id"])


@st.cache_resource(show_spinner=False)
def _worksheets():
    return {}   # {탭 이름: Worksheet} — 한 번 연 탭은 다시 조회하지 않음


def _retry(fn):
    """한도 초과(429)면 잠깐 기다렸다가 다시 시도"""
    for wait in RETRY_WAITS + [None]:
        try:
            return fn()
        except gspread.exceptions.APIError as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status != 429 or wait is None:
                raise
            time.sleep(wait)


def worksheet(title, n_cols):
    """탭 열기 (없으면 새로 만듦)"""
    cache = _worksheets()
    if title not in cache:
        ss = _spreadsheet()
        try:
            cache[title] = _retry(lambda: ss.worksheet(title))
        except gspread.exceptions.WorksheetNotFound:
            cache[title] = _retry(lambda: ss.add_worksheet(title=title, rows=1000, cols=max(n_cols, 1)))
    return cache[title]


def col_letter(i):
    """0 → A, 1 → B …"""
    return chr(ord("A") + i)


def now():
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M")


def _parse(ws, headers, values):
    """시트 값 → (처음 만든 표인지, [{컬럼: 값, 'row': 시트 행 번호}])
    비어 있으면 1행에 headers를 채우고 (True, [])"""
    if not values or not any(str(c).strip() for c in values[0]):
        _retry(lambda: ws.batch_update([{"range": f"A1:{col_letter(len(headers) - 1)}1", "values": [headers]}]))
        return True, []
    header = [str(c).strip() for c in values[0]]
    if header[:len(headers)] != headers:
        raise RuntimeError(f"The first row of the '{ws.title}' sheet must be: {', '.join(headers)}")
    records = []
    for r, row in enumerate(values[1:], start=2):
        row = ([str(c) for c in row] + [""] * len(headers))[:len(headers)]
        if any(c.strip() for c in row):
            records.append({**dict(zip(headers, row)), "row": r})
    return False, records


def read_tables(spec):
    """{탭 이름: headers} → {탭 이름: (처음 만든 표인지, 행 목록)} — 모든 탭을 요청 1회로 읽음"""
    sheets = {title: worksheet(title, len(headers)) for title, headers in spec.items()}
    ss = _spreadsheet()
    titles = list(spec)
    result = _retry(lambda: ss.values_batch_get([f"'{t}'" for t in titles]))
    ranges = result.get("valueRanges", [])
    out = {}
    for i, title in enumerate(titles):
        values = ranges[i].get("values", []) if i < len(ranges) else []
        out[title] = _parse(sheets[title], spec[title], values)
    return out


def read_table(ws, headers):
    """탭 하나 읽기 (요청 1회)"""
    return read_tables({ws.title: headers})[ws.title]


def write_rows(ws, headers, rows, old_count):
    """2행부터 rows로 덮어쓰고, 예전보다 줄이 줄었으면 남는 줄은 비움 (탭 전체를 지우지 않음)"""
    blank = [""] * len(headers)
    values = [[r.get(h, "") for h in headers] for r in rows] + [blank] * max(0, old_count - len(rows))
    if values:
        _retry(lambda: ws.batch_update(
            [{"range": f"A2:{col_letter(len(headers) - 1)}{len(values) + 1}", "values": values}]))


def append_row(ws, values):
    _retry(lambda: ws.append_row(values))   # RAW 입력 (수식으로 해석 안 됨)


def batch_update(ws, data):
    _retry(lambda: ws.batch_update(data))


@st.cache_resource(show_spinner=False)
def _dropdown_done(spreadsheet_id, sheet_id, col, options):
    return {"done": False}


def ensure_dropdown(ws, col, options):
    """col(0부터) 컬럼의 2행부터 끝까지를 options 드롭다운으로 (앱이 켜져 있는 동안 같은 설정은 한 번만)"""
    ss = _spreadsheet()
    flag = _dropdown_done(ss.id, ws.id, col, tuple(options))
    if flag["done"]:
        return
    try:
        _retry(lambda: ss.batch_update({"requests": [{"setDataValidation": {
            "range": {"sheetId": ws.id, "startRowIndex": 1, "startColumnIndex": col, "endColumnIndex": col + 1},
            "rule": {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": v} for v in options]},
                     "strict": True, "showCustomUi": True},
        }}]}))
        flag["done"] = True
    except Exception:
        pass                                # 드롭다운 설정이 실패해도 앱은 계속
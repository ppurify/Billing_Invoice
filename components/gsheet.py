"""
앱 전용 구글 시트 연결 (Users · Channel Rules · Rule Requests 탭이 모두 같은 스프레드시트)

Secrets
  [users_sheet]
  spreadsheet_id = "..."      # 이 스프레드시트 하나에 모든 탭이 있음
  worksheet = "Users"         # 사용자 탭 이름 (생략하면 Users)

  [gcp_service_account]       # 서비스 계정 키 JSON 내용

탭이 없으면 자동으로 만들고, 비어 있으면 1행에 양식(헤더)을 채웁니다.
※ gspread 5.x / 6.x 모두 동작하도록 get_all_values · batch_update · append_row만 사용
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread
import streamlit as st
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
TIMEZONE = ZoneInfo("America/Los_Angeles")      # 시트에 남는 시간 기준


@st.cache_resource(show_spinner=False)
def _client():
    creds = Credentials.from_service_account_info(dict(st.secrets["gcp_service_account"]), scopes=SCOPES)
    return gspread.authorize(creds)


def worksheet(title, n_cols):
    """탭 열기 (없으면 새로 만듦)"""
    ss = _client().open_by_key(st.secrets["users_sheet"]["spreadsheet_id"])
    try:
        return ss.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return ss.add_worksheet(title=title, rows=1000, cols=max(n_cols, 1))


def col_letter(i):
    """0 → A, 1 → B …"""
    return chr(ord("A") + i)


def now():
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M")


def read_table(ws, headers):
    """1행 = headers 인 표 → (처음 만든 표인지, [{컬럼: 값, 'row': 시트 행 번호}])
    비어 있으면 1행에 headers를 채우고 (True, [])"""
    values = ws.get_all_values()
    if not values or not any(c.strip() for c in values[0]):
        ws.batch_update([{"range": f"A1:{col_letter(len(headers) - 1)}1", "values": [headers]}])
        return True, []
    header = [c.strip() for c in values[0]]
    if header[:len(headers)] != headers:
        raise RuntimeError(f"The first row of the '{ws.title}' sheet must be: {', '.join(headers)}")
    records = []
    for r, row in enumerate(values[1:], start=2):
        row = (row + [""] * len(headers))[:len(headers)]
        if any(c.strip() for c in row):
            records.append({**dict(zip(headers, row)), "row": r})
    return False, records


def write_rows(ws, headers, rows, old_count):
    """2행부터 rows로 덮어쓰고, 예전보다 줄이 줄었으면 남는 줄은 비움 (탭 전체를 지우지 않음)"""
    blank = [""] * len(headers)
    values = [[r.get(h, "") for h in headers] for r in rows] + [blank] * max(0, old_count - len(rows))
    if values:
        ws.batch_update([{"range": f"A2:{col_letter(len(headers) - 1)}{len(values) + 1}", "values": values}])


@st.cache_resource(show_spinner=False)
def _dropdown_done(spreadsheet_id, sheet_id, col, options):
    return {"done": False}


def ensure_dropdown(ws, col, options):
    """col(0부터) 컬럼의 2행부터 끝까지를 options 드롭다운으로 (앱이 켜져 있는 동안 탭마다 한 번만)"""
    flag = _dropdown_done(ws.spreadsheet.id, ws.id, col, tuple(options))
    if flag["done"]:
        return
    try:
        ws.spreadsheet.batch_update({"requests": [{"setDataValidation": {
            "range": {"sheetId": ws.id, "startRowIndex": 1, "startColumnIndex": col, "endColumnIndex": col + 1},
            "rule": {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": v} for v in options]},
                     "strict": True, "showCustomUi": True},
        }}]})
        flag["done"] = True
    except Exception:
        pass                                # 드롭다운 설정이 실패해도 앱은 계속

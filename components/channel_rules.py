"""
IBD / IBA / RED 구분 규칙 (구글 시트에 저장)

탭 (Users와 같은 스프레드시트, 없으면 자동 생성)
  Channel Rules : 현재 적용 중인 규칙. Priority 순서대로 확인해서 처음 맞는 키워드의 Channel 적용, 없으면 RED
                  규칙은 이 탭에만 있고 코드에는 없음 (처음 만들어지면 비어 있음 → 관리자가 페이지에서 입력)
  Rule Requests : 사용자의 규칙 변경 요청. 관리자가 Approved 하면 그때 Channel Rules에 반영

요청 종류
  Add    : 새 키워드 추가 (Priority 비우면 맨 뒤)
  Change : 기존 키워드의 Channel 또는 Priority 변경 (Priority 비우면 위치 유지)
  Delete : 기존 키워드 삭제

관리자는 요청 없이 Channel Rules를 바로 수정할 수도 있습니다 ('Channel Rules' 페이지).
"""
import streamlit as st

from billing.fedex_parser import IBD, IBA, RED
from components import gsheet

RULES_TAB, REQUESTS_TAB = "Channel Rules", "Rule Requests"
RULE_HEADERS = ["Priority", "Keyword", "Channel", "Updated At", "Updated By"]
REQ_HEADERS = ["Requested At", "Requested By", "Action", "Keyword", "Channel", "Priority", "Reason",
               "Reviewed At", "Reviewed By", "Status"]
CHANNELS = [IBD, IBA, RED]
ADD, CHANGE, DELETE = "Add", "Change", "Delete"
ACTIONS = [ADD, CHANGE, DELETE]
PENDING, APPROVED, REJECTED = "Pending", "Approved", "Rejected"
STATUSES = [PENDING, APPROVED, REJECTED]
RULES_CACHE_TTL = 60   # 다른 사람이 바꾼 규칙이 FedEx 화면에 반영되기까지 최대 시간(초)


def _key(keyword):
    return (keyword or "").strip().upper()


def _to_int(value):
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


# ── 규칙 (Channel Rules) ─────────────────────────────────────────────
def _rules_ws():
    ws = gsheet.worksheet(RULES_TAB, len(RULE_HEADERS))
    gsheet.ensure_dropdown(ws, RULE_HEADERS.index("Channel"), CHANNELS)
    return ws


def read_rules():
    """현재 규칙 [{Priority, Keyword, Channel, Updated At, Updated By}] (Priority 순)"""
    ws = _rules_ws()
    _, rows = gsheet.read_table(ws, RULE_HEADERS)
    rules = [r for r in rows if r["Keyword"].strip()]
    rules.sort(key=lambda r: (_to_int(r["Priority"]) is None, _to_int(r["Priority"]) or 0, r["row"]))
    return ws, rules, len(rows)


@st.cache_data(ttl=RULES_CACHE_TTL, show_spinner=False)
def load_rules():
    """FedEx 화면용: ((키워드, Channel), ...) — Priority 순"""
    _, rules, _ = read_rules()
    return tuple((r["Keyword"].strip(), r["Channel"].strip().upper()) for r in rules
                 if r["Channel"].strip().upper() in CHANNELS)


def save_rules(rules, by):
    """규칙 목록을 Priority 순서 그대로 1, 2, 3 … 으로 다시 번호 매겨 저장.
    내용이 바뀐 규칙만 Updated At / By 갱신"""
    ws, old, old_count = read_rules()
    old_by_key = {_key(r["Keyword"]): r for r in old}
    out = []
    for i, r in enumerate(rules, start=1):
        prev = old_by_key.get(_key(r["Keyword"]))
        same = prev and prev["Channel"].strip().upper() == r["Channel"] and _to_int(prev["Priority"]) == i
        out.append({"Priority": i, "Keyword": r["Keyword"].strip(), "Channel": r["Channel"],
                    "Updated At": prev["Updated At"] if same else gsheet.now(),
                    "Updated By": prev["Updated By"] if same else by})
    gsheet.write_rows(ws, RULE_HEADERS, out, old_count)
    load_rules.clear()


def validate_rules(rules):
    """[{Keyword, Channel}] 확인 → 오류 메시지 목록"""
    errors, seen = [], set()
    for i, r in enumerate(rules, start=1):
        k = _key(r.get("Keyword"))
        if not k:
            errors.append(f"Row {i}: keyword is empty.")
        elif k in seen:
            errors.append(f"Row {i}: keyword '{r['Keyword'].strip()}' is duplicated.")
        seen.add(k)
        if r.get("Channel") not in CHANNELS:
            errors.append(f"Row {i}: channel must be one of {', '.join(CHANNELS)}.")
    return errors


def apply_change(rules, action, keyword, channel=None, priority=None):
    """규칙 목록에 요청 하나 적용 → (새 목록, 오류 메시지 또는 None). 원래 목록은 바꾸지 않음"""
    rules = [dict(r) for r in rules]
    idx = next((i for i, r in enumerate(rules) if _key(r["Keyword"]) == _key(keyword)), None)
    pos = None if priority is None else max(0, min(int(priority) - 1, len(rules)))
    if action == ADD:
        if idx is not None:
            return rules, f"Keyword '{keyword}' already exists."
        new = {"Keyword": keyword.strip(), "Channel": channel}
        rules.insert(len(rules) if pos is None else pos, new)
    elif action == CHANGE:
        if idx is None:
            return rules, f"Keyword '{keyword}' does not exist."
        rule = rules.pop(idx)
        rule["Channel"] = channel or rule["Channel"]
        rules.insert(idx if pos is None else min(pos, len(rules)), rule)
    elif action == DELETE:
        if idx is None:
            return rules, f"Keyword '{keyword}' does not exist."
        rules.pop(idx)
    else:
        return rules, f"Unknown action '{action}'."
    return rules, None


def describe(action, keyword, channel=None, priority=None):
    """요청 한 줄 설명"""
    where = f" at priority {priority}" if priority not in (None, "") else ""
    if action == ADD:
        return f"Add '{keyword}' → {channel}{where}"
    if action == CHANGE:
        return f"Change '{keyword}' → {channel}{where}"
    return f"Delete '{keyword}'"


# ── 요청 (Rule Requests) ─────────────────────────────────────────────
def _requests_ws():
    ws = gsheet.worksheet(REQUESTS_TAB, len(REQ_HEADERS))
    gsheet.ensure_dropdown(ws, REQ_HEADERS.index("Status"), STATUSES)
    return ws


def read_requests():
    ws = _requests_ws()
    _, rows = gsheet.read_table(ws, REQ_HEADERS)
    return ws, rows


def submit_request(username, action, keyword, channel, priority, reason):
    """사용자 요청 저장 → 오류 메시지 또는 None"""
    keyword = (keyword or "").strip()
    if not keyword:
        return "Please enter a keyword."
    if action != DELETE and channel not in CHANNELS:
        return "Please choose a channel."
    _, rules, _ = read_rules()
    _, err = apply_change(rules, action, keyword, channel, priority)   # 지금 규칙 기준으로 가능한 요청인지 확인
    if err:
        return err
    ws, _ = read_requests()
    ws.append_row([gsheet.now(), username, action, keyword, "" if action == DELETE else channel,
                   "" if priority is None else int(priority), (reason or "").strip(), "", "", PENDING])
    return None


def review_request(req, approve, admin):
    """요청 승인(규칙에 반영) 또는 거절 → 오류 메시지 또는 None"""
    if approve:
        _, rules, _ = read_rules()
        new_rules, err = apply_change(rules, req["Action"], req["Keyword"],
                                      req["Channel"].strip().upper() or None, _to_int(req["Priority"]))
        if err:
            return f"Cannot apply: {err}"
        save_rules([{"Keyword": r["Keyword"], "Channel": r["Channel"].strip().upper()} for r in new_rules],
                   by=f"{admin} (request by {req['Requested By']})")
    ws = _requests_ws()
    c = REQ_HEADERS.index
    ws.batch_update([
        {"range": f"{gsheet.col_letter(c('Reviewed At'))}{req['row']}:{gsheet.col_letter(c('Status'))}{req['row']}",
         "values": [[gsheet.now(), admin, APPROVED if approve else REJECTED]]},
    ])
    return None
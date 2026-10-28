"""
Channel 목록 · 구분 규칙 (구글 시트에 저장, 코드에는 없음)

탭 (Users와 같은 스프레드시트, 없으면 자동 생성)
  Channels      : 사용할 Channel 목록 (Order 순서 = FedEx 합계표 컬럼 순서)
                  RED는 기본 Channel이라 항상 있고 지울 수 없음 (규칙에 안 맞는 행 · Other Charges)
                  처음 만들 때는 RED + 'Channel Rules' 탭에서 이미 쓰고 있는 Channel로 채움
  Channel Rules : 키워드 → Channel. Priority 순서대로 확인해서 처음 맞는 키워드의 Channel, 없으면 RED
                  처음 만들어지면 비어 있음 → 관리자가 페이지에서 입력
  Rule Requests : 사용자의 변경 요청. 관리자가 Approve 하면 그때 반영

요청 종류
  Add / Change / Delete           : 키워드 규칙 추가 · 변경 · 삭제
  Add Channel / Delete Channel    : Channel 추가 · 삭제 (규칙에서 쓰고 있는 Channel은 삭제 불가)

관리자는 요청 없이 Channel 목록과 규칙을 바로 수정할 수도 있습니다 ('Channel Rules' 페이지).
"""
import re

import streamlit as st

from billing.fedex_parser import RED
from components import gsheet

CHANNELS_TAB, RULES_TAB, REQUESTS_TAB = "Channels", "Channel Rules", "Rule Requests"
CH_HEADERS = ["Order", "Channel", "Updated At", "Updated By"]
RULE_HEADERS = ["Priority", "Keyword", "Channel", "Updated At", "Updated By"]
REQ_HEADERS = ["Requested At", "Requested By", "Action", "Keyword", "Channel", "Priority", "Reason",
               "Reviewed At", "Reviewed By", "Status"]
DEFAULT_CHANNEL = RED
ADD, CHANGE, DELETE, ADD_CH, DEL_CH = "Add", "Change", "Delete", "Add Channel", "Delete Channel"
ACTIONS = [ADD, CHANGE, DELETE, ADD_CH, DEL_CH]
CHANNEL_ACTIONS = (ADD_CH, DEL_CH)
PENDING, APPROVED, REJECTED = "Pending", "Approved", "Rejected"
STATUSES = [PENDING, APPROVED, REJECTED]
CACHE_TTL = 60   # 다른 사람이 바꾼 내용이 FedEx 화면에 반영되기까지 최대 시간(초)

CHANNEL_RE = re.compile(r"^[A-Z0-9][A-Z0-9 _-]{0,19}$")
RESERVED = {"TOTAL", "ROWS", "CATEGORY"}   # FedEx 합계표의 다른 컬럼 이름과 겹치지 않게


def _key(text):
    return (text or "").strip().upper()


def _to_int(value):
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _sorted(rows, num_col):
    return sorted(rows, key=lambda r: (_to_int(r[num_col]) is None, _to_int(r[num_col]) or 0, r["row"]))


def _insert(items, item, position):
    """position(1부터)에 넣기, 없으면 맨 뒤"""
    pos = len(items) if position is None else max(0, min(int(position) - 1, len(items)))
    items.insert(pos, item)


# ── Channel 목록 (Channels) ───────────────────────────────────────────
def read_channels():
    """(탭, Channel 이름 목록 (Order 순, RED 항상 포함), 시트에 있던 줄 수)"""
    ws = gsheet.worksheet(CHANNELS_TAB, len(CH_HEADERS))
    created, rows = gsheet.read_table(ws, CH_HEADERS)
    if created:   # 처음: RED + 이미 규칙에서 쓰고 있는 Channel
        _, rules, _ = _raw_rules()
        names = list(dict.fromkeys(c for c in (_key(r["Channel"]) for r in rules) if c and c != DEFAULT_CHANNEL))
        names.append(DEFAULT_CHANNEL)
        rows = [{"Order": i, "Channel": n, "Updated At": gsheet.now(), "Updated By": "system"}
                for i, n in enumerate(names, start=1)]
        gsheet.write_rows(ws, CH_HEADERS, rows, 0)
        return ws, names, len(rows)
    names = list(dict.fromkeys(_key(r["Channel"]) for r in _sorted(rows, "Order") if _key(r["Channel"])))
    if DEFAULT_CHANNEL not in names:
        names.append(DEFAULT_CHANNEL)
    return ws, names, len(rows)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_channels():
    """FedEx 화면용 Channel 이름 (Order 순)"""
    return tuple(read_channels()[1])


def validate_channels(names, rules):
    """Channel 목록 확인 → 오류 메시지 목록"""
    errors, seen = [], set()
    for i, n in enumerate(names, start=1):
        if not CHANNEL_RE.match(n):
            errors.append(f"Row {i}: '{n}' — use 1–20 letters, numbers, space, '-' or '_'.")
        elif n in RESERVED:
            errors.append(f"Row {i}: '{n}' cannot be used as a channel name.")
        elif n in seen:
            errors.append(f"Row {i}: '{n}' is duplicated.")
        seen.add(n)
    if DEFAULT_CHANNEL not in seen:
        errors.append(f"{DEFAULT_CHANNEL} is the default channel and cannot be removed.")
    used = {_key(r["Channel"]) for r in rules}
    for n in sorted(used - seen):
        errors.append(f"'{n}' is used by a rule. Change or delete those rules first.")
    return errors


def save_channels(names, by):
    ws, old, old_count = read_channels()
    _, raw_rows = gsheet.read_table(ws, CH_HEADERS)
    old_info = {_key(r["Channel"]): r for r in raw_rows}
    out = []
    for i, n in enumerate(names, start=1):
        prev = old_info.get(n)
        same = prev and _to_int(prev["Order"]) == i
        out.append({"Order": i, "Channel": n,
                    "Updated At": prev["Updated At"] if same else gsheet.now(),
                    "Updated By": prev["Updated By"] if same else by})
    gsheet.write_rows(ws, CH_HEADERS, out, old_count)
    gsheet.ensure_dropdown(_rules_ws(), RULE_HEADERS.index("Channel"), names)   # 규칙 탭 드롭다운도 새 목록으로
    load_channels.clear()
    load_rules.clear()


def apply_channel_change(names, rules, action, name, order=None):
    """Channel 목록에 요청 하나 적용 → (새 목록, 오류 메시지 또는 None)"""
    names, name = list(names), _key(name)
    if action == ADD_CH:
        if name in names:
            return names, f"Channel '{name}' already exists."
        if not CHANNEL_RE.match(name):
            return names, f"'{name}' — use 1–20 letters, numbers, space, '-' or '_'."
        if name in RESERVED:
            return names, f"'{name}' cannot be used as a channel name."
        _insert(names, name, order)
        return names, None
    if action == DEL_CH:
        if name not in names:
            return names, f"Channel '{name}' does not exist."
        if name == DEFAULT_CHANNEL:
            return names, f"{DEFAULT_CHANNEL} is the default channel and cannot be deleted."
        users = [r["Keyword"] for r in rules if _key(r["Channel"]) == name]
        if users:
            return names, f"Channel '{name}' is used by rule(s): {', '.join(users)}. Change or delete them first."
        names.remove(name)
        return names, None
    return names, f"Unknown action '{action}'."


# ── 규칙 (Channel Rules) ─────────────────────────────────────────────
def _rules_ws():
    return gsheet.worksheet(RULES_TAB, len(RULE_HEADERS))


def _raw_rules():
    ws = _rules_ws()
    _, rows = gsheet.read_table(ws, RULE_HEADERS)
    return ws, _sorted([r for r in rows if r["Keyword"].strip()], "Priority"), len(rows)


def read_rules(channels=None):
    """(탭, 현재 규칙 [{Priority, Keyword, Channel, Updated At, Updated By}] (Priority 순), 시트에 있던 줄 수)
    channels를 주면 Channel 컬럼 드롭다운도 그 목록으로 맞춤"""
    ws, rules, n = _raw_rules()
    if channels:
        gsheet.ensure_dropdown(ws, RULE_HEADERS.index("Channel"), list(channels))
    return ws, rules, n


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_rules():
    """FedEx 화면용: ((키워드, Channel), ...) — Priority 순. 목록에 없는 Channel의 규칙은 건너뜀"""
    channels = set(load_channels())
    _, rules, _ = _raw_rules()
    return tuple((r["Keyword"].strip(), _key(r["Channel"])) for r in rules if _key(r["Channel"]) in channels)


def save_rules(rules, by):
    """규칙 목록을 순서대로 Priority 1, 2, 3 … 으로 저장. 내용이 바뀐 규칙만 Updated At / By 갱신"""
    ws, old, old_count = _raw_rules()
    old_by_key = {_key(r["Keyword"]): r for r in old}
    out = []
    for i, r in enumerate(rules, start=1):
        prev = old_by_key.get(_key(r["Keyword"]))
        same = prev and _key(prev["Channel"]) == _key(r["Channel"]) and _to_int(prev["Priority"]) == i
        out.append({"Priority": i, "Keyword": r["Keyword"].strip(), "Channel": _key(r["Channel"]),
                    "Updated At": prev["Updated At"] if same else gsheet.now(),
                    "Updated By": prev["Updated By"] if same else by})
    gsheet.write_rows(ws, RULE_HEADERS, out, old_count)
    load_rules.clear()


def validate_rules(rules, channels):
    """[{Keyword, Channel}] 확인 → 오류 메시지 목록"""
    errors, seen = [], set()
    for i, r in enumerate(rules, start=1):
        k = _key(r.get("Keyword"))
        if not k:
            errors.append(f"Row {i}: keyword is empty.")
        elif k in seen:
            errors.append(f"Row {i}: keyword '{r['Keyword'].strip()}' is duplicated.")
        seen.add(k)
        if _key(r.get("Channel")) not in channels:
            errors.append(f"Row {i}: channel must be one of {', '.join(channels)}.")
    return errors


def apply_change(rules, action, keyword, channel=None, priority=None):
    """규칙 목록에 요청 하나 적용 → (새 목록, 오류 메시지 또는 None). 원래 목록은 바꾸지 않음"""
    rules = [dict(r) for r in rules]
    idx = next((i for i, r in enumerate(rules) if _key(r["Keyword"]) == _key(keyword)), None)
    if action == ADD:
        if idx is not None:
            return rules, f"Keyword '{keyword}' already exists."
        _insert(rules, {"Keyword": keyword.strip(), "Channel": _key(channel)}, priority)
    elif action == CHANGE:
        if idx is None:
            return rules, f"Keyword '{keyword}' does not exist."
        rule = rules.pop(idx)
        rule["Channel"] = _key(channel) or rule["Channel"]
        _insert(rules, rule, idx + 1 if priority is None else priority)
    elif action == DELETE:
        if idx is None:
            return rules, f"Keyword '{keyword}' does not exist."
        rules.pop(idx)
    else:
        return rules, f"Unknown action '{action}'."
    return rules, None


def describe(action, keyword, channel=None, priority=None):
    """요청 한 줄 설명"""
    where = f" at {'order' if action in CHANNEL_ACTIONS else 'priority'} {priority}" \
        if priority not in (None, "") else ""
    return {
        ADD: f"Add keyword '{keyword}' → {channel}{where}",
        CHANGE: f"Change keyword '{keyword}' → {channel}{where}",
        DELETE: f"Delete keyword '{keyword}'",
        ADD_CH: f"Add channel '{channel}'{where}",
        DEL_CH: f"Delete channel '{channel}'",
    }.get(action, action)


# ── 요청 (Rule Requests) ─────────────────────────────────────────────
def _requests_ws():
    ws = gsheet.worksheet(REQUESTS_TAB, len(REQ_HEADERS))
    gsheet.ensure_dropdown(ws, REQ_HEADERS.index("Status"), STATUSES)
    return ws


def read_requests():
    ws = _requests_ws()
    _, rows = gsheet.read_table(ws, REQ_HEADERS)
    return ws, rows


def _check(action, keyword, channel, priority):
    """지금 시트 기준으로 적용 가능한 요청인지 → (Channel 목록, 규칙 목록, 새 목록, 오류)"""
    _, channels, _ = read_channels()
    _, rules, _ = _raw_rules()
    if action in CHANNEL_ACTIONS:
        new, err = apply_channel_change(channels, rules, action, channel, priority)
    else:
        if not (keyword or "").strip():
            return channels, rules, None, "Please enter a keyword."
        if action != DELETE and _key(channel) not in channels:
            return channels, rules, None, "Please choose a channel from the list."
        new, err = apply_change(rules, action, keyword, channel, priority)
    return channels, rules, new, err


def submit_request(username, action, keyword, channel, priority, reason):
    """사용자 요청 저장 → 오류 메시지 또는 None"""
    if action in CHANNEL_ACTIONS and not _key(channel):
        return "Please enter a channel name."
    *_, err = _check(action, keyword, channel, priority)
    if err:
        return err
    ws, _ = read_requests()
    ws.append_row([gsheet.now(), username, action,
                   "" if action in CHANNEL_ACTIONS else (keyword or "").strip(),
                   "" if action == DELETE else _key(channel),
                   "" if priority is None else int(priority), (reason or "").strip(), "", "", PENDING])
    return None


def review_request(req, approve, admin):
    """요청 승인(바로 반영) 또는 거절 → 오류 메시지 또는 None"""
    if approve:
        _, _, new, err = _check(req["Action"], req["Keyword"], req["Channel"] or None, _to_int(req["Priority"]))
        if err:
            return f"Cannot apply: {err}"
        by = f"{admin} (request by {req['Requested By']})"
        if req["Action"] in CHANNEL_ACTIONS:
            save_channels(new, by)
        else:
            save_rules(new, by)
    c = REQ_HEADERS.index
    _requests_ws().batch_update([
        {"range": f"{gsheet.col_letter(c('Reviewed At'))}{req['row']}:{gsheet.col_letter(c('Status'))}{req['row']}",
         "values": [[gsheet.now(), admin, APPROVED if approve else REJECTED]]},
    ])
    return None

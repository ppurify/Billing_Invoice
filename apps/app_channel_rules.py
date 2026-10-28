"""
Channel Rules 페이지 (Channel 목록 · 키워드 구분 규칙)

  사용자 : Channel 목록 · 규칙 보기 / 키워드 · Channel 추가 · 변경 · 삭제 요청 / 내 요청 상태 보기
  관리자 : 요청 승인 · 거절 (승인하면 바로 반영) / Channel 목록 · 규칙 직접 수정

저장 · 적용 방식은 components/channel_rules.py 참고
"""
import pandas as pd
import streamlit as st

from billing.fedex_parser import classify_channel
from components.theme import apply_theme
from components.auth import current_user
from components import channel_rules as cr

apply_theme()
st.title("Channel Rules")
st.markdown("<br>", unsafe_allow_html=True)

user = current_user()
is_admin = bool(user and user.get("is_admin"))
EDITOR_VER = "rules_editor_ver"

st.caption("Rules are checked from the top (Priority 1 first). The first keyword found in "
           "P.O.# / Ref.#2 / Cust. Ref. decides the channel (case-insensitive). "
           f"No match → {cr.DEFAULT_CHANNEL}. Other Charges are always {cr.DEFAULT_CHANNEL}.")

try:
    _, channels, _ = cr.read_channels()
    _, rules, _ = cr.read_rules(channels)
    _, requests = cr.read_requests()
except Exception as e:
    st.error(f"❌ Cannot read the rules sheet: {e}")
    st.stop()

ACTION_LABELS = {
    cr.ADD: "Add a keyword", cr.CHANGE: "Change a keyword", cr.DELETE: "Delete a keyword",
    cr.ADD_CH: "Add a channel", cr.DEL_CH: "Delete a channel",
}


def _rules_df(rules):
    return pd.DataFrame([{"Priority": i, "Keyword": r["Keyword"], "Channel": r["Channel"].strip().upper(),
                          "Updated At": r["Updated At"], "Updated By": r["Updated By"]}
                         for i, r in enumerate(rules, start=1)])


def _channels_df(channels, rules):
    used = pd.Series([r["Channel"].strip().upper() for r in rules]).value_counts() if rules else {}
    return pd.DataFrame([{"Order": i, "Channel": c, "Rules": int(used.get(c, 0)),
                          "Note": "default (no match · Other Charges)" if c == cr.DEFAULT_CHANNEL else ""}
                         for i, c in enumerate(channels, start=1)])


def _requests_df(rows):
    cols = ["Requested At", "Requested By", "Action", "Keyword", "Channel", "Priority", "Reason",
            "Reviewed At", "Reviewed By", "Status"]
    df = pd.DataFrame(rows, columns=cols + ["row"]) if rows else pd.DataFrame(columns=cols)
    return df[cols].rename(columns={"Priority": "Priority / Order"}).iloc[::-1]   # 최신 요청이 위


def _sorted_rows(edited, num_col):
    """편집표 → 번호 순서대로 정렬 (번호 비운 줄은 맨 뒤, 같은 번호는 표 순서)"""
    e = edited.dropna(how="all").copy()
    e["_order"] = range(len(e))
    e["_n"] = pd.to_numeric(e[num_col], errors="coerce")
    return e.sort_values(["_n", "_order"], na_position="last")


def _done(msg):
    st.session_state["rules_flash"] = msg
    st.session_state[EDITOR_VER] = st.session_state.get(EDITOR_VER, 0) + 1
    st.rerun()


if msg := st.session_state.pop("rules_flash", None):
    st.success(msg)
ver = st.session_state.get(EDITOR_VER, 0)

# ==============================================================================
# 관리자
# ==============================================================================
if is_admin:
    pending = [r for r in requests if r["Status"].strip().capitalize() == cr.PENDING]
    st.markdown(f"### 📬 Pending Requests ({len(pending)})")
    if not pending:
        st.caption("No pending requests.")
    for req in pending:
        with st.container(border=True):
            c1, c2, c3 = st.columns([5, 1, 1], vertical_alignment="center")
            c1.markdown(f"**{cr.describe(req['Action'], req['Keyword'], req['Channel'], req['Priority'])}**")
            c1.caption(f"by {req['Requested By']} · {req['Requested At']}"
                       + (f" · Reason: {req['Reason']}" if req["Reason"].strip() else ""))
            for col, label, approve in ((c2, "✅ Approve", True), (c3, "✖️ Reject", False)):
                if col.button(label, key=f"req_{'ok' if approve else 'no'}_{req['row']}", width="stretch"):
                    try:
                        err = cr.review_request(req, approve, user["username"])
                    except Exception as e:
                        err = str(e)
                    if err:
                        st.error(f"❌ {err}")
                    else:
                        _done("✅ Approved and applied." if approve else "Request rejected.")

    # --- Channel 목록 ---
    st.markdown("---")
    st.markdown("### 🗂️ Edit Channels")
    st.caption(f"Order = column order in the FedEx summary. {cr.DEFAULT_CHANNEL} is the default channel "
               "and cannot be removed. A channel used by a rule cannot be removed.")
    ch_edited = st.data_editor(
        _channels_df(channels, rules)[["Order", "Channel"]], key=f"channels_editor_{ver}",
        num_rows="dynamic", width="stretch", hide_index=True,
        column_config={"Order": st.column_config.NumberColumn(min_value=1, step=1, help="Empty = last"),
                       "Channel": st.column_config.TextColumn(required=True, help="1–20 letters / numbers")},
    )
    if st.button("💾 Save channels", type="primary", key="channels_save"):
        new_channels = [str(r.Channel or "").strip().upper() for r in _sorted_rows(ch_edited, "Order").itertuples()]
        errors = cr.validate_channels(new_channels, rules)
        if errors:
            st.error("❌ " + " / ".join(errors))
        else:
            try:
                cr.save_channels(new_channels, by=user["username"])
                _done(f"✅ Saved {len(new_channels)} channel(s).")
            except Exception as ex:
                st.error(f"❌ Cannot save: {ex}")

    # --- 규칙 ---
    st.markdown("---")
    st.markdown("### ✏️ Edit Rules")
    st.caption("Edit, add (bottom row) or delete (select row → 🗑) rules, then Save. "
               "Rows are saved in Priority order and renumbered 1, 2, 3 …  "
               "To use a new channel, add it in Edit Channels first.")
    df = _rules_df(rules)[["Priority", "Keyword", "Channel"]] if rules else \
        pd.DataFrame(columns=["Priority", "Keyword", "Channel"])
    edited = st.data_editor(
        df, key=f"rules_editor_{ver}", num_rows="dynamic", width="stretch", hide_index=True,
        column_config={
            "Priority": st.column_config.NumberColumn(min_value=1, step=1,
                                                      help="Lower = checked first. Empty = last."),
            "Keyword": st.column_config.TextColumn(required=True),
            "Channel": st.column_config.SelectboxColumn(options=channels, required=True),
        },
    )
    if st.button("💾 Save rules", type="primary", key="rules_save"):
        new_rules = [{"Keyword": str(r.Keyword or "").strip(), "Channel": r.Channel}
                     for r in _sorted_rows(edited, "Priority").itertuples()]
        errors = cr.validate_rules(new_rules, channels)
        if errors:
            st.error("❌ " + " / ".join(errors))
        else:
            try:
                cr.save_rules(new_rules, by=user["username"])
                _done(f"✅ Saved {len(new_rules)} rule(s).")
            except Exception as ex:
                st.error(f"❌ Cannot save: {ex}")

    with st.expander("📜 All requests"):
        st.dataframe(_requests_df(requests), width="stretch", hide_index=True)

# ==============================================================================
# 사용자
# ==============================================================================
else:
    c1, c2 = st.columns([1, 3])
    with c1:
        st.markdown("### 🗂️ Channels")
        st.dataframe(_channels_df(channels, rules), width="stretch", hide_index=True)
    with c2:
        st.markdown("### 📋 Current Rules")
        if rules:
            st.dataframe(_rules_df(rules), width="stretch", hide_index=True)
        else:
            st.caption(f"No rules. Every shipment is {cr.DEFAULT_CHANNEL}.")

    st.markdown("---")
    st.markdown("### 📝 Request a Change")
    st.caption("Your request is applied after the administrator approves it.")
    action = st.radio("Request", cr.ACTIONS, horizontal=True, key="rq_action", format_func=ACTION_LABELS.get)
    keyword, channel, priority = None, None, None
    k1, k2, k3 = st.columns([2, 1, 1])
    if action == cr.ADD:
        keyword = k1.text_input("Keyword", key="rq_keyword_new", help="Text to find in P.O.# / Ref.#2 / Cust. Ref.")
    elif action in (cr.CHANGE, cr.DELETE):
        keyword = k1.selectbox("Keyword", [r["Keyword"] for r in rules], index=None, key="rq_keyword_old",
                               placeholder="Choose a keyword")
    if action in (cr.ADD, cr.CHANGE):
        channel = k2.selectbox("Channel", channels, index=None, key="rq_channel", placeholder="Choose",
                               help="Need a new channel? Request 'Add a channel' first.")
        priority = k3.number_input("Priority (optional)", min_value=1, step=1, value=None, key="rq_priority",
                                   help="1 = checked first. Empty: Add → last, Change → keep position")
    elif action == cr.ADD_CH:
        channel = k1.text_input("New channel name", key="rq_channel_new", help="1–20 letters / numbers")
        priority = k2.number_input("Order (optional)", min_value=1, step=1, value=None, key="rq_order",
                                   help="Column position in the FedEx summary. Empty = last")
    elif action == cr.DEL_CH:
        channel = k1.selectbox("Channel", [c for c in channels if c != cr.DEFAULT_CHANNEL], index=None,
                               key="rq_channel_old", placeholder="Choose a channel",
                               help="Only channels not used by any rule can be deleted.")
    reason = st.text_input("Reason", key="rq_reason", placeholder="Why is this change needed?")
    if st.button("📨 Send request", type="primary", key="rq_send"):
        try:
            err = cr.submit_request(user["username"], action, keyword, channel, priority, reason)
        except Exception as e:
            err = str(e)
        if err:
            st.error(f"❌ {err}")
        else:
            for k in ("rq_keyword_new", "rq_keyword_old", "rq_channel", "rq_priority", "rq_channel_new",
                      "rq_order", "rq_channel_old", "rq_reason"):
                st.session_state.pop(k, None)
            _done("✅ Request sent. It will be applied after approval.")

    st.markdown("---")
    st.markdown("### 📮 My Requests")
    mine = [r for r in requests if r["Requested By"].strip().lower() == user["username"].lower()]
    if mine:
        st.dataframe(_requests_df(mine).drop(columns=["Requested By"]), width="stretch", hide_index=True)
    else:
        st.caption("No requests yet.")

# ==============================================================================
# 공통: 규칙 테스트
# ==============================================================================
st.markdown("---")
with st.expander("🧪 Test a reference"):
    sample = st.text_input("P.O.# / Ref.#2 / Cust. Ref. text", key="rules_test",
                           placeholder="Paste a reference from the invoice")
    if sample:
        rule_pairs = [(r["Keyword"].strip(), r["Channel"].strip().upper()) for r in rules
                      if r["Channel"].strip().upper() in channels]
        hit = next((k for k, _ in rule_pairs if k and k.upper() in sample.upper()), None)
        ch = classify_channel(sample, rule_pairs)
        st.markdown(f"→ **{ch}**" + (f" (matched keyword '{hit}')" if hit else " (no keyword matched)"))

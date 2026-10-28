"""
User Approval 페이지 (관리자만 보임)

가입 요청 목록에서 Status를 바꾸고 'Save'를 누르면 시트에 저장됩니다.
  Pending  → 승인 대기 (로그인 불가)
  Approved → 로그인 가능
  Rejected → 로그인 불가 (승인 취소도 이걸로)
"""
import pandas as pd
import streamlit as st

from components.theme import apply_theme
from components.auth import current_user, load_users, set_statuses, STATUSES, PENDING

apply_theme()
st.title("User Approval")
st.markdown("<br>", unsafe_allow_html=True)

user = current_user()
if not user or not user.get("is_admin"):
    st.error("❌ Only the administrator can open this page.")
    st.stop()

try:
    _, users = load_users()
except Exception as e:
    st.error(f"❌ Cannot read the Users sheet: {e}")
    st.stop()

if not users:
    st.info("No sign-up requests yet.")
    st.stop()

df = pd.DataFrame(users)[["row", "Name", "Username", "Requested At", "Reviewed At", "Status"]]
df["Status"] = df["Status"].str.strip().str.capitalize().where(lambda s: s.isin(STATUSES), PENDING)
df["_order"] = df["Status"] != PENDING                       # Pending을 맨 위로, 그 안에서는 최신순
df = df.sort_values(["_order", "Requested At"], ascending=[True, False]).drop(columns="_order").set_index("row")

n_pending = int((df["Status"] == PENDING).sum())
c1, c2 = st.columns([4, 1], vertical_alignment="center")
c1.metric("Waiting for approval", n_pending)
if c2.button("🔄 Refresh", width="stretch", key="approval_refresh"):
    st.rerun()

edited = st.data_editor(
    df, key="approval_editor", width="stretch", hide_index=True,
    disabled=["Name", "Username", "Requested At", "Reviewed At"],
    column_config={"Status": st.column_config.SelectboxColumn(
        "Status ✏️", options=STATUSES, required=True,
        help="Approved = can log in · Pending / Rejected = cannot log in")},
)
changes = {row: status for row, status in edited["Status"].items() if status != df.at[row, "Status"]}

if st.button(f"💾 Save ({len(changes)} change(s))", type="primary", disabled=not changes, key="approval_save"):
    try:
        set_statuses(changes)
        st.success(f"✅ Saved {len(changes)} change(s).")
        st.rerun()
    except Exception as e:
        st.error(f"❌ Cannot save: {e}")
st.caption("Change 'Status' and click Save. Users can log in once their status is Approved.")

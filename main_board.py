import streamlit as st

# Layout configuration (최상단 고정은 필수)
st.set_page_config(page_title="Spigen FedEx Billing", layout="wide")

from components.theme import apply_theme
from components.auth import render_login_page, render_sidebar_account, current_user
apply_theme()

# ==============================================================================
# 🚪 로그인 / 회원가입 (가입 → 관리자 승인 → 로그인 가능). components/auth.py 참고
# ==============================================================================
if render_login_page():
    pages = [st.Page(
        page="apps/app_fedex_billing.py",   # FedEx Billing Invoice PDF 파싱 페이지
        title="FedEx Billing Invoice",
        icon="🧾",
        default=True,
    )]
    pages.append(st.Page(page="apps/app_channel_rules.py", title="Channel Rules", icon="🏷️"))
    if current_user().get("is_admin"):      # 관리자에게만 보이는 승인 페이지
        pages.append(st.Page(page="apps/app_user_approval.py", title="User Approval", icon="✅"))

    pg = st.navigation({"Workspace Views": pages})
    render_sidebar_account()
    pg.run()

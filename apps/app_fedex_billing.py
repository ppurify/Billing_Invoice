"""
FedEx Billing Invoice 페이지
billing/fedex.py의 render_fedex_billing()을 호출합니다.
"""
import streamlit as st

from components.theme import apply_theme
from billing.fedex import render_fedex_billing

apply_theme()          # main_board.py에서 이미 호출했어도 문제 없음
st.title("FedEx Billing Invoice")
st.markdown("<br>", unsafe_allow_html=True)

try:
    render_fedex_billing()
except Exception as e:
    st.error(f"❌ FedEx page error: {e}")
    st.exception(e)

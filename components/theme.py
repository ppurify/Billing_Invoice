"""
대시보드 디자인 — 색 · 버튼 모양을 이 파일 하나에서 관리합니다
====================================================================
main_board.py에서 한 번 호출하면 모든 페이지에 적용됩니다.

    from components.theme import apply_theme
    apply_theme()

바꾸고 싶으면 아래 '설정'만 고치세요.
  - ACCENT        : 강조색 — 선택된 탭(UPS / FedEx / DHL …)의 글자, 버튼에 마우스를 올렸을 때 테두리 · 글자
  - BUTTON_BORDER : 버튼 테두리 색 (색은 없고 테두리만 있는 버튼 — Data Explorer의 SAP Sync 버튼과 같은 모양)
  - BUTTON_RADIUS : 버튼 모서리 둥글기
'📁 Save into folder' 버튼(components/folder_save.py)도 이 설정을 그대로 씁니다.

※ .streamlit/config.toml의 [theme]은 쓰지 않습니다 (색은 여기서만 관리).
  체크박스 · 토글처럼 이 파일에서 따로 정하지 않은 곳은 Streamlit 기본색(빨강 #FF4B4B)을 씁니다.
"""
import streamlit as st

# ── 설정 ──────────────────────────────────────────────────────────────
ACCENT = "#FF4B4B"                          # 강조색 (Streamlit 기본 빨강)
BUTTON_BORDER = "rgba(128, 128, 128, 0.4)"  # 테두리만 있는 버튼의 테두리 (밝은/어두운 테마 모두 보이는 회색)
BUTTON_RADIUS = "0.5rem"
TEXT_LIGHT_THEME = "#31333F"                # 밝은 테마 글자색
TEXT_DARK_THEME = "#FAFAFA"                 # 어두운 테마 글자색


# ── 대시보드 화면용 CSS ────────────────────────────────────────────────
def _page_css():
    return f"""
<style>
/* 모든 Primary 버튼(📤 Upload to monday, 📥 Download & classify, 🔍 Load invoices …)
   → 색 없이 테두리만 (SAP Sync 같은 기본 버튼 모양). 글자는 테마 글자색을 따라감 */
[data-testid^="stBaseButton-primary"] {{
    background-color: transparent !important;
    border: 1px solid {BUTTON_BORDER} !important;
    color: inherit !important;
    border-radius: {BUTTON_RADIUS} !important;
}}
[data-testid^="stBaseButton-primary"]:hover {{
    border-color: {ACCENT} !important;
    color: {ACCENT} !important;
}}
[data-testid^="stBaseButton-primary"]:disabled {{
    opacity: 0.45 !important;
    border-color: {BUTTON_BORDER} !important;
    color: inherit !important;
}}
[data-testid^="stBaseButton-primary"] p {{ color: inherit !important; }}

/* 탭 (Billing Invoice의 UPS / FedEx / DHL 등, 모든 페이지): 선택된 탭 글자만 강조색. 나머지 탭은 기본 회색 */
[role="tab"][aria-selected="true"],
[role="tab"][aria-selected="true"] p {{ color: {ACCENT} !important; }}
</style>
"""


# ── 'Save into folder'(iframe 안) 버튼용 CSS ──────────────────────────
#   iframe은 대시보드 CSS가 안 닿아서 따로 넣음. 글자색은 folder_save.py가 테마에 맞춰 정함
def iframe_button_css():
    return f"""
  button {{ font: inherit; font-size: 14px; padding: 0.4rem 0.8rem; border-radius: {BUTTON_RADIUS}; cursor: pointer;
           background: transparent; color: inherit; border: 1px solid {BUTTON_BORDER}; }}
  button:hover {{ border-color: {ACCENT}; color: {ACCENT}; }}
  button:disabled {{ opacity: 0.45; cursor: default; border-color: {BUTTON_BORDER}; color: inherit; }}
"""


def apply_theme():
    st.markdown(_page_css(), unsafe_allow_html=True)
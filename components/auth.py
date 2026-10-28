"""
로그인 · 회원가입 · 승인 (가입 정보는 Google Sheet 'Users' 탭에 저장)

동작
  - Sign up  : 가입하면 Status = Pending 으로 시트에 저장 → 승인 전에는 로그인 불가
  - 승인      : 관리자가 'User Approval' 페이지에서 Status를 Approved로 바꾸면 로그인 가능
               (시트의 Status 칸을 직접 'Approved'로 바꿔도 됨)
  - 관리자    : Streamlit Secrets의 [admin] 계정 (시트에 저장하지 않음)

비밀번호는 bcrypt 해시로만 저장합니다. 시트를 열어도 실제 비밀번호는 알 수 없습니다.
시트 연결은 components/gsheet.py (Channel Rules 탭과 같은 스프레드시트)

※ 시트 ID · 서비스 계정 · 관리자 계정은 코드에 쓰지 않고 Streamlit Secrets에서만 읽습니다.
   (저장소가 public이어도 노출되지 않음)

Secrets 형식
  [admin]
  username = "..."
  password = "..."

  [users_sheet]
  spreadsheet_id = "..."
  worksheet = "Users"          # 생략하면 Users

  [gcp_service_account]
  type = "service_account"
  ...                           # 서비스 계정 JSON 내용 그대로
"""
import re

import bcrypt
import streamlit as st

from components import gsheet

# ── 시트 양식 ─────────────────────────────────────────────────────────
HEADERS = ["Name", "Username", "Password Hash", "Requested At", "Reviewed At", "Status"]
COL = {name: i for i, name in enumerate(HEADERS)}          # 컬럼 이름 → 0부터 시작하는 번호
PENDING, APPROVED, REJECTED = "Pending", "Approved", "Rejected"
STATUSES = [PENDING, APPROVED, REJECTED]

USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,30}$")
MIN_PASSWORD_LEN = 8

SESSION_USER = "auth_user"   # 로그인한 사용자 {"username", "name", "is_admin"}


def _worksheet():
    return gsheet.worksheet(st.secrets["users_sheet"].get("worksheet", "Users"), len(HEADERS))


def _norm(username):
    return (username or "").strip().lower()


def load_users():
    """시트의 모든 사용자 → (탭, [{컬럼: 값, 'row': 시트 행 번호}])
    시트가 비어 있으면 첫 줄에 양식(HEADERS)을 자동으로 채우고, Status는 드롭다운으로 설정합니다."""
    ws = _worksheet()
    _, users = gsheet.read_table(ws, HEADERS)
    gsheet.ensure_dropdown(ws, COL["Status"], STATUSES)
    return ws, [u for u in users if u["Username"].strip()]


def _find(users, username):
    key = _norm(username)
    return next((u for u in users if _norm(u["Username"]) == key), None)


# ── 회원가입 · 로그인 · 승인 ─────────────────────────────────────────
def sign_up(username, name, password, password2):
    """성공하면 None, 실패하면 오류 메시지"""
    username, name = _norm(username), (name or "").strip()
    if not USERNAME_RE.match(username):
        return "Username must be 3–30 characters: letters, numbers, '.', '_' or '-'."
    if not name:
        return "Please enter your name."
    if len(password) < MIN_PASSWORD_LEN:
        return f"Password must be at least {MIN_PASSWORD_LEN} characters."
    if password != password2:
        return "Passwords do not match."
    if username == _norm(st.secrets["admin"]["username"]):
        return "This username is not available."
    ws, users = load_users()
    if _find(users, username):
        return "This username is already registered."
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    row = {"Name": name, "Username": username, "Password Hash": pw_hash,
           "Requested At": gsheet.now(), "Reviewed At": "", "Status": PENDING}
    ws.append_row([row[h] for h in HEADERS])   # RAW 입력 (수식으로 해석 안 됨)
    return None


def log_in(username, password):
    """성공하면 (사용자 dict, None), 실패하면 (None, 오류 메시지)"""
    admin = st.secrets["admin"]
    if _norm(username) == _norm(admin["username"]):
        if password == admin["password"]:
            return {"username": admin["username"], "name": "Admin", "is_admin": True}, None
        return None, "Invalid username or password."

    _, users = load_users()
    user = _find(users, username)
    ok = bool(user) and bcrypt.checkpw(password.encode(), user["Password Hash"].encode())
    if not ok:
        return None, "Invalid username or password."
    status = user["Status"].strip().capitalize()
    if status == PENDING:
        return None, "Your account is waiting for approval."
    if status != APPROVED:
        return None, "Your account is not approved. Please contact the administrator."
    return {"username": user["Username"], "name": user["Name"], "is_admin": False}, None


def set_statuses(changes):
    """changes: {시트 행 번호: 새 Status} → Status와 Reviewed At을 한 번에 저장"""
    if not changes:
        return
    ws = _worksheet()
    now = gsheet.now()
    s, t = gsheet.col_letter(COL["Status"]), gsheet.col_letter(COL["Reviewed At"])
    data = []
    for row, status in changes.items():
        data.append({"range": f"{s}{row}", "values": [[status]]})
        data.append({"range": f"{t}{row}", "values": [[now]]})
    ws.batch_update(data)


# ── 화면 ─────────────────────────────────────────────────────────────
def current_user():
    return st.session_state.get(SESSION_USER)


def log_out():
    st.session_state.pop(SESSION_USER, None)
    st.rerun()


def render_login_page():
    """로그인 / 회원가입 화면. 로그인하면 True"""
    if current_user():
        return True

    _, center, _ = st.columns([1, 2, 1])
    with center:
        st.markdown("<br><br><br>", unsafe_allow_html=True)
        st.subheader("🔒 [Spigen] FedEx Billing Login")
        tab_login, tab_signup = st.tabs(["Log in", "Sign up"])

        with tab_login:
            with st.form("auth_login_form", border=False):
                username = st.text_input("Username (ID)")
                password = st.text_input("Password", type="password")
                submitted = st.form_submit_button("Log in", width="stretch")
            if submitted:
                try:
                    user, err = log_in(username, password)
                except Exception as e:
                    user, err = None, f"Cannot check the account right now: {e}"
                if user:
                    st.session_state[SESSION_USER] = user
                    st.rerun()
                st.error(f"❌ {err}")

        with tab_signup:
            with st.form("auth_signup_form", border=False, clear_on_submit=False):
                new_user = st.text_input("Username (ID)", help="3–30 characters: letters, numbers, . _ -")
                new_name = st.text_input("Name")
                new_pw = st.text_input("Password", type="password",
                                       help=f"At least {MIN_PASSWORD_LEN} characters")
                new_pw2 = st.text_input("Confirm password", type="password")
                submitted = st.form_submit_button("Request account", width="stretch")
            if submitted:
                try:
                    err = sign_up(new_user, new_name, new_pw, new_pw2)
                except Exception as e:
                    err = f"Cannot save the request right now: {e}"
                if err:
                    st.error(f"❌ {err}")
                else:
                    st.success("✅ Request sent. You can log in after the administrator approves it.")
    return False


def render_sidebar_account():
    user = current_user()
    if not user:
        return
    with st.sidebar:
        st.caption(f"👤 {user['name']} ({user['username']})")
        if st.button("Log out", width="stretch", key="auth_logout"):
            log_out()

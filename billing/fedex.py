"""
FedEx Billing Invoice 페이지 (새 앱 전용)

FedEx Invoice PDF 업로드 → 섹션별 추출 → IBD / IBA / RED 합계
apps/app_fedex_billing.py 에서 render_fedex_billing()을 호출합니다.

관련 파일
  billing/fedex_parser.py : FedEx PDF 파싱, Channel 구분

합계 기준
  Transportation (Express 운송, Ground, Other Charges(Pickup 등, 항상 RED)) : Total Charge
  Duty (Express 관세)                    : Total Duties, Tax, Customs, Other Fees - (Customs Duty + MPF)

Address Correction이 있는 Transportation 행은 빨간 글씨 + Notes에 메모

Channel 구분 규칙
  구글 시트 'Channel Rules' 탭의 규칙으로 구분합니다 (Channel Rules 페이지에서 요청 · 승인 · 수정).
  규칙이 바뀌면 PDF를 다시 읽지 않고 이미 읽은 참조 값으로 다시 구분합니다 (최대 1분 뒤 반영).
  규칙은 시트에만 있고 코드에는 없습니다. 시트를 못 읽으면 잘못된 합계가 나오지 않도록 결과를 보여주지 않습니다.

Channel 직접 수정
  Detail 표의 Channel 칸을 바꾸면 st.session_state['fedex_channel_overrides']에 저장되고,
  Other Charges by Channel 합계 · 두 Excel 다운로드에 모두 반영됩니다. (브라우저 세션 동안 유지)
  원래 자동 구분 값은 'Channel (Auto)'에 보관하고, 바꾼 행은 Notes에 표시합니다.

다운로드 (둘 다 'Download Excel' 버튼, 마우스를 올리면 내용 설명)
  Other Charges by Channel : 합계표 바로 위 오른쪽 — 화면의 합계표 그대로
  Detail                   : Detail의 Channel 필터와 같은 줄 오른쪽 끝
                             — Transportation · Duty 시트 2개, 현재 Channel 필터 적용

※ Spigen-dashboard의 Duty ↔ Shipment 구글 시트 비교 기능은 이 앱에는 없습니다.
"""
import hashlib
import io
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st

from billing.fedex_parser import (
    parse_fedex_invoice, TRANSPORTATION, DUTY, IBD, IBA, RED,
    DUTY_TOTAL_LABEL, OTHER_FEES_COL, ADDRESS_CORRECTION_COL,
    CHANNEL_REFS_COL, channel_for,
)
from components.channel_rules import load_rules

CHANNELS = [IBD, IBA, RED]
MONEY_COLS = ["Total Charge", "Customs Duty", "Merchandise Processing Fee",
              DUTY_TOTAL_LABEL, OTHER_FEES_COL, ADDRESS_CORRECTION_COL, "Billed Amount"]
FLAG_COL = "Address Correction?"          # 파서가 만든 True/False 컬럼 (화면에는 숨김)
RED_STYLE = "color: #e5484d; font-weight: 600;"
ROW_ID = "_rid"                            # 행 고유 ID (Invoice No.|순번) — Channel 수정 내용을 기억하는 키
AUTO_CHANNEL_COL = "Channel (Auto)"        # 파서가 자동으로 구분한 원래 Channel
OVERRIDES_KEY = "fedex_channel_overrides"  # {ROW_ID: 사용자가 고른 Channel}
EDITOR_VER_KEY = "fedex_editor_ver"        # 수정 반영 후 편집표를 새로 그리기 위한 번호
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


PAGE_CSS = """
<style>
/* Detail 필터(Channel)의 선택 항목 색: Streamlit 기본(빨간 배경) → 회색 (이 영역에만 적용) */
.st-key-fedex_filters [data-testid="stMultiSelectTagsContainer"] > span > span,
.st-key-fedex_filters span[data-baseweb="tag"] {
    background-color: rgba(128, 128, 128, 0.18) !important;
    color: inherit !important;
}
.st-key-fedex_filters [data-testid="stMultiSelectTagsContainer"] > span > span *,
.st-key-fedex_filters span[data-baseweb="tag"] * {
    color: inherit !important;
    fill: currentColor !important;
}

</style>
"""
# Detail 표: Type별로 필요한 컬럼만 표시 (Detail Excel도 같은 컬럼)
COMMON_COLS = ["Invoice No.", "Invoice Date", "Channel"]
DETAIL_COLS = {
    TRANSPORTATION: COMMON_COLS + ["Tracking ID", "Cust. Ref.", "Reference", "Total Charge",
                                   ADDRESS_CORRECTION_COL, "Notes", "Check"],
    DUTY: COMMON_COLS + ["Tracking ID", "Entry No.", "Cust. Ref.", "Reference", "Customs Duty",
                         "Merchandise Processing Fee", OTHER_FEES_COL, "Notes", "Check"],
}

# 합계표: (행 이름, 대상 Type, 합산할 컬럼)
SUMMARY_ROWS = [
    ("Transportation", TRANSPORTATION, "Total Charge"),
    ("Duty", DUTY, OTHER_FEES_COL),
]


# 파서 파일 내용이 바뀌면 캐시 키도 바뀌어서 자동으로 다시 분석 (예전 결과가 남지 않게)
_PARSER_VERSION = hashlib.md5(
    (Path(__file__).with_name("fedex_parser.py")).read_bytes()
).hexdigest()


@st.cache_data(show_spinner=False)
def _parse(file_bytes, parser_version):
    return parse_fedex_invoice(io.BytesIO(file_bytes))


def _to_mmddyyyy(value):
    """'Mar 16, 2026' / '2026-03-16' 등 → '03/16/2026' (날짜가 아니면 그대로)"""
    d = pd.to_datetime(value, errors="coerce")
    return d.strftime("%m/%d/%Y") if pd.notna(d) else (value or "")


def _money_config(cols):
    return {c: st.column_config.NumberColumn(format="$%.2f") for c in cols}


def _summary_records(df, invoice_no, invoice_date):
    """한 인보이스의 Category × Channel 합계 행들"""
    records = []
    for label, type_, col in SUMMARY_ROWS:
        sub = df[df["Type"] == type_]
        rec = {"Invoice No.": invoice_no, "Invoice Date": invoice_date, "Category": label, "Rows": len(sub)}
        for ch in CHANNELS:
            rec[ch] = round(sub.loc[sub["Channel"] == ch, col].fillna(0).sum(), 2)
        rec["Total"] = round(sum(rec[ch] for ch in CHANNELS), 2)
        records.append(rec)
    return records


def _channel_summary(df):
    """Invoice No.별 Category × Channel 합계표 — 인보이스에 있는 Type만 (Transportation / Duty)"""
    records = []
    for inv, g in df.groupby("Invoice No.", sort=False):   # 업로드한 순서대로
        records += _summary_records(g, inv, g["Invoice Date"].iloc[0])
    out = pd.DataFrame(records)
    return out[out["Rows"] > 0].reset_index(drop=True)   # 해당 Type이 없는 줄(0건)은 숨김


def _styled_detail(df, cols):
    """cols만 보여주고 Address Correction 행은 빨간 글씨 (화면 · Excel 공통)"""
    flags = df[FLAG_COL].astype(bool).values
    view = df[cols].copy()
    for c in MONEY_COLS:   # 빈 금액(None)을 숫자형 NaN으로 → 화면에 'None' 대신 빈칸
        if c in view.columns:
            view[c] = pd.to_numeric(view[c], errors="coerce")
    grid = np.broadcast_to(flags[:, None], view.shape)            # 행 단위 True/False → 모든 칸
    styles = pd.DataFrame(np.where(grid, RED_STYLE, ""), index=view.index, columns=view.columns)
    return (
        view.style
        .format({c: "${:,.2f}" for c in MONEY_COLS if c in view.columns}, na_rep="")
        .apply(lambda _: styles, axis=None)
    )


def _apply_channel_overrides(df):
    """세션에 저장된 Channel 수정 내용을 적용하고, 바꾼 행은 Notes에 표시"""
    overrides = st.session_state.setdefault(OVERRIDES_KEY, {})
    df[AUTO_CHANNEL_COL] = df["Channel"]
    edited = df[ROW_ID].isin(overrides.keys())
    if edited.any():
        df.loc[edited, "Channel"] = df.loc[edited, ROW_ID].map(overrides)
        note = "Channel edited (auto: " + df.loc[edited, AUTO_CHANNEL_COL] + ")"
        df.loc[edited, "Notes"] = np.where(df.loc[edited, "Notes"].fillna("") != "",
                                           df.loc[edited, "Notes"].fillna("") + " / " + note, note)
    return int(edited.sum())


def _save_channel_edits(original, edited):
    """편집표에서 바뀐 Channel → 세션에 저장. 바뀐 게 있으면 True"""
    overrides = st.session_state.setdefault(OVERRIDES_KEY, {})
    changed = edited["Channel"].ne(original["Channel"])
    for rid in edited.index[changed]:
        new, auto = edited.at[rid, "Channel"], original.at[rid, AUTO_CHANNEL_COL]
        if new == auto:
            overrides.pop(rid, None)      # 원래 값으로 되돌리면 수정 기록 삭제
        else:
            overrides[rid] = new
    return bool(changed.any())


def _rerun_with_fresh_editor():
    st.session_state[EDITOR_VER_KEY] = st.session_state.get(EDITOR_VER_KEY, 0) + 1
    st.rerun()


def _to_excel(sheets):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for name, df in sheets.items():
            df.to_excel(writer, index=False, sheet_name=name)   # DataFrame 또는 Styler(빨간 글씨 유지)
    return buf.getvalue()


# ==============================================================================
# FedEx 화면
# ==============================================================================
def render_fedex_billing():
    st.markdown(PAGE_CSS, unsafe_allow_html=True)

    uploaded_files = st.file_uploader(
        "Upload FedEx invoice PDF(s)", type=["pdf"], accept_multiple_files=True, key="fedex_uploader"
    )
    if not uploaded_files:
        st.info("📄 Upload one or more FedEx invoice PDFs (Express / Ground / Other Charges).")
        return

    summaries, frames = [], []
    for f in uploaded_files:
        try:
            with st.spinner(f"Reading {f.name}…"):
                summary, df = _parse(f.getvalue(), _PARSER_VERSION)
        except Exception as e:  # 파일 하나가 실패해도 나머지는 계속
            st.error(f"❌ {f.name}: {e}")
            continue

        total = summary["total_this_invoice"]
        diff = round((total or 0) - summary["sum_of_shipments"], 2)
        df = df.copy()
        df["Invoice Date"] = df["Invoice Date"].map(_to_mmddyyyy)   # 화면 표시 형식 MM/DD/YYYY
        df[ROW_ID] = f"{summary['invoice_no'] or f.name}|" + df.index.astype(str)
        summaries.append({
            "Invoice No.": summary["invoice_no"],
            "Invoice Date": _to_mmddyyyy(summary["invoice_date"]),
            "TOTAL THIS INVOICE": total,
            "Sum of Shipments": summary["sum_of_shipments"],
            "Difference": diff,
            "Shipments": summary["shipment_count"],
            "Status": "✅ Matched" if total is not None and abs(diff) < 0.005 else "⚠️ Check",
        })
        frames.append(df)

    if not frames:
        return

    df_all = pd.concat(frames, ignore_index=True)
    df_summary = pd.DataFrame(summaries)
    try:
        rules = load_rules()
    except Exception as e:
        st.error(f"❌ Cannot read Channel Rules from the sheet, so IBD / IBA / RED cannot be decided. "
                 f"Please try again in a moment. ({e})")
        return
    if not rules:
        st.warning("⚠️ There are no Channel Rules yet, so every row is RED. "
                   "Add rules on the 'Channel Rules' page.")
    df_all["Channel"] = [channel_for(sec, refs, rules)                      # 현재 규칙으로 다시 구분
                         for sec, refs in zip(df_all["Section"], df_all[CHANNEL_REFS_COL])]
    n_edited = _apply_channel_overrides(df_all)   # 직접 수정한 Channel 반영 (합계 · 다운로드 모두)
    file_stem = str(df_summary["Invoice No."].iloc[0]) if len(df_summary) == 1 else "fedex_invoices"

    # --- 📑 Invoice Summary ---
    st.markdown("### 📑 Invoice Summary")
    c1, c2, c3 = st.columns(3)
    c1.metric("Invoices", f"{len(df_summary)}")
    c2.metric("Rows", f"{len(df_all):,}")
    c3.metric("TOTAL THIS INVOICE (USD)", f"${df_summary['TOTAL THIS INVOICE'].fillna(0).sum():,.2f}")

    st.dataframe(
        df_summary, width="stretch", hide_index=True,
        column_config=_money_config(["TOTAL THIS INVOICE", "Sum of Shipments", "Difference"]),
    )
    if (df_summary["Status"] != "✅ Matched").any():
        st.warning("⚠️ For some invoices, the sum of extracted rows does not match TOTAL THIS INVOICE. "
                   "Please check the PDF.")
    n_ac = int(df_all[FLAG_COL].sum())
    if n_ac:
        st.error(f"🔴 {n_ac} transportation row(s) have an Address Correction. "
                 "They are shown in red in the Detail table (see 'Notes').")
    bad_rows = df_all[df_all["Check"] != "OK"]
    if not bad_rows.empty:
        st.warning(f"⚠️ {len(bad_rows)} row(s) could not be fully parsed. See the 'Check' column.")

    # --- 💼 IBD / IBA / RED 합계 ---
    st.markdown("---")
    st.markdown("### 💼 Other Charges by Channel")
    df_channel = _channel_summary(df_all)
    # 합계표 바로 위: 설명 ……… Download Excel (합계표)
    s1, s2 = st.columns([5, 1], vertical_alignment="bottom")
    s1.caption(
        "Transportation = Total Charge (incl. Other Charges such as pickup fees, always RED) · "
        f"Duty = {DUTY_TOTAL_LABEL} − (Customs Duty + Merchandise Processing Fee)"
    )
    s2.download_button(
        "⬇️ Download Excel",
        _to_excel({"By Channel": df_channel}),
        file_name=f"{file_stem}_channel_summary.xlsx", mime=XLSX_MIME,
        width="stretch", key="fedex_dl_summary",
        help="Other Charges by Channel table below "
             "(IBD / IBA / RED totals per invoice, manual Channel edits included).",
    )
    st.dataframe(
        df_channel, width="stretch", hide_index=True,
        column_config=_money_config(CHANNELS + ["Total"]),
    )
    if n_edited:
        e1, e2 = st.columns([5, 1], vertical_alignment="center")
        e1.caption(f"✏️ {n_edited} row(s) have a manually edited Channel (included above).")
        if e2.button("↩️ Reset", key="fedex_reset_channel", width="stretch",
                     help="Undo all manual Channel edits"):
            st.session_state[OVERRIDES_KEY] = {}
            _rerun_with_fresh_editor()

    # --- 🔍 상세 (Type별 탭, 필요한 컬럼만) ---
    st.markdown("---")
    st.markdown("### 🔍 Detail")
    # Channel 필터 ……… Download Excel (상세) — 버튼은 필터 입력칸과 같은 줄 맨 오른쪽
    f1, f2 = st.columns([5, 1], vertical_alignment="bottom")
    with f1.container(key="fedex_filters"):   # key → 'st-key-fedex_filters' 클래스 (CSS 적용 범위)
        channels = st.multiselect("Channel", CHANNELS, default=CHANNELS, key="fedex_channel_filter")
    df_ch = df_all[df_all["Channel"].isin(channels)]
    f2.download_button(
        "⬇️ Download Excel",
        _to_excel({t: _styled_detail(df_ch[df_ch["Type"] == t], DETAIL_COLS[t])
                   for t in (TRANSPORTATION, DUTY)}),
        file_name=f"{file_stem}_detail.xlsx", mime=XLSX_MIME,
        width="stretch", key="fedex_dl_detail",
        help="Detail table below: one sheet each for Transportation and Duty, "
             "with the current Channel filter and manual Channel edits applied. "
             "Address Correction rows stay red.",
    )

    types = [TRANSPORTATION, DUTY]
    tabs = st.tabs([f"{t} ({int((df_ch['Type'] == t).sum())})" for t in types])
    for tab, type_ in zip(tabs, types):
        with tab:
            df_t = df_ch[df_ch["Type"] == type_]
            if type_ == TRANSPORTATION:
                if st.toggle("Address Correction only", value=False, key="fedex_only_ac"):
                    df_t = df_t[df_t[FLAG_COL]]
            if df_t.empty:
                st.caption("No rows.")
                continue
            df_t = df_t.set_index(ROW_ID)
            cols = DETAIL_COLS[type_]
            edited = st.data_editor(
                _styled_detail(df_t, cols),
                key=f"fedex_editor_{type_}_{st.session_state.get(EDITOR_VER_KEY, 0)}",
                width="stretch", hide_index=True,
                disabled=[c for c in cols if c != "Channel"],          # Channel만 수정 가능
                column_config={"Channel": st.column_config.SelectboxColumn(
                    "Channel ✏️", options=CHANNELS, required=True,
                    help="Click to change. The change is reflected in 'Other Charges by Channel'.")},
            )
            if _save_channel_edits(df_t, edited):
                _rerun_with_fresh_editor()
            if type_ == TRANSPORTATION:
                st.caption("🔴 Red rows = Address Correction (see 'Notes'). ✏️ Channel can be edited.")
            elif type_ == DUTY:
                st.caption(f"{OTHER_FEES_COL} = {DUTY_TOTAL_LABEL} − (Customs Duty + MPF). "
                           "✏️ Channel can be edited.")
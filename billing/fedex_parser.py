"""
[FedEx] Invoice (PDF) 파서
(billing/fedex.py에서 사용)

- parse_fedex_invoice(pdf_file) -> (summary: dict, df_rows: DataFrame)
  pdf_file: 파일 경로 또는 파일 객체(Streamlit UploadedFile 등)

하나의 인보이스 안에 여러 섹션이 같이 들어 있을 수 있음 (예: Express + Other Charges)
→ 인보이스 단위가 아니라 '섹션' 단위로 규칙을 적용합니다.

섹션별 추출 항목
  FedEx Ground (P.O.# 블록)          : Tracking ID, Total Charge                       → Transportation
  FedEx Ground (Destination Zip 블록) : Shipment Total (Tracking ID 없음)               → Transportation
  FedEx Express (운송)               : Tracking ID, Total Charge / Total Transportation Charges → Transportation
  FedEx Express (관세)               : Tracking ID, Entry No., Customs Duty, MPF, Total Duties, Tax, Customs, Other Fees  → Duty
  FedEx Other Charges (Pickup)       : Total Charges                   → Transportation

Express 운송/관세 구분: 블록에 'Total Duties, Tax, Customs, Other Fees' 줄이 있으면 관세.

Address Correction
  Transportation 블록에 'Address Correction' 글자가 있으면 표시 (금액이 있으면 함께 추출)
  → 'Address Correction' 컬럼에 금액, 'Notes'에 메모, 화면에서는 해당 행을 빨간색으로 표시

Channel(IBD / IBA / RED)
  Other Charges → 항상 RED
  나머지 → P.O.# / Ref.#2 / Cust. Ref를 보고 (대소문자 무시)
           규칙 목록(키워드 → Channel)을 위에서부터 확인해서 처음 맞는 규칙의 Channel
           아무 규칙에도 안 맞으면 RED
  규칙은 구글 시트 'Channel Rules' 탭에만 있고 코드에는 없습니다 (components/channel_rules.py).
  규칙을 넘기지 않으면(이 파일만 단독 실행 등) 모두 RED.
  각 행에서 확인한 참조 값은 CHANNEL_REFS_COL(숨김 컬럼)에 남겨서, 규칙이 바뀌면 PDF를 다시 읽지 않고 재분류합니다.

처리 방식
  모든 페이지를 레이아웃 그대로 읽고, 페이지마다 반복되는 상단 헤더를 지운 뒤 이어 붙입니다.
  → 블록이 다음 페이지로 넘어가도('Tracking ID: ... continued') 한 블록으로 처리됩니다.
"""
import re
from datetime import datetime

import pandas as pd
from pypdf import PdfReader

AMOUNT = r"-?[\d,]+\.\d{2}"

# 섹션 · 유형 · Channel 이름
EXPRESS, GROUND, GROUND_MW, OTHER = "Express", "Ground", "Ground Multiweight", "Other Charges"
TRANSPORTATION, DUTY, OTHER_CHARGES = "Transportation", "Duty", "Other Charges"
IBD, IBA, RED = "IBD", "IBA", "RED"
CHANNEL_REFS_COL = "_channel_refs"   # Channel 구분에 쓴 참조 값 (화면 · Excel에는 안 나옴)

DUTY_TOTAL_LABEL = "Total Duties, Tax, Customs, Other Fees"
OTHER_FEES_COL = "Other Fees (Duty)"   # = Total Duties, Tax, Customs, Other Fees - (Customs Duty + MPF)
NO_REF = "NO REFERENCE INFORMATION"
ADDRESS_CORRECTION_COL = "Address Correction"

COLUMNS = [
    "Invoice No.", "Invoice Date", "Section", "Type", "Channel",
    "Tracking ID", "Entry No.", "Cust. Ref.", "Reference",
    "Total Charge", "Customs Duty", "Merchandise Processing Fee", DUTY_TOTAL_LABEL, OTHER_FEES_COL,
    ADDRESS_CORRECTION_COL, "Billed Amount", "Notes", "Check",
]

SECTION_HEADINGS = [   # 위에서부터 먼저 맞는 것 적용
    (re.compile(r"FedEx Express Shipment Detail"), EXPRESS),
    (re.compile(r"FedEx Ground .*Multiweight Detail"), GROUND_MW),
    (re.compile(r"FedEx Ground .*Detail"), GROUND),
    (re.compile(r"FedEx Other Charges Detail"), OTHER),
]
INVOICE_HEAD_RE = re.compile(r"(\d-\d{3}-\d{5})\s+([A-Z][a-z]{2} \d{1,2}, \d{4})")
PAGE_HEADER_LINE_RE = re.compile(
    r"^\s*(Invoice Number\s+Invoice Date\s+Account Number\s+Page"
    r"|\d+ of \d+"
    r"|\d-\d{3}-\d{5}\s+[A-Z][a-z]{2} \d{1,2}, \d{4}\s+\S+(\s+\d+ of \d+)?)\s*$"
)
PACKAGE_ROW_RE = re.compile(r"^\s*\d{12,}\s{2,}")


# ------------------------------------------------------------------------------
# 공통 유틸
# ------------------------------------------------------------------------------
def _to_mmddyyyy(text):
    """'Sep 04, 2026' → '09/04/2026' (형식이 다르면 원래 값 그대로)"""
    try:
        return datetime.strptime(text, "%b %d, %Y").strftime("%m/%d/%Y")
    except (TypeError, ValueError):
        return text or ""


def _to_float(s):
    return float(s.replace("$", "").replace(",", "")) if s else None


def _money(pattern, text):
    m = re.search(pattern + rf"\s+\$?(-?\$?[\d,]+\.\d{{2}})", text)
    return _to_float(m.group(1)) if m else None


def _field(label_re, text):
    """'Label:   값   다음라벨:' → 값 (빈칸이면 '')"""
    m = re.search(label_re + r"[ \t]*([^\n]*)", text)
    if not m:
        return ""
    value = re.split(r"\s{2,}", m.group(1).strip())[0]
    if re.match(r"^(Ref\.#\d|P\.O\.#|Dept\.#|Cust\. Ref\.)", value):   # 값 없이 다음 라벨이 바로 온 경우
        return ""
    value = value.strip().strip('"')
    return "" if value.upper() == NO_REF else value


def _address_correction(block):
    """Transportation 블록의 Address Correction → {컬럼: 값} (없으면 빈 dict)"""
    if not re.search(r"address correction", block, re.I):
        return {}
    m = re.search(r"Address Correction[^\n\d$-]*?\s+\$?(-?[\d,]+\.\d{2})", block, re.I)
    amount = _to_float(m.group(1)) if m else None
    note = f"Address Correction ${amount:,.2f} included in Total Charge" if amount is not None \
        else "Address Correction found (amount not detected)"
    return {ADDRESS_CORRECTION_COL: amount, "Notes": note, "_address_correction": True}


def _refs_text(refs):
    return " ".join(str(r) for r in refs if r)


def classify_channel(refs, rules=None):
    """refs: 확인할 값 (목록 또는 이미 합친 문자열) · rules: [(키워드, Channel)] — 처음 맞는 규칙 적용, 없으면 RED"""
    text = (refs if isinstance(refs, str) else _refs_text(refs)).upper()
    for keyword, channel in (rules or []):
        if keyword and keyword.upper() in text:
            return channel
    return RED


def channel_for(section, refs_text, rules=None):
    """행 하나의 Channel — Other Charges는 항상 RED"""
    return RED if section == OTHER else classify_channel(refs_text or "", rules)


def _split_blocks(text, start_re):
    """start_re가 나오는 줄마다 새 블록 시작. 첫 시작 줄 이전 내용은 버림."""
    blocks, cur = [], None
    for line in text.splitlines():
        if re.search(start_re, line):
            if cur is not None:
                blocks.append("\n".join(cur))
            cur = [line]
        elif cur is not None:
            cur.append(line)
    if cur is not None:
        blocks.append("\n".join(cur))
    return blocks


# ------------------------------------------------------------------------------
# 섹션별 파싱
# ------------------------------------------------------------------------------
def _parse_express(block):
    cust = _field(r"Cust\. Ref\.:", block)
    ref2 = _field(r"Ref\.#2:", block)
    m = re.search(r"Tracking ID\s+(\d{9,})", block)
    tracking = m.group(1) if m else ""
    channel = classify_channel([ref2, cust])
    base = {"Section": EXPRESS, "Channel": channel, CHANNEL_REFS_COL: _refs_text([ref2, cust]), "Tracking ID": tracking,
            "Cust. Ref.": cust, "Reference": ref2}

    rows = []
    transport = _money(r"Total (?:Transportation Charges|Charge)\s+USD", block)
    if transport is not None:
        rows.append({**base, "Type": TRANSPORTATION, "Total Charge": transport, "Billed Amount": transport,
                     **_address_correction(block)})

    duty_total = _money(re.escape(DUTY_TOTAL_LABEL) + r"\s+USD", block)
    if duty_total is not None:
        duty = _money(r"Customs Duty", block) or 0.0
        mpf = _money(r"Merchandise Processing Fee", block) or 0.0
        rows.append({**base, "Type": DUTY, "Entry No.": _field(r"Entry No\.", block),
                     "Customs Duty": duty, "Merchandise Processing Fee": mpf,
                     DUTY_TOTAL_LABEL: duty_total, OTHER_FEES_COL: round(duty_total - duty - mpf, 2),
                     "Billed Amount": duty_total})

    if not rows:
        rows.append({**base, "Type": "", "Billed Amount": None})
    return rows


def _parse_ground(block):
    cust = _field(r"Cust\. Ref\.:", block)
    po = _field(r"P\.O\.#:", block)
    m = re.search(r"Tracking ID\s+(\d{9,})", block)
    total = _money(r"Total Charge\s+USD", block)
    return [{"Section": GROUND, "Type": TRANSPORTATION, "Channel": classify_channel([po, cust]),
             CHANNEL_REFS_COL: _refs_text([po, cust]),
             "Tracking ID": m.group(1) if m else "", "Cust. Ref.": cust, "Reference": po,
             "Total Charge": total, "Billed Amount": total, **_address_correction(block)}]


def _parse_ground_multiweight(block):
    # 패키지 행: Tracking | Shipper | Customer Reference | P.O. Number | (Department) | Weight | Amount
    cust_refs, pos = [], []
    for line in block.splitlines():
        if PACKAGE_ROW_RE.match(line):
            cols = re.split(r"\s{2,}", line.strip())
            refs = [c.strip('"') for c in cols[2:-2]]   # Shipper와 Weight 사이
            if refs:
                cust_refs.append(refs[0])
                pos.extend(refs[1:])
    cust_refs = [c for c in dict.fromkeys(cust_refs) if c and c.upper() != NO_REF]
    pos = [p for p in dict.fromkeys(pos) if p]
    total = _money(r"Shipment Total\s+[\d,.]+(?:\s*lbs)?", block)
    dest = _field(r"Destination Zip:", block)
    return [{"Section": GROUND_MW, "Type": TRANSPORTATION,
             "Channel": classify_channel(cust_refs + pos), CHANNEL_REFS_COL: _refs_text(cust_refs + pos),
             "Tracking ID": "", "Cust. Ref.": ", ".join(cust_refs),
             "Reference": ", ".join(pos) + (f" (Dest. Zip {dest})" if dest else ""),
             "Total Charge": total, "Billed Amount": total, **_address_correction(block)}]


def _parse_other(block):
    total = _money(r"Total Charges\s+USD", block)
    # Other Charges도 Transportation으로 구분
    return [{"Section": OTHER, "Type": TRANSPORTATION, "Channel": RED, "Total Charge": total, "Billed Amount": total}]


PARSERS = {
    EXPRESS: (r"^\s*Ship Date:", _parse_express),
    GROUND: (r"^\s*Ship Date:", _parse_ground),
    GROUND_MW: (r"^\s*Ship Date:", _parse_ground_multiweight),
    OTHER: (r"^\s*Transaction ID\s+\d", _parse_other),   # Pickup 블록 시작 위치 (화면에는 안 나옴)
}


# ------------------------------------------------------------------------------
# 메인
# ------------------------------------------------------------------------------
def _read_pages(pdf_file):
    reader = PdfReader(pdf_file)
    return [p.extract_text(extraction_mode="layout") or "" for p in reader.pages]


def _split_sections(pages):
    """[(섹션 이름, 텍스트)] — 섹션 제목이 나올 때마다 새 섹션 시작"""
    sections, cur_name, cur_lines = [], None, []
    for page in pages:
        for line in page.splitlines():
            if not line.strip() or PAGE_HEADER_LINE_RE.match(line):
                continue
            heading = next((name for rx, name in SECTION_HEADINGS if rx.search(line)), None)
            if heading:
                if cur_name:
                    sections.append((cur_name, "\n".join(cur_lines)))
                cur_name, cur_lines = heading, []
            elif cur_name:
                cur_lines.append(line)
    if cur_name:
        sections.append((cur_name, "\n".join(cur_lines)))
    return sections


def _check(row):
    issues = []
    if row["Billed Amount"] is None or pd.isna(row["Billed Amount"]):
        issues.append("No amount")
    if row["Section"] in (EXPRESS, GROUND) and not row["Tracking ID"]:
        issues.append("No tracking ID")
    if row["Section"] == EXPRESS and not row["Type"]:
        issues.append("Unknown type")
    return ", ".join(issues) if issues else "OK"


def parse_fedex_invoice(pdf_file):
    pages = _read_pages(pdf_file)
    if not pages:
        raise ValueError("PDF에 페이지가 없습니다.")

    first = pages[0]
    m = INVOICE_HEAD_RE.search(first)
    summary = {
        "invoice_no": m.group(1) if m else "",
        "invoice_date": _to_mmddyyyy(m.group(2)) if m else "",   # MM/DD/YYYY
        "total_this_invoice": _money(r"TOTAL THIS INVOICE\s+USD", first),
        "page_count": len(pages),
    }

    rows = []
    for name, text in _split_sections(pages):
        start_re, parse = PARSERS[name]
        for block in _split_blocks(text, start_re):
            rows.extend(parse(block))

    df = pd.DataFrame(rows)
    flagged = df["_address_correction"].fillna(False).astype(bool) if "_address_correction" in df else \
        pd.Series(False, index=df.index)
    refs = df[CHANNEL_REFS_COL] if CHANNEL_REFS_COL in df else pd.Series("", index=df.index)
    df = df.reindex(columns=COLUMNS)
    df[CHANNEL_REFS_COL] = refs.fillna("").values   # 규칙이 바뀌었을 때 재분류용
    df["Address Correction?"] = flagged.values   # 화면에서 빨간색 표시용 (True/False)
    df["Invoice No."] = summary["invoice_no"]
    df["Invoice Date"] = summary["invoice_date"]
    for c in ["Tracking ID", "Entry No.", "Cust. Ref.", "Reference", "Type", "Notes"]:
        df[c] = df[c].fillna("")
    df["Check"] = df.apply(_check, axis=1)

    summary["shipment_count"] = len(df)
    summary["sum_of_shipments"] = float(round(df["Billed Amount"].fillna(0).sum(), 2))
    return summary, df


if __name__ == "__main__":
    import sys
    s, d = parse_fedex_invoice(sys.argv[1])
    pd.set_option("display.width", 250, "display.max_columns", 20, "display.max_rows", 200)
    print(s)
    print(d.drop(columns=["Invoice No.", "Invoice Date"]))
import os
import io
import json
import uuid
import shutil
from flask import Flask, render_template, request, jsonify, send_file, session
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Link
from pypdf.generic import RectangleObject
from PIL import Image
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.platypus import PageBreak
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "bundlemaker-dev-secret-change-in-production")

_BASE = os.environ.get("DATA_DIR") or os.path.join(os.path.dirname(__file__))
UPLOAD_FOLDER   = os.path.join(_BASE, "uploads")
OUTPUT_FOLDER   = os.path.join(_BASE, "output")
SESSIONS_FOLDER = os.path.join(_BASE, "sessions")

TEMPLATES = {
    "application_record": {"label": "Application Record",        "header": "APPLICATION RECORD",  "tab_style": "alpha"},
    "book_of_authorities": {"label": "Book of Authorities",      "header": "BOOK OF AUTHORITIES", "tab_style": "numeric"},
    "index_of_materials":  {"label": "Index of Materials",       "header": "INDEX OF MATERIALS",  "tab_style": "alpha"},
    "compendium":          {"label": "Compendium",               "header": "COMPENDIUM",           "tab_style": "alpha"},
    "motion_record":       {"label": "Motion Record",            "header": "MOTION RECORD",        "tab_style": "alpha"},
    "appeal_book":         {"label": "Appeal Book",              "header": "APPEAL BOOK",          "tab_style": "numeric"},
    "factum":              {"label": "Factum / Written Argument", "header": "FACTUM",              "tab_style": "alpha"},
    "trial_record":        {"label": "Trial Record",             "header": "TRIAL RECORD",         "tab_style": "alpha"},
    "exhibits":            {"label": "Exhibits",                 "header": "EXHIBITS",             "tab_style": "alpha"},
}

IMAGE_EXTENSIONS  = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp", ".heic"}
ALLOWED_EXTENSIONS = {".pdf"} | IMAGE_EXTENSIONS

JURISDICTIONS = {
    "Canada": [
        {"value": "ON",     "label": "Ontario",                     "court": "SUPERIOR COURT OF JUSTICE",               "rule_body": "Rules of Civil Procedure, R.R.O. 1990, Reg. 194"},
        {"value": "ON_CA",  "label": "Ontario — Court of Appeal",   "court": "COURT OF APPEAL FOR ONTARIO",             "rule_body": "Rules of Civil Procedure, R.R.O. 1990, Reg. 194"},
        {"value": "BC",     "label": "British Columbia",             "court": "SUPREME COURT OF BRITISH COLUMBIA",       "rule_body": "Supreme Court Civil Rules, B.C. Reg. 168/2009"},
        {"value": "AB",     "label": "Alberta",                     "court": "COURT OF KING'S BENCH OF ALBERTA",        "rule_body": "Alberta Rules of Court, Alta. Reg. 124/2010"},
        {"value": "QC",     "label": "Québec",                      "court": "SUPERIOR COURT",                          "rule_body": "Code of Civil Procedure, CQLR c. C-25.01"},
        {"value": "MB",     "label": "Manitoba",                    "court": "COURT OF KING'S BENCH OF MANITOBA",       "rule_body": "Court of Queen's Bench Rules, Man. Reg. 553/88"},
        {"value": "SK",     "label": "Saskatchewan",                "court": "COURT OF KING'S BENCH FOR SASKATCHEWAN",  "rule_body": "Queen's Bench Rules"},
        {"value": "NS",     "label": "Nova Scotia",                 "court": "SUPREME COURT OF NOVA SCOTIA",            "rule_body": "Nova Scotia Civil Procedure Rules"},
        {"value": "NB",     "label": "New Brunswick",               "court": "COURT OF KING'S BENCH OF NEW BRUNSWICK",  "rule_body": "Rules of Court, NB Reg. 82-73"},
        {"value": "CA_FED", "label": "Federal Court of Canada",     "court": "FEDERAL COURT",                           "rule_body": "Federal Courts Rules, SOR/98-106"},
        {"value": "CA_FCA", "label": "Federal Court of Appeal",     "court": "FEDERAL COURT OF APPEAL",                 "rule_body": "Federal Courts Rules, SOR/98-106"},
        {"value": "SCC",    "label": "Supreme Court of Canada",     "court": "SUPREME COURT OF CANADA",                 "rule_body": "Rules of the Supreme Court of Canada, SOR/2002-156"},
    ],
    "United Kingdom": [
        {"value": "EW_HC", "label": "England & Wales — High Court",        "court": "HIGH COURT OF JUSTICE",                        "rule_body": "Civil Procedure Rules 1998 (SI 1998/3132)"},
        {"value": "EW_CA", "label": "England & Wales — Court of Appeal",   "court": "COURT OF APPEAL",                              "rule_body": "Civil Procedure Rules 1998 (SI 1998/3132)"},
        {"value": "UKSC",  "label": "UK Supreme Court",                    "court": "THE SUPREME COURT OF THE UNITED KINGDOM",      "rule_body": "Supreme Court Rules 2009 (SI 2009/1603)"},
        {"value": "SC_OS", "label": "Scotland — Outer House",              "court": "COURT OF SESSION — OUTER HOUSE",               "rule_body": "Rules of the Court of Session 1994 (SI 1994/1443)"},
        {"value": "SC_IH", "label": "Scotland — Inner House",              "court": "COURT OF SESSION — INNER HOUSE",               "rule_body": "Rules of the Court of Session 1994 (SI 1994/1443)"},
        {"value": "NI",    "label": "Northern Ireland",                    "court": "HIGH COURT OF JUSTICE IN NORTHERN IRELAND",    "rule_body": "Rules of the Court of Judicature (NI) 1980"},
    ],
    "United States": [
        {"value": "US_FED",   "label": "Federal District Court",       "court": "UNITED STATES DISTRICT COURT",              "rule_body": "Federal Rules of Civil Procedure"},
        {"value": "US_CA",    "label": "Federal Court of Appeals",     "court": "UNITED STATES COURT OF APPEALS",            "rule_body": "Federal Rules of Appellate Procedure"},
        {"value": "USSC",     "label": "US Supreme Court",             "court": "SUPREME COURT OF THE UNITED STATES",        "rule_body": "Rules of the Supreme Court of the United States"},
        {"value": "US_NY",    "label": "New York — Supreme Court",     "court": "SUPREME COURT OF THE STATE OF NEW YORK",    "rule_body": "New York Civil Practice Law and Rules"},
        {"value": "US_CA_ST", "label": "California — Superior Court",  "court": "SUPERIOR COURT OF THE STATE OF CALIFORNIA", "rule_body": "California Rules of Court"},
        {"value": "US_TX",    "label": "Texas — District Court",       "court": "DISTRICT COURT OF TEXAS",                   "rule_body": "Texas Rules of Civil Procedure"},
        {"value": "US_FL",    "label": "Florida — Circuit Court",      "court": "CIRCUIT COURT OF FLORIDA",                  "rule_body": "Florida Rules of Civil Procedure"},
        {"value": "US_IL",    "label": "Illinois — Circuit Court",     "court": "CIRCUIT COURT OF COOK COUNTY, ILLINOIS",    "rule_body": "Illinois Supreme Court Rules"},
    ],
    "Australia": [
        {"value": "AU_FED", "label": "Federal Court of Australia",          "court": "FEDERAL COURT OF AUSTRALIA",          "rule_body": "Federal Court Rules 2011 (Cth)"},
        {"value": "AU_HCA", "label": "High Court of Australia",             "court": "HIGH COURT OF AUSTRALIA",             "rule_body": "High Court Rules 2004 (Cth)"},
        {"value": "AU_NSW", "label": "New South Wales — Supreme Court",     "court": "SUPREME COURT OF NEW SOUTH WALES",    "rule_body": "Uniform Civil Procedure Rules 2005 (NSW)"},
        {"value": "AU_VIC", "label": "Victoria — Supreme Court",            "court": "SUPREME COURT OF VICTORIA",           "rule_body": "Supreme Court (General Civil Procedure) Rules 2015 (Vic)"},
        {"value": "AU_QLD", "label": "Queensland — Supreme Court",          "court": "SUPREME COURT OF QUEENSLAND",         "rule_body": "Uniform Civil Procedure Rules 1999 (Qld)"},
        {"value": "AU_WA",  "label": "Western Australia — Supreme Court",   "court": "SUPREME COURT OF WESTERN AUSTRALIA",  "rule_body": "Rules of the Supreme Court 1971 (WA)"},
    ],
    "New Zealand": [
        {"value": "NZ_HC", "label": "High Court",     "court": "HIGH COURT OF NEW ZEALAND",       "rule_body": "High Court Rules 2016"},
        {"value": "NZ_CA", "label": "Court of Appeal","court": "COURT OF APPEAL OF NEW ZEALAND",  "rule_body": "Court of Appeal (Civil) Rules 2005"},
        {"value": "NZSC",  "label": "Supreme Court",  "court": "SUPREME COURT OF NEW ZEALAND",    "rule_body": "Supreme Court Rules 2004"},
    ],
    "Ireland": [
        {"value": "IE_HC", "label": "High Court",     "court": "HIGH COURT", "rule_body": "Rules of the Superior Courts (SI 15/1986)"},
        {"value": "IE_CA", "label": "Court of Appeal","court": "COURT OF APPEAL", "rule_body": "Rules of the Superior Courts"},
        {"value": "IE_SC", "label": "Supreme Court",  "court": "SUPREME COURT",   "rule_body": "Rules of the Superior Courts"},
    ],
    "Singapore": [
        {"value": "SG_GD", "label": "General Division — High Court", "court": "GENERAL DIVISION OF THE HIGH COURT", "rule_body": "Rules of Court 2021"},
        {"value": "SG_CA", "label": "Court of Appeal",               "court": "COURT OF APPEAL",                    "rule_body": "Rules of Court 2021"},
    ],
    "Other / Custom": [
        {"value": "CUSTOM", "label": "Custom / Other jurisdiction", "court": "", "rule_body": ""},
    ],
}


# ── Session helpers ──────────────────────────────────────────────────────────

def session_path(sid):
    return os.path.join(SESSIONS_FOLDER, f"{sid}.json")

def _default_session():
    return {
        "tabs": [],
        "doc_type": "application_record",
        "title": "",
        "court_file": "",
        "parties": "",
        "recitals": "",
        "country": "Canada",
        "jurisdiction": "ON",
        "custom_court": "",
        "custom_rules": "",
        "use_dividers": True,
    }

def get_session(sid):
    path = session_path(sid)
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
        # Migrate old flat-items sessions to tabs format
        if "items" in data and "tabs" not in data:
            old_items = data.pop("items")
            data["tabs"] = []
            if old_items:
                data["tabs"].append({"id": uuid.uuid4().hex, "name": "Documents", "items": old_items})
        if "use_dividers" not in data:
            data["use_dividers"] = True
        return data
    return _default_session()

def save_session(sid, data):
    with open(session_path(sid), "w") as f:
        json.dump(data, f)


# ── Label helpers ────────────────────────────────────────────────────────────

def alpha_label(n):
    result = ""
    while n >= 0:
        result = chr(65 + (n % 26)) + result
        n = n // 26 - 1
    return result

def numeric_label(n):
    return str(n + 1)


# ── Image → PDF ──────────────────────────────────────────────────────────────

def image_to_pdf(image_path, pdf_path):
    img = Image.open(image_path)
    if img.mode in ("RGBA", "P", "LA"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")

    page_w, page_h = letter
    margin = 36
    max_w = page_w - 2 * margin
    max_h = page_h - 2 * margin
    iw, ih = img.size
    scale = min(max_w / iw, max_h / ih, 1.0)
    new_w, new_h = int(iw * scale), int(ih * scale)

    buf = io.BytesIO()
    img_resized = img.resize((new_w, new_h), Image.LANCZOS)

    c_doc = SimpleDocTemplate(buf, pagesize=letter,
                              rightMargin=margin, leftMargin=margin,
                              topMargin=margin, bottomMargin=margin)
    from reportlab.platypus import Image as RLImage
    import tempfile, os as _os
    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    img_resized.save(tmp.name, "JPEG", quality=92)
    tmp.close()
    story = [RLImage(tmp.name, width=new_w, height=new_h)]
    c_doc.build(story)
    _os.unlink(tmp.name)

    with open(pdf_path, "wb") as f:
        f.write(buf.getvalue())


def get_pdf_page_count(filepath):
    try:
        reader = PdfReader(filepath)
        return len(reader.pages)
    except Exception:
        return 0


def resolve_jurisdiction(country, jurisdiction_value):
    for j in JURISDICTIONS.get(country, []):
        if j["value"] == jurisdiction_value:
            return j["court"], j["rule_body"]
    return "", ""


# ── PDF generation ───────────────────────────────────────────────────────────

def generate_cover_toc(doc_type, tabs, title, court_file, parties, output_path,
                       country="Canada", jurisdiction="ON",
                       custom_court="", custom_rules="", recitals="",
                       use_dividers=True):
    """Generate cover page + optional recitals + TOC. tabs is a list of tab dicts."""
    tmpl = TEMPLATES[doc_type]
    doc = SimpleDocTemplate(
        output_path,
        pagesize=letter,
        rightMargin=1 * inch,
        leftMargin=1.25 * inch,
        topMargin=1.25 * inch,
        bottomMargin=1.25 * inch,
    )

    styles = getSampleStyleSheet()
    normal = styles["Normal"]
    normal.fontName = "Times-Roman"
    normal.fontSize = 12

    center_bold = ParagraphStyle(
        "center_bold", parent=normal,
        alignment=TA_CENTER, fontName="Times-Bold",
        fontSize=14, spaceAfter=10, spaceBefore=4,
    )
    center_normal = ParagraphStyle(
        "center_normal", parent=normal,
        alignment=TA_CENTER, fontSize=12,
        spaceAfter=6, leading=18,
    )
    small_center = ParagraphStyle(
        "small_center", parent=normal,
        alignment=TA_CENTER, fontSize=10,
        spaceAfter=6, leading=15,
    )
    toc_header = ParagraphStyle(
        "toc_header", parent=normal,
        alignment=TA_CENTER, fontName="Times-Bold",
        fontSize=14, spaceAfter=16, spaceBefore=16,
    )

    court_name, rule_body = resolve_jurisdiction(country, jurisdiction)
    if jurisdiction == "CUSTOM":
        court_name = custom_court
        rule_body = custom_rules

    story = []

    # ── Cover Page ──────────────────────────────────────────────────────────
    story.append(Spacer(1, 0.75 * inch))

    if country and country != "Other / Custom":
        story.append(Paragraph(country.upper(), center_bold))
        story.append(Spacer(1, 0.15 * inch))

    if court_name:
        story.append(Paragraph(court_name, center_bold))

    story.append(Spacer(1, 0.4 * inch))

    if court_file:
        right_normal = ParagraphStyle(
            "right_normal", parent=normal,
            alignment=TA_RIGHT, fontSize=12, spaceAfter=6,
        )
        story.append(Paragraph(f"Court File No.: {court_file}", right_normal))
        story.append(Spacer(1, 0.25 * inch))

    if parties:
        for line in parties.strip().split("\n"):
            story.append(Paragraph(line, center_normal))
        story.append(Spacer(1, 0.4 * inch))

    story.append(Spacer(1, 0.3 * inch))
    story.append(Paragraph(tmpl["header"], center_bold))

    if title:
        story.append(Spacer(1, 0.2 * inch))
        story.append(Paragraph(title, center_normal))

    if rule_body:
        story.append(Spacer(1, 0.4 * inch))
        story.append(Paragraph(rule_body, small_center))

    story.append(PageBreak())

    # ── Written Recitals (optional page before TOC) ─────────────────────────
    if recitals and recitals.strip():
        recital_style = ParagraphStyle(
            "recital", parent=normal,
            alignment=TA_LEFT, fontSize=11,
            leading=20, spaceAfter=10,
        )
        story.append(Spacer(1, 0.6 * inch))
        for para in recitals.strip().split("\n"):
            if para.strip():
                story.append(Paragraph(para.strip(), recital_style))
                story.append(Spacer(1, 0.1 * inch))
        story.append(PageBreak())

    # ── Table of Contents ───────────────────────────────────────────────────
    story.append(Paragraph("TABLE OF CONTENTS", toc_header))

    tab_fn = alpha_label if tmpl["tab_style"] == "alpha" else numeric_label

    th_style   = ParagraphStyle("th",   parent=normal, fontName="Times-Bold", fontSize=11)
    th_right   = ParagraphStyle("thr",  parent=normal, fontName="Times-Bold", fontSize=11, alignment=TA_RIGHT)
    tab_row_st = ParagraphStyle("tabr", parent=normal, fontName="Times-Bold", fontSize=11, leading=16)
    tab_row_rt = ParagraphStyle("tabrt",parent=normal, fontName="Times-Bold", fontSize=11, alignment=TA_RIGHT, leading=16)
    doc_row_st = ParagraphStyle("docr", parent=normal, fontSize=10, leading=15, leftIndent=10)
    doc_row_rt = ParagraphStyle("docrt",parent=normal, fontSize=10, alignment=TA_RIGHT, leading=15)

    toc_data   = [[
        Paragraph("<b>Tab</b>",      th_style),
        Paragraph("<b>Document</b>", th_style),
        Paragraph("<b>Page(s)</b>",  th_right),
    ]]
    # Each entry: (row_index_in_toc_data, target_pdf_page_index)
    # We'll compute this in add_toc_links; here just build the visual rows.

    current_page = 1  # logical page counter (1-based, excluding dividers)

    for i, tab in enumerate(tabs):
        tab_label = tab_fn(i)
        tab_name  = tab.get("name") or f"Tab {tab_label}"
        tab_items = tab.get("items", [])
        total_pages = sum(item.get("page_count", 1) for item in tab_items) or 1

        if use_dividers:
            # Bold summary row for the tab
            tab_page_str = str(current_page) if total_pages == 1 else f"{current_page}–{current_page + total_pages - 1}"
            toc_data.append([
                Paragraph(f"<b>Tab {tab_label}</b>", tab_row_st),
                Paragraph(f"<b>{tab_name}</b>",      tab_row_st),
                Paragraph(tab_page_str,               tab_row_rt),
            ])
            # Indented rows for each document in this tab
            doc_page = current_page
            for item in tab_items:
                pc = item.get("page_count", 1)
                doc_name = item.get("custom_name") or item.get("filename", "Document")
                doc_page_str = str(doc_page) if pc == 1 else f"{doc_page}–{doc_page + pc - 1}"
                toc_data.append([
                    Paragraph("", doc_row_st),
                    Paragraph(f"  ▸  {doc_name}", doc_row_st),
                    Paragraph(doc_page_str, doc_row_rt),
                ])
                doc_page += pc
            current_page += total_pages + 1  # +1 for the next tab's divider
        else:
            # No dividers: flat rows, one per document, labelled with tab
            for item in tab_items:
                pc = item.get("page_count", 1)
                doc_name = item.get("custom_name") or item.get("filename", "Document")
                doc_page_str = str(current_page) if pc == 1 else f"{current_page}–{current_page + pc - 1}"
                toc_data.append([
                    Paragraph(f"Tab {tab_label}", tab_row_st),
                    Paragraph(doc_name, tab_row_st),
                    Paragraph(doc_page_str, tab_row_rt),
                ])
                current_page += pc

    # Build table style — shade tab-summary rows
    tab_shade = colors.Color(0.93, 0.91, 0.87)  # light parchment
    ts = [
        ("FONTNAME",      (0, 0), (-1, 0),  "Times-Bold"),
        ("FONTSIZE",      (0, 0), (-1, -1), 11),
        ("LINEBELOW",     (0, 0), (-1, 0),  1,    colors.black),
        ("LINEBELOW",     (0, 1), (-1, -1), 0.25, colors.lightgrey),
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING",    (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING",   (0, 0), (-1, -1), 4),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
    ]
    if use_dividers:
        # shade every tab-summary row (every row that starts with bold Tab label)
        row_idx = 1
        for tab in tabs:
            ts.append(("BACKGROUND", (0, row_idx), (-1, row_idx), tab_shade))
            n_items = len(tab.get("items", []))
            row_idx += 1 + n_items  # 1 summary + N item rows

    toc_table = Table(toc_data, colWidths=[0.9 * inch, 4.5 * inch, 0.8 * inch])
    toc_table.setStyle(TableStyle(ts))
    story.append(toc_table)
    story.append(PageBreak())

    doc.build(story)


def generate_divider_page(tab_label, tab_name, output_path):
    doc = SimpleDocTemplate(
        output_path,
        pagesize=letter,
        rightMargin=1 * inch,
        leftMargin=1.25 * inch,
        topMargin=2 * inch,
        bottomMargin=1 * inch,
    )
    styles = getSampleStyleSheet()
    normal = styles["Normal"]

    tab_style = ParagraphStyle(
        "tab_big", parent=normal,
        fontName="Times-Bold", fontSize=36,
        alignment=TA_CENTER, spaceAfter=24,
    )
    name_style = ParagraphStyle(
        "tab_name", parent=normal,
        fontName="Times-Roman", fontSize=14,
        alignment=TA_CENTER, leading=20,
    )

    story = [
        Spacer(1, 1.5 * inch),
        Paragraph(f"TAB {tab_label}", tab_style),
        Paragraph(tab_name, name_style),
        PageBreak(),
    ]
    doc.build(story)


def add_toc_links(writer, toc_page_index, tabs, first_tab_page_index, use_dividers=True):
    """Stamp clickable GoTo links on the TOC page for every row."""
    page = writer.pages[toc_page_index]
    page_height = float(page.mediabox.height)
    page_width  = float(page.mediabox.width)

    left  = 1.0 * 72
    right = page_width - 1.0 * 72

    # Approximate TOC layout (matches generate_cover_toc):
    #   top margin 1.25" = 90pt
    #   "TABLE OF CONTENTS" heading ≈ 56pt
    #   header row ≈ 30pt
    #   tab-summary rows ≈ 30pt each  (7+11+7 + ~5 leading)
    #   doc-item rows ≈ 25pt each     (7+10+7 + ~1)
    first_row_top = page_height - 90 - 56 - 30  # top of first data row
    tab_row_h  = 30
    doc_row_h  = 25

    # Build a flat list: [(row_offset_from_first, height, target_pdf_page_index)]
    link_rows = []
    current_pdf_page = first_tab_page_index  # 0-indexed in the final PDF

    if use_dividers:
        y_offset = 0  # cumulative y consumed
        for tab in tabs:
            items = tab.get("items", [])
            # Tab summary row → divider page
            link_rows.append((y_offset, tab_row_h, current_pdf_page))
            y_offset += tab_row_h
            # Individual doc rows → first page of each doc
            doc_pdf_page = current_pdf_page + 1  # +1 past the divider
            for item in items:
                link_rows.append((y_offset, doc_row_h, doc_pdf_page))
                y_offset    += doc_row_h
                doc_pdf_page += item.get("page_count", 1)
            total_pages = sum(item.get("page_count", 1) for item in items)
            current_pdf_page += 1 + total_pages
    else:
        y_offset = 0
        for tab in tabs:
            for item in tab.get("items", []):
                link_rows.append((y_offset, tab_row_h, current_pdf_page))
                y_offset         += tab_row_h
                current_pdf_page += item.get("page_count", 1)

    for (y_off, row_h, target_page) in link_rows:
        row_top    = first_row_top - y_off
        row_bottom = row_top - row_h
        rect = RectangleObject([left, row_bottom, right, row_top])
        try:
            annotation = Link(rect=rect, target_page_index=target_page)
            writer.add_annotation(page_number=toc_page_index, annotation=annotation)
        except Exception:
            pass


def merge_pdfs(session_data, output_path):
    doc_type     = session_data["doc_type"]
    tabs         = session_data.get("tabs", [])
    use_dividers = session_data.get("use_dividers", True)
    tmpl         = TEMPLATES[doc_type]
    tab_fn       = alpha_label if tmpl["tab_style"] == "alpha" else numeric_label

    writer = PdfWriter()

    # 1. Cover + TOC
    toc_path = os.path.join(OUTPUT_FOLDER, f"_toc_{uuid.uuid4().hex}.pdf")
    generate_cover_toc(
        doc_type, tabs,
        session_data.get("title", ""),
        session_data.get("court_file", ""),
        session_data.get("parties", ""),
        toc_path,
        country=session_data.get("country", "Canada"),
        jurisdiction=session_data.get("jurisdiction", "ON"),
        custom_court=session_data.get("custom_court", ""),
        custom_rules=session_data.get("custom_rules", ""),
        recitals=session_data.get("recitals", ""),
        use_dividers=use_dividers,
    )
    reader = PdfReader(toc_path)
    cover_page_count = len(reader.pages)
    for page in reader.pages:
        writer.add_page(page)
    os.remove(toc_path)

    toc_page_index       = cover_page_count - 1
    first_tab_page_index = cover_page_count

    # 2. For each tab: optional divider + all documents
    for i, tab in enumerate(tabs):
        if use_dividers:
            tab_label = tab_fn(i)
            tab_name  = tab.get("name") or f"Tab {tab_label}"
            div_path  = os.path.join(OUTPUT_FOLDER, f"_div_{uuid.uuid4().hex}.pdf")
            generate_divider_page(tab_label, tab_name, div_path)
            div_reader = PdfReader(div_path)
            for page in div_reader.pages:
                writer.add_page(page)
            os.remove(div_path)

        for item in tab.get("items", []):
            doc_path = item.get("filepath")
            if doc_path and os.path.exists(doc_path):
                doc_reader = PdfReader(doc_path)
                for page in doc_reader.pages:
                    writer.add_page(page)

    # 3. Stamp clickable links on the TOC page
    add_toc_links(writer, toc_page_index, tabs, first_tab_page_index, use_dividers=use_dividers)

    with open(output_path, "wb") as f:
        writer.write(f)


# ── Routes ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if "sid" not in session:
        session["sid"] = uuid.uuid4().hex
    return render_template("index.html")


@app.route("/api/session", methods=["GET"])
def get_session_data():
    sid = session.get("sid", uuid.uuid4().hex)
    session["sid"] = sid
    return jsonify(get_session(sid))


@app.route("/api/session", methods=["POST"])
def update_session():
    sid = session.get("sid")
    data = request.json
    sess = get_session(sid)
    for key in ("doc_type", "title", "court_file", "parties", "recitals", "country", "jurisdiction", "custom_court", "custom_rules", "use_dividers"):
        if key in data:
            sess[key] = data[key]
    save_session(sid, sess)
    return jsonify({"ok": True})


@app.route("/api/jurisdictions", methods=["GET"])
def get_jurisdictions():
    return jsonify(JURISDICTIONS)


# ── Tab routes ───────────────────────────────────────────────────────────────

@app.route("/api/tabs", methods=["GET"])
def get_tabs():
    sid = session.get("sid")
    sess = get_session(sid)
    return jsonify(sess.get("tabs", []))


@app.route("/api/tabs", methods=["POST"])
def create_tab():
    sid = session.get("sid")
    sess = get_session(sid)
    data = request.json or {}
    tab = {
        "id":    uuid.uuid4().hex,
        "name":  data.get("name", ""),
        "items": [],
    }
    sess["tabs"].append(tab)
    save_session(sid, sess)
    return jsonify(tab)


@app.route("/api/tabs/reorder", methods=["POST"])
def reorder_tabs():
    sid = session.get("sid")
    sess = get_session(sid)
    new_order = request.json.get("order", [])
    id_map = {t["id"]: t for t in sess["tabs"]}
    sess["tabs"] = [id_map[i] for i in new_order if i in id_map]
    save_session(sid, sess)
    return jsonify({"ok": True})


@app.route("/api/tabs/<tab_id>", methods=["PATCH"])
def update_tab(tab_id):
    sid = session.get("sid")
    sess = get_session(sid)
    data = request.json or {}
    for tab in sess["tabs"]:
        if tab["id"] == tab_id:
            if "name" in data:
                tab["name"] = data["name"]
            break
    save_session(sid, sess)
    return jsonify({"ok": True})


@app.route("/api/tabs/<tab_id>", methods=["DELETE"])
def delete_tab(tab_id):
    sid = session.get("sid")
    sess = get_session(sid)
    sess["tabs"] = [t for t in sess["tabs"] if t["id"] != tab_id]
    save_session(sid, sess)
    return jsonify({"ok": True})


@app.route("/api/tabs/<tab_id>/upload", methods=["POST"])
def upload_to_tab(tab_id):
    sid = session.get("sid")
    sess = get_session(sid)
    tab = next((t for t in sess["tabs"] if t["id"] == tab_id), None)
    if tab is None:
        return jsonify({"error": "Tab not found"}), 404

    files = request.files.getlist("files")
    added = []
    for f in files:
        ext = os.path.splitext(f.filename.lower())[1]
        if ext not in ALLOWED_EXTENSIONS:
            continue
        item_id  = uuid.uuid4().hex
        raw_dest = os.path.join(UPLOAD_FOLDER, f"{item_id}{ext}")
        f.save(raw_dest)

        if ext in IMAGE_EXTENSIONS:
            pdf_dest = os.path.join(UPLOAD_FOLDER, f"{item_id}.pdf")
            image_to_pdf(raw_dest, pdf_dest)
            os.remove(raw_dest)
            filepath = pdf_dest
        else:
            filepath = raw_dest

        base_name  = os.path.splitext(f.filename)[0].replace("_", " ").replace("-", " ")
        page_count = get_pdf_page_count(filepath)
        item = {
            "id":           item_id,
            "filename":     base_name,
            "custom_name":  "",
            "filepath":     filepath,
            "page_count":   page_count,
            "file_type":    "image" if ext in IMAGE_EXTENSIONS else "pdf",
            "original_ext": ext,
        }
        tab["items"].append(item)
        added.append(item)

    save_session(sid, sess)
    return jsonify(added)


@app.route("/api/tabs/<tab_id>/items/reorder", methods=["POST"])
def reorder_tab_items(tab_id):
    sid = session.get("sid")
    sess = get_session(sid)
    tab = next((t for t in sess["tabs"] if t["id"] == tab_id), None)
    if tab is None:
        return jsonify({"error": "Tab not found"}), 404
    new_order = request.json.get("order", [])
    id_map = {item["id"]: item for item in tab["items"]}
    tab["items"] = [id_map[i] for i in new_order if i in id_map]
    save_session(sid, sess)
    return jsonify({"ok": True})


@app.route("/api/tabs/<tab_id>/items/<item_id>", methods=["PATCH"])
def update_tab_item(tab_id, item_id):
    sid = session.get("sid")
    sess = get_session(sid)
    data = request.json or {}
    for tab in sess["tabs"]:
        if tab["id"] == tab_id:
            for item in tab["items"]:
                if item["id"] == item_id:
                    if "custom_name" in data:
                        item["custom_name"] = data["custom_name"]
                    break
            break
    save_session(sid, sess)
    return jsonify({"ok": True})


@app.route("/api/tabs/<tab_id>/items/<item_id>", methods=["DELETE"])
def delete_tab_item(tab_id, item_id):
    sid = session.get("sid")
    sess = get_session(sid)
    for tab in sess["tabs"]:
        if tab["id"] == tab_id:
            tab["items"] = [i for i in tab["items"] if i["id"] != item_id]
            break
    save_session(sid, sess)
    return jsonify({"ok": True})


# ── Generate & Download ──────────────────────────────────────────────────────

@app.route("/api/generate", methods=["POST"])
def generate():
    sid  = session.get("sid")
    sess = get_session(sid)
    tabs = sess.get("tabs", [])
    total_docs = sum(len(t.get("items", [])) for t in tabs)
    if total_docs == 0:
        return jsonify({"error": "No documents added yet. Please upload at least one file."}), 400
    out_name = f"legal_document_{uuid.uuid4().hex[:8]}.pdf"
    out_path = os.path.join(OUTPUT_FOLDER, out_name)
    try:
        merge_pdfs(sess, out_path)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"filename": out_name})


@app.route("/api/download/<filename>")
def download(filename):
    path = os.path.join(OUTPUT_FOLDER, filename)
    if not os.path.exists(path):
        return "Not found", 404
    return send_file(path, as_attachment=True, download_name=filename)


@app.route("/api/reset", methods=["POST"])
def reset():
    sid = session.get("sid")
    save_session(sid, _default_session())
    return jsonify({"ok": True})


if __name__ == "__main__":
    os.makedirs(UPLOAD_FOLDER,   exist_ok=True)
    os.makedirs(OUTPUT_FOLDER,   exist_ok=True)
    os.makedirs(SESSIONS_FOLDER, exist_ok=True)
    port = int(os.environ.get("PORT", 5050))
    app.run(debug=False, port=port)

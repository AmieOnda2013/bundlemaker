import os
import io
import json
import uuid
import shutil
from flask import Flask, render_template, request, jsonify, send_file, session, redirect, url_for, flash
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
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
import stripe
from models import db, User

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "bundlemaker-dev-secret-change-in-production")

# ── Database ─────────────────────────────────────────────────────────────────
_BASE_DIR = os.environ.get("DATA_DIR") or os.path.dirname(__file__)
_db_url = os.environ.get("DATABASE_URL") or f"sqlite:///{os.path.join(_BASE_DIR, 'bundlemaker.db')}"
# Railway PostgreSQL uses postgres:// but SQLAlchemy requires postgresql://
if _db_url.startswith("postgres://"):
    _db_url = _db_url.replace("postgres://", "postgresql+pg8000://", 1)
elif _db_url.startswith("postgresql://"):
    _db_url = _db_url.replace("postgresql://", "postgresql+pg8000://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = _db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SESSION_COOKIE_HTTPONLY"]  = True
app.config["SESSION_COOKIE_SAMESITE"]  = "Lax"
app.config["SESSION_COOKIE_SECURE"]    = os.environ.get("RAILWAY_ENVIRONMENT") == "production"

db.init_app(app)

# ── Stripe ────────────────────────────────────────────────────────────────────
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_SOLO_PRICE_ID  = os.environ.get("STRIPE_SOLO_PRICE_ID", "")
STRIPE_FIRM_PRICE_ID  = os.environ.get("STRIPE_FIRM_PRICE_ID", "")

PLANS = {
    "solo": {"name": "Solo",  "price": "$19/mo", "price_id": STRIPE_SOLO_PRICE_ID, "limit": None},
    "firm": {"name": "Firm",  "price": "$49/mo", "price_id": STRIPE_FIRM_PRICE_ID, "limit": None},
}

# ── Auth ──────────────────────────────────────────────────────────────────────
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to access BundleMaker."

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

UPLOAD_FOLDER   = os.path.join(_BASE_DIR, "uploads")
OUTPUT_FOLDER   = os.path.join(_BASE_DIR, "output")
SESSIONS_FOLDER = os.path.join(_BASE_DIR, "sessions")

for _d in (UPLOAD_FOLDER, OUTPUT_FOLDER, SESSIONS_FOLDER):
    os.makedirs(_d, exist_ok=True)

with app.app_context():
    db.create_all()

TEMPLATES = {
    "application_record":  {"label": "Application Record",        "header": "APPLICATION RECORD",  "tab_style": "alpha"},
    "book_of_authorities": {"label": "Book of Authorities",       "header": "BOOK OF AUTHORITIES", "tab_style": "numeric"},
    "index_of_materials":  {"label": "Index of Materials",        "header": "INDEX OF MATERIALS",  "tab_style": "alpha"},
    "compendium":          {"label": "Compendium",                "header": "COMPENDIUM",           "tab_style": "alpha"},
    "motion_record":       {"label": "Motion Record",             "header": "MOTION RECORD",        "tab_style": "alpha"},
    "appeal_book":         {"label": "Appeal Book",               "header": "APPEAL BOOK",          "tab_style": "numeric"},
    "factum":              {"label": "Factum / Written Argument",  "header": "FACTUM",              "tab_style": "alpha"},
    "trial_record":        {"label": "Trial Record",              "header": "TRIAL RECORD",         "tab_style": "alpha"},
    "exhibits":            {"label": "Exhibits",                  "header": "EXHIBITS",             "tab_style": "alpha"},
}

IMAGE_EXTENSIONS   = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp", ".heic"}
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
        {"value": "EW_HC", "label": "England & Wales — High Court",       "court": "HIGH COURT OF JUSTICE",                     "rule_body": "Civil Procedure Rules 1998 (SI 1998/3132)"},
        {"value": "EW_CA", "label": "England & Wales — Court of Appeal",  "court": "COURT OF APPEAL",                           "rule_body": "Civil Procedure Rules 1998 (SI 1998/3132)"},
        {"value": "UKSC",  "label": "UK Supreme Court",                   "court": "THE SUPREME COURT OF THE UNITED KINGDOM",   "rule_body": "Supreme Court Rules 2009 (SI 2009/1603)"},
        {"value": "SC_OS", "label": "Scotland — Outer House",             "court": "COURT OF SESSION — OUTER HOUSE",            "rule_body": "Rules of the Court of Session 1994 (SI 1994/1443)"},
        {"value": "SC_IH", "label": "Scotland — Inner House",             "court": "COURT OF SESSION — INNER HOUSE",            "rule_body": "Rules of the Court of Session 1994 (SI 1994/1443)"},
        {"value": "NI",    "label": "Northern Ireland",                   "court": "HIGH COURT OF JUSTICE IN NORTHERN IRELAND", "rule_body": "Rules of the Court of Judicature (NI) 1980"},
    ],
    "United States": [
        {"value": "US_FED",   "label": "Federal District Court",      "court": "UNITED STATES DISTRICT COURT",              "rule_body": "Federal Rules of Civil Procedure"},
        {"value": "US_CA",    "label": "Federal Court of Appeals",    "court": "UNITED STATES COURT OF APPEALS",            "rule_body": "Federal Rules of Appellate Procedure"},
        {"value": "USSC",     "label": "US Supreme Court",            "court": "SUPREME COURT OF THE UNITED STATES",        "rule_body": "Rules of the Supreme Court of the United States"},
        {"value": "US_NY",    "label": "New York — Supreme Court",    "court": "SUPREME COURT OF THE STATE OF NEW YORK",    "rule_body": "New York Civil Practice Law and Rules"},
        {"value": "US_CA_ST", "label": "California — Superior Court", "court": "SUPERIOR COURT OF THE STATE OF CALIFORNIA", "rule_body": "California Rules of Court"},
        {"value": "US_TX",    "label": "Texas — District Court",      "court": "DISTRICT COURT OF TEXAS",                   "rule_body": "Texas Rules of Civil Procedure"},
        {"value": "US_FL",    "label": "Florida — Circuit Court",     "court": "CIRCUIT COURT OF FLORIDA",                  "rule_body": "Florida Rules of Civil Procedure"},
        {"value": "US_IL",    "label": "Illinois — Circuit Court",    "court": "CIRCUIT COURT OF COOK COUNTY, ILLINOIS",    "rule_body": "Illinois Supreme Court Rules"},
    ],
    "Australia": [
        {"value": "AU_FED", "label": "Federal Court of Australia",        "court": "FEDERAL COURT OF AUSTRALIA",         "rule_body": "Federal Court Rules 2011 (Cth)"},
        {"value": "AU_HCA", "label": "High Court of Australia",           "court": "HIGH COURT OF AUSTRALIA",            "rule_body": "High Court Rules 2004 (Cth)"},
        {"value": "AU_NSW", "label": "New South Wales — Supreme Court",   "court": "SUPREME COURT OF NEW SOUTH WALES",   "rule_body": "Uniform Civil Procedure Rules 2005 (NSW)"},
        {"value": "AU_VIC", "label": "Victoria — Supreme Court",          "court": "SUPREME COURT OF VICTORIA",          "rule_body": "Supreme Court (General Civil Procedure) Rules 2015 (Vic)"},
        {"value": "AU_QLD", "label": "Queensland — Supreme Court",        "court": "SUPREME COURT OF QUEENSLAND",        "rule_body": "Uniform Civil Procedure Rules 1999 (Qld)"},
        {"value": "AU_WA",  "label": "Western Australia — Supreme Court", "court": "SUPREME COURT OF WESTERN AUSTRALIA", "rule_body": "Rules of the Supreme Court 1971 (WA)"},
    ],
    "New Zealand": [
        {"value": "NZ_HC", "label": "High Court",      "court": "HIGH COURT OF NEW ZEALAND",      "rule_body": "High Court Rules 2016"},
        {"value": "NZ_CA", "label": "Court of Appeal", "court": "COURT OF APPEAL OF NEW ZEALAND", "rule_body": "Court of Appeal (Civil) Rules 2005"},
        {"value": "NZSC",  "label": "Supreme Court",   "court": "SUPREME COURT OF NEW ZEALAND",   "rule_body": "Supreme Court Rules 2004"},
    ],
    "Ireland": [
        {"value": "IE_HC", "label": "High Court",      "court": "HIGH COURT",     "rule_body": "Rules of the Superior Courts (SI 15/1986)"},
        {"value": "IE_CA", "label": "Court of Appeal", "court": "COURT OF APPEAL","rule_body": "Rules of the Superior Courts"},
        {"value": "IE_SC", "label": "Supreme Court",   "court": "SUPREME COURT",  "rule_body": "Rules of the Superior Courts"},
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
    # Namespace by user ID so sessions are always isolated per account
    if current_user.is_authenticated:
        user_dir = os.path.join(SESSIONS_FOLDER, f"u{current_user.id}")
        os.makedirs(user_dir, exist_ok=True)
        return os.path.join(user_dir, f"{sid}.json")
    return os.path.join(SESSIONS_FOLDER, f"{sid}.json")

def _default_session():
    return {
        "items": [],           # flat individual documents
        "tabs": [],            # grouped tabs [{id, name, items:[...]}]
        "use_dividers": True,
        "doc_type": "application_record",
        "title": "", "court_file": "", "parties": "", "recitals": "",
        "country": "Canada", "jurisdiction": "ON",
        "custom_court": "", "custom_rules": "",
    }

def get_session(sid):
    path = session_path(sid)
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
        # Migrate: old flat-only sessions (items but no tabs)
        if "items" not in data:
            data["items"] = []
        if "tabs" not in data:
            data["tabs"] = []
        if "use_dividers" not in data:
            data["use_dividers"] = True
        # Remove legacy bundle_mode if present
        data.pop("bundle_mode", None)
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
        return len(PdfReader(filepath).pages)
    except Exception:
        return 0


def resolve_jurisdiction(country, jurisdiction_value):
    for j in JURISDICTIONS.get(country, []):
        if j["value"] == jurisdiction_value:
            return j["court"], j["rule_body"]
    return "", ""


def _make_file_item(f, ext):
    """Save an uploaded file, convert images, return item dict."""
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
    return {
        "id":           item_id,
        "filename":     base_name,
        "custom_name":  "",
        "filepath":     filepath,
        "page_count":   page_count,
        "file_type":    "image" if ext in IMAGE_EXTENSIONS else "pdf",
        "original_ext": ext,
    }


# ── PDF generation ───────────────────────────────────────────────────────────

def generate_cover_toc(doc_type, items, tabs, title, court_file, parties,
                       output_path, country="Canada", jurisdiction="ON",
                       custom_court="", custom_rules="", recitals="",
                       use_dividers=True):
    """
    items  — flat individual documents (each gets its own tab letter)
    tabs   — grouped tabs (one tab letter per group, sub-rows per doc)
    """
    tmpl = TEMPLATES.get(doc_type, {"header": doc_type.upper(), "tab_style": "alpha"})
    doc  = SimpleDocTemplate(
        output_path, pagesize=letter,
        rightMargin=1*inch, leftMargin=1.25*inch,
        topMargin=1.25*inch, bottomMargin=1.25*inch,
    )

    styles = getSampleStyleSheet()
    normal = styles["Normal"]
    normal.fontName = "Times-Roman"
    normal.fontSize = 12

    center_bold = ParagraphStyle("center_bold", parent=normal,
        alignment=TA_CENTER, fontName="Times-Bold",
        fontSize=14, spaceAfter=10, spaceBefore=4)
    center_normal = ParagraphStyle("center_normal", parent=normal,
        alignment=TA_CENTER, fontSize=12, spaceAfter=6, leading=18)
    small_center = ParagraphStyle("small_center", parent=normal,
        alignment=TA_CENTER, fontSize=10, spaceAfter=6, leading=15)
    toc_header_st = ParagraphStyle("toc_header", parent=normal,
        alignment=TA_CENTER, fontName="Times-Bold",
        fontSize=14, spaceAfter=16, spaceBefore=16)

    court_name, rule_body = resolve_jurisdiction(country, jurisdiction)
    if jurisdiction == "CUSTOM":
        court_name = custom_court
        rule_body  = custom_rules

    story = []

    # ── Cover Page ──────────────────────────────────────────────────────────
    story.append(Spacer(1, 0.75*inch))
    if country and country != "Other / Custom":
        story.append(Paragraph(country.upper(), center_bold))
        story.append(Spacer(1, 0.15*inch))
    if court_name:
        story.append(Paragraph(court_name, center_bold))
    story.append(Spacer(1, 0.4*inch))
    if court_file:
        right_st = ParagraphStyle("right_st", parent=normal,
            alignment=TA_RIGHT, fontSize=12, spaceAfter=6)
        story.append(Paragraph(f"Court File No.: {court_file}", right_st))
        story.append(Spacer(1, 0.25*inch))
    if parties:
        for line in parties.strip().split("\n"):
            story.append(Paragraph(line, center_normal))
        story.append(Spacer(1, 0.4*inch))
    story.append(Spacer(1, 0.3*inch))
    story.append(Paragraph(tmpl["header"], center_bold))
    if title:
        story.append(Spacer(1, 0.2*inch))
        story.append(Paragraph(title, center_normal))
    if rule_body:
        story.append(Spacer(1, 0.4*inch))
        story.append(Paragraph(rule_body, small_center))
    story.append(PageBreak())

    # ── Written Recitals ────────────────────────────────────────────────────
    if recitals and recitals.strip():
        recital_st = ParagraphStyle("recital", parent=normal,
            alignment=TA_LEFT, fontSize=11, leading=20, spaceAfter=10)
        story.append(Spacer(1, 0.6*inch))
        for para in recitals.strip().split("\n"):
            if para.strip():
                story.append(Paragraph(para.strip(), recital_st))
                story.append(Spacer(1, 0.1*inch))
        story.append(PageBreak())

    # ── Table of Contents ───────────────────────────────────────────────────
    story.append(Paragraph("TABLE OF CONTENTS", toc_header_st))

    tab_fn = alpha_label if tmpl["tab_style"] == "alpha" else numeric_label

    th_st  = ParagraphStyle("th",   parent=normal, fontName="Times-Bold", fontSize=11)
    th_rt  = ParagraphStyle("thr",  parent=normal, fontName="Times-Bold", fontSize=11, alignment=TA_RIGHT)
    row_st = ParagraphStyle("row",  parent=normal, fontSize=11, leading=16)
    row_rt = ParagraphStyle("rowr", parent=normal, fontSize=11, alignment=TA_RIGHT, leading=16)
    grp_st = ParagraphStyle("grp",  parent=normal, fontName="Times-Bold", fontSize=11, leading=16)
    grp_rt = ParagraphStyle("grpr", parent=normal, fontName="Times-Bold", fontSize=11, alignment=TA_RIGHT, leading=16)
    sub_st = ParagraphStyle("sub",  parent=normal, fontSize=10, leading=15)
    sub_rt = ParagraphStyle("subr", parent=normal, fontSize=10, alignment=TA_RIGHT, leading=15)

    toc_data = [[
        Paragraph("<b>Item</b>",      th_st),
        Paragraph("<b>Document</b>", th_st),
        Paragraph("<b>Page(s)</b>",  th_rt),
    ]]

    current_page = 1  # logical page number in the body (excluding dividers)
    item_num = 1      # global item counter — continues across individual items and tab sub-items

    # Individual items — numbered 1, 2, 3… continuing into tab sub-items
    for item in items:
        name     = item.get("custom_name") or item.get("filename", "Document")
        pc       = item.get("page_count", 1)
        page_str = str(current_page) if pc == 1 else f"{current_page}–{current_page+pc-1}"
        toc_data.append([
            Paragraph(str(item_num), row_st),
            Paragraph(name, row_st),
            Paragraph(page_str, row_rt),
        ])
        current_page += pc + (1 if use_dividers else 0)
        item_num += 1

    # Grouped tabs — Tab A, Tab B… sub-items continue numbering from individual items
    tab_shade = colors.Color(0.93, 0.91, 0.87)
    shaded_rows = []  # row indices for shading

    for grp_idx, tab in enumerate(tabs):
        label      = alpha_label(grp_idx)
        tab_name   = tab.get("name") or f"Tab {label}"
        tab_items  = tab.get("items", [])
        total_pc   = sum(i.get("page_count", 1) for i in tab_items) or 1
        tab_pg_str = str(current_page) if total_pc == 1 else f"{current_page}–{current_page+total_pc-1}"

        shaded_rows.append(len(toc_data))
        toc_data.append([
            Paragraph(f"<b>Tab {label}</b>", grp_st),
            Paragraph(f"<b>{tab_name}</b>",  grp_st),
            Paragraph(tab_pg_str,             grp_rt),
        ])

        doc_page = current_page
        for item in tab_items:
            pc   = item.get("page_count", 1)
            name = item.get("custom_name") or item.get("filename", "Document")
            ps   = str(doc_page) if pc == 1 else f"{doc_page}–{doc_page+pc-1}"
            toc_data.append([
                Paragraph(str(item_num), sub_st),
                Paragraph(f"  {name}", sub_st),
                Paragraph(ps, sub_rt),
            ])
            doc_page += pc
            item_num += 1

        current_page += total_pc + (1 if use_dividers else 0)

    # Build table style
    ts = [
        ("FONTNAME",      (0,0),(-1,0),  "Times-Bold"),
        ("FONTSIZE",      (0,0),(-1,-1), 11),
        ("LINEBELOW",     (0,0),(-1,0),  1,    colors.black),
        ("LINEBELOW",     (0,1),(-1,-1), 0.25, colors.lightgrey),
        ("VALIGN",        (0,0),(-1,-1), "TOP"),
        ("TOPPADDING",    (0,0),(-1,-1), 7),
        ("BOTTOMPADDING", (0,0),(-1,-1), 7),
        ("LEFTPADDING",   (0,0),(-1,-1), 4),
        ("RIGHTPADDING",  (0,0),(-1,-1), 4),
    ]
    for r in shaded_rows:
        ts.append(("BACKGROUND", (0,r),(-1,r), tab_shade))

    toc_table = Table(toc_data, colWidths=[0.9*inch, 4.5*inch, 0.8*inch])
    toc_table.setStyle(TableStyle(ts))
    story.append(toc_table)
    story.append(PageBreak())

    doc.build(story)


def generate_divider_page(tab_label, name, output_path):
    doc = SimpleDocTemplate(output_path, pagesize=letter,
        rightMargin=1*inch, leftMargin=1.25*inch,
        topMargin=2*inch, bottomMargin=1*inch)
    styles = getSampleStyleSheet()
    normal = styles["Normal"]
    story = [
        Spacer(1, 1.5*inch),
        Paragraph(f"TAB {tab_label}", ParagraphStyle("big", parent=normal,
            fontName="Times-Bold", fontSize=36, alignment=TA_CENTER, spaceAfter=24)),
        Paragraph(name, ParagraphStyle("nm", parent=normal,
            fontName="Times-Roman", fontSize=14, alignment=TA_CENTER, leading=20)),
        PageBreak(),
    ]
    doc.build(story)


def add_toc_links(writer, toc_page_index, items, tabs,
                  first_page_index, use_dividers=True):
    """Stamp clickable links on TOC rows for items and tab groups."""
    page        = writer.pages[toc_page_index]
    page_height = float(page.mediabox.height)
    page_width  = float(page.mediabox.width)
    left  = 1.0 * 72
    right = page_width - 1.0 * 72

    # Approximate Y of first data row (after margin + heading + header row)
    first_row_top = page_height - 90 - 56 - 30
    ROW_H = 30   # individual item rows and group summary rows
    SUB_H = 25   # sub-document rows within a group

    link_rows = []      # [(y_offset_from_first_row_top, row_height, target_pdf_page)]
    y = 0
    current_pdf = first_page_index

    # Individual items
    for item in items:
        target = current_pdf  # link to divider (or doc if no dividers)
        link_rows.append((y, ROW_H, target))
        y += ROW_H
        pc = item.get("page_count", 1)
        current_pdf += (1 + pc) if use_dividers else pc

    # Grouped tabs — summary row + sub-rows
    for tab in tabs:
        target = current_pdf  # link to tab divider (or first doc)
        link_rows.append((y, ROW_H, target))
        y += ROW_H
        doc_pdf = current_pdf + (1 if use_dividers else 0)
        for item in tab.get("items", []):
            link_rows.append((y, SUB_H, doc_pdf))
            y      += SUB_H
            pc      = item.get("page_count", 1)
            doc_pdf += pc
        total = sum(i.get("page_count", 1) for i in tab.get("items", []))
        current_pdf += (1 + total) if use_dividers else total

    for (y_off, h, target) in link_rows:
        row_top    = first_row_top - y_off
        row_bottom = row_top - h
        rect = RectangleObject([left, row_bottom, right, row_top])
        try:
            writer.add_annotation(
                page_number=toc_page_index,
                annotation=Link(rect=rect, target_page_index=target),
            )
        except Exception:
            pass


def merge_pdfs(session_data, output_path):
    doc_type     = session_data["doc_type"]
    items        = session_data.get("items", [])
    tabs         = session_data.get("tabs", [])
    use_dividers = session_data.get("use_dividers", True)
    tmpl         = TEMPLATES.get(doc_type, {"header": doc_type.upper(), "tab_style": "alpha"})
    tab_fn       = alpha_label

    writer = PdfWriter()

    # 1. Cover + TOC
    toc_path = os.path.join(OUTPUT_FOLDER, f"_toc_{uuid.uuid4().hex}.pdf")
    generate_cover_toc(
        doc_type, items, tabs,
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
    rdr = PdfReader(toc_path)
    cover_count = len(rdr.pages)
    for pg in rdr.pages:
        writer.add_page(pg)
    os.remove(toc_path)

    toc_page_index = cover_count - 1
    first_page_idx = cover_count
    # 2. Individual items — each gets its own numbered divider (if enabled) + doc pages
    for i, item in enumerate(items):
        label = str(i + 1)
        name  = item.get("custom_name") or item.get("filename", f"Document {label}")

        if use_dividers:
            div_path = os.path.join(OUTPUT_FOLDER, f"_div_{uuid.uuid4().hex}.pdf")
            generate_divider_page(label, name, div_path)
            for pg in PdfReader(div_path).pages:
                writer.add_page(pg)
            os.remove(div_path)

        doc_path = item.get("filepath")
        if doc_path and os.path.exists(doc_path):
            for pg in PdfReader(doc_path).pages:
                writer.add_page(pg)

    # 3. Grouped tabs — Tab A, Tab B… (independent alpha sequence)
    for grp_idx, tab in enumerate(tabs):
        label    = alpha_label(grp_idx)
        tab_name = tab.get("name") or f"Tab {label}"

        if use_dividers:
            div_path = os.path.join(OUTPUT_FOLDER, f"_div_{uuid.uuid4().hex}.pdf")
            generate_divider_page(label, tab_name, div_path)
            for pg in PdfReader(div_path).pages:
                writer.add_page(pg)
            os.remove(div_path)

        for item in tab.get("items", []):
            doc_path = item.get("filepath")
            if doc_path and os.path.exists(doc_path):
                for pg in PdfReader(doc_path).pages:
                    writer.add_page(pg)

    # 4. Stamp TOC links
    add_toc_links(writer, toc_page_index, items, tabs, first_page_idx, use_dividers)

    with open(output_path, "wb") as f:
        writer.write(f)


# ── Auth routes ──────────────────────────────────────────────────────────────

@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("home"))
    error = None
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm", "")
        if not email or not password:
            error = "Email and password are required."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        elif password != confirm:
            error = "Passwords do not match."
        elif User.query.filter_by(email=email).first():
            error = "An account with that email already exists."
        else:
            user = User(email=email)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            login_user(user, remember=True)
            return redirect(url_for("home"))
    return render_template("register.html", error=error)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("home"))
    error = None
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user     = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            login_user(user, remember=True)
            return redirect(request.args.get("next") or url_for("home"))
        error = "Invalid email or password."
    return render_template("login.html", error=error)


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/account")
@login_required
def account():
    return render_template("account.html", plans=PLANS)


@app.route("/pricing")
def pricing():
    return render_template("pricing.html", plans=PLANS)


@app.route("/upgrade/<plan>")
@login_required
def upgrade(plan):
    if plan not in PLANS:
        return redirect(url_for("account"))
    price_id = PLANS[plan]["price_id"]
    if not price_id or not stripe.api_key:
        return redirect(url_for("account"))

    # Create or retrieve Stripe customer
    if not current_user.stripe_customer_id:
        customer = stripe.Customer.create(email=current_user.email)
        current_user.stripe_customer_id = customer.id
        db.session.commit()

    base_url = request.host_url.rstrip("/")
    checkout = stripe.checkout.Session.create(
        customer=current_user.stripe_customer_id,
        payment_method_types=["card"],
        line_items=[{"price": price_id, "quantity": 1}],
        mode="subscription",
        success_url=f"{base_url}/upgrade/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{base_url}/account",
        metadata={"user_id": str(current_user.id), "plan": plan},
    )
    return redirect(checkout.url, code=303)


@app.route("/upgrade/success")
@login_required
def upgrade_success():
    return render_template("upgrade_success.html")


@app.route("/billing-portal")
@login_required
def billing_portal():
    if not current_user.stripe_customer_id or not stripe.api_key:
        return redirect(url_for("account"))
    base_url = request.host_url.rstrip("/")
    portal = stripe.billing_portal.Session.create(
        customer=current_user.stripe_customer_id,
        return_url=f"{base_url}/account",
    )
    return redirect(portal.url, code=303)


@app.route("/webhooks/stripe", methods=["POST"])
def stripe_webhook():
    payload   = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        return "Invalid signature", 400

    obj = event["data"]["object"]

    if event["type"] == "checkout.session.completed":
        user_id = obj.get("metadata", {}).get("user_id")
        plan    = obj.get("metadata", {}).get("plan")
        sub_id  = obj.get("subscription")
        if user_id and plan:
            user = User.query.get(int(user_id))
            if user:
                user.plan = plan
                user.stripe_subscription_id = sub_id
                db.session.commit()

    elif event["type"] in ("customer.subscription.updated", "customer.subscription.deleted"):
        sub    = obj
        status = sub.get("status")
        cust_id = sub.get("customer")
        user = User.query.filter_by(stripe_customer_id=cust_id).first()
        if user:
            if status in ("canceled", "unpaid", "incomplete_expired"):
                user.plan = "free"
                user.stripe_subscription_id = None
            elif status == "active":
                # Determine plan from price ID
                price_id = sub["items"]["data"][0]["price"]["id"]
                for plan_key, plan_data in PLANS.items():
                    if plan_data["price_id"] == price_id:
                        user.plan = plan_key
                        break
            db.session.commit()

    return "OK", 200


# ── Public pages ─────────────────────────────────────────────────────────────

@app.route("/")
def home():
    if current_user.is_authenticated:
        if "sid" not in session:
            session["sid"] = uuid.uuid4().hex
        return render_template("index.html")
    return render_template("landing.html")

@app.route("/privacy")
def privacy():
    return render_template("privacy.html")

@app.route("/terms")
def terms():
    return render_template("terms.html")


@app.route("/api/session", methods=["GET"])
@login_required
def get_session_data():
    sid = session.get("sid", uuid.uuid4().hex)
    session["sid"] = sid
    return jsonify(get_session(sid))


@app.route("/api/session", methods=["POST"])
@login_required
def update_session():
    sid  = session.get("sid")
    data = request.json
    sess = get_session(sid)
    for key in ("doc_type","title","court_file","parties","recitals",
                "country","jurisdiction","custom_court","custom_rules","use_dividers"):
        if key in data:
            sess[key] = data[key]
    save_session(sid, sess)
    return jsonify({"ok": True})


@app.route("/api/jurisdictions", methods=["GET"])
@login_required
def get_jurisdictions():
    return jsonify(JURISDICTIONS)


# ── Individual-item routes ───────────────────────────────────────────────────

@app.route("/api/upload", methods=["POST"])
@login_required
def upload():
    sid  = session.get("sid")
    sess = get_session(sid)
    added = []
    for f in request.files.getlist("files"):
        ext = os.path.splitext(f.filename.lower())[1]
        if ext not in ALLOWED_EXTENSIONS:
            continue
        item = _make_file_item(f, ext)
        sess["items"].append(item)
        added.append(item)
    save_session(sid, sess)
    return jsonify(added)


@app.route("/api/items", methods=["GET"])
@login_required
def get_items():
    sid = session.get("sid")
    return jsonify(get_session(sid).get("items", []))


@app.route("/api/items/reorder", methods=["POST"])
@login_required
def reorder_items():
    sid  = session.get("sid")
    sess = get_session(sid)
    order = request.json.get("order", [])
    id_map = {i["id"]: i for i in sess["items"]}
    sess["items"] = [id_map[x] for x in order if x in id_map]
    save_session(sid, sess)
    return jsonify({"ok": True})


@app.route("/api/items/<item_id>", methods=["PATCH"])
@login_required
def update_item(item_id):
    sid  = session.get("sid")
    sess = get_session(sid)
    data = request.json or {}
    for item in sess["items"]:
        if item["id"] == item_id:
            if "custom_name" in data:
                item["custom_name"] = data["custom_name"]
            break
    save_session(sid, sess)
    return jsonify({"ok": True})


@app.route("/api/items/<item_id>", methods=["DELETE"])
@login_required
def delete_item(item_id):
    sid  = session.get("sid")
    sess = get_session(sid)
    sess["items"] = [i for i in sess["items"] if i["id"] != item_id]
    save_session(sid, sess)
    return jsonify({"ok": True})


# ── Grouped-tab routes ───────────────────────────────────────────────────────

@app.route("/api/tabs", methods=["GET"])
@login_required
def get_tabs():
    sid = session.get("sid")
    return jsonify(get_session(sid).get("tabs", []))


@app.route("/api/tabs", methods=["POST"])
@login_required
def create_tab():
    sid  = session.get("sid")
    sess = get_session(sid)
    data = request.json or {}
    tab  = {"id": uuid.uuid4().hex, "name": data.get("name", ""), "items": []}
    sess["tabs"].append(tab)
    save_session(sid, sess)
    return jsonify(tab)


@app.route("/api/tabs/reorder", methods=["POST"])
@login_required
def reorder_tabs():
    sid  = session.get("sid")
    sess = get_session(sid)
    order = request.json.get("order", [])
    id_map = {t["id"]: t for t in sess["tabs"]}
    sess["tabs"] = [id_map[x] for x in order if x in id_map]
    save_session(sid, sess)
    return jsonify({"ok": True})


@app.route("/api/tabs/<tab_id>", methods=["PATCH"])
@login_required
def update_tab(tab_id):
    sid  = session.get("sid")
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
@login_required
def delete_tab(tab_id):
    sid  = session.get("sid")
    sess = get_session(sid)
    sess["tabs"] = [t for t in sess["tabs"] if t["id"] != tab_id]
    save_session(sid, sess)
    return jsonify({"ok": True})


@app.route("/api/tabs/<tab_id>/upload", methods=["POST"])
@login_required
def upload_to_tab(tab_id):
    sid  = session.get("sid")
    sess = get_session(sid)
    tab  = next((t for t in sess["tabs"] if t["id"] == tab_id), None)
    if tab is None:
        return jsonify({"error": "Tab not found"}), 404
    added = []
    for f in request.files.getlist("files"):
        ext = os.path.splitext(f.filename.lower())[1]
        if ext not in ALLOWED_EXTENSIONS:
            continue
        item = _make_file_item(f, ext)
        tab["items"].append(item)
        added.append(item)
    save_session(sid, sess)
    return jsonify(added)


@app.route("/api/tabs/<tab_id>/items/reorder", methods=["POST"])
@login_required
def reorder_tab_items(tab_id):
    sid  = session.get("sid")
    sess = get_session(sid)
    tab  = next((t for t in sess["tabs"] if t["id"] == tab_id), None)
    if tab is None:
        return jsonify({"error": "Tab not found"}), 404
    order  = request.json.get("order", [])
    id_map = {i["id"]: i for i in tab["items"]}
    tab["items"] = [id_map[x] for x in order if x in id_map]
    save_session(sid, sess)
    return jsonify({"ok": True})


@app.route("/api/tabs/<tab_id>/items/<item_id>", methods=["PATCH"])
@login_required
def update_tab_item(tab_id, item_id):
    sid  = session.get("sid")
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
@login_required
def delete_tab_item(tab_id, item_id):
    sid  = session.get("sid")
    sess = get_session(sid)
    for tab in sess["tabs"]:
        if tab["id"] == tab_id:
            tab["items"] = [i for i in tab["items"] if i["id"] != item_id]
            break
    save_session(sid, sess)
    return jsonify({"ok": True})


# ── Generate / Download / Reset ──────────────────────────────────────────────

@app.route("/api/generate", methods=["POST"])
@login_required
def generate():
    # Enforce free-tier bundle limit
    if not current_user.can_generate():
        return jsonify({
            "error": "You have used all 3 free bundles. Please upgrade to continue.",
            "upgrade": True
        }), 403

    sid  = session.get("sid")
    sess = get_session(sid)
    total = len(sess.get("items", [])) + sum(len(t.get("items", [])) for t in sess.get("tabs", []))
    if total == 0:
        return jsonify({"error": "No documents added yet. Please upload at least one file."}), 400
    out_name = f"bundle_{uuid.uuid4().hex[:8]}.pdf"
    out_path = os.path.join(OUTPUT_FOLDER, out_name)
    try:
        merge_pdfs(sess, out_path)
        # Track usage
        current_user.bundles_used += 1
        db.session.commit()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({
        "filename": out_name,
        "bundles_used": current_user.bundles_used,
        "plan": current_user.plan,
    })


@app.route("/api/download/<filename>")
@login_required
def download(filename):
    # Security: only allow filenames that belong to the current session
    safe_name = os.path.basename(filename)
    path = os.path.join(OUTPUT_FOLDER, safe_name)
    if not os.path.exists(path):
        return "Not found", 404

    response = send_file(path, as_attachment=True, download_name=safe_name)

    # Delete the generated bundle after sending (zero-storage)
    @response.call_on_close
    def cleanup():
        try:
            os.remove(path)
        except OSError:
            pass

    return response


@app.route("/api/reset", methods=["POST"])
@login_required
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

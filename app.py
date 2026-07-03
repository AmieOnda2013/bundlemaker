import os
import io
import json
import uuid
import shutil
from flask import Flask, render_template, request, jsonify, send_file, session, redirect, url_for, flash
from markupsafe import escape
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
import requests as http_requests
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
from models import db, User, PLAN_LIMITS

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "bundlemaker-dev-secret-change-in-production")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB max upload
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 365  # 1 year

# Comma-separated owner/admin emails — these accounts bypass all bundle limits and email verification
_OWNER_EMAILS = {e.strip().lower() for e in os.environ.get("OWNER_EMAILS", "").split(",") if e.strip()}

def is_owner(user=None):
    u = user or current_user
    return bool(_OWNER_EMAILS and getattr(u, "email", None) and u.email.lower() in _OWNER_EMAILS)

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

# ── Mail (Brevo HTTP API) ─────────────────────────────────────────────────────
BREVO_API_KEY     = os.environ.get("BREVO_API_KEY", "")
MAIL_FROM_EMAIL   = os.environ.get("MAIL_FROM_EMAIL", "noreply@bundlemaker.app")
MAIL_FROM_NAME    = os.environ.get("MAIL_FROM_NAME", "BundleMaker")

# ── Stripe ────────────────────────────────────────────────────────────────────
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

PLANS = {
    "solo": {
        "name": "Solo", "bundles": 20,
        "monthly": {"price": "$19/mo",  "price_id": os.environ.get("STRIPE_SOLO_MONTHLY_PRICE_ID", "")},
        "annual":  {"price": "$16/mo",  "price_id": os.environ.get("STRIPE_SOLO_ANNUAL_PRICE_ID",  ""), "total": "$192/yr"},
    },
    "professional": {
        "name": "Professional", "bundles": 60,
        "monthly": {"price": "$49/mo",  "price_id": os.environ.get("STRIPE_PRO_MONTHLY_PRICE_ID",  "")},
        "annual":  {"price": "$41/mo",  "price_id": os.environ.get("STRIPE_PRO_ANNUAL_PRICE_ID",   ""), "total": "$492/yr"},
    },
    "firm": {
        "name": "Firm", "bundles": None,
        "monthly": {"price": "$99/mo",  "price_id": os.environ.get("STRIPE_FIRM_MONTHLY_PRICE_ID", "")},
        "annual":  {"price": "$82/mo",  "price_id": os.environ.get("STRIPE_FIRM_ANNUAL_PRICE_ID",  ""), "total": "$984/yr"},
    },
}

# ── Auth ──────────────────────────────────────────────────────────────────────
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to access BundleMaker."

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

UPLOAD_FOLDER   = os.path.join(_BASE_DIR, "uploads")
OUTPUT_FOLDER   = os.path.join(_BASE_DIR, "output")
SESSIONS_FOLDER = os.path.join(_BASE_DIR, "sessions")

for _d in (UPLOAD_FOLDER, OUTPUT_FOLDER, SESSIONS_FOLDER):
    os.makedirs(_d, exist_ok=True)

_db_ready = False

def _migrate_db():
    """Add any missing columns to existing tables without losing data."""
    migrations = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verify_token VARCHAR(128)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS plan_period VARCHAR(20) DEFAULT 'monthly'",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS bundles_used INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS bundles_reset_date TIMESTAMP",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_subscription_id VARCHAR(255)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_customer_id VARCHAR(255)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_reset_token VARCHAR(128)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_reset_expires TIMESTAMP",
    ]
    for sql in migrations:
        try:
            db.session.execute(db.text(sql))
        except Exception as e:
            app.logger.warning(f"Migration skipped ({e})")
    db.session.commit()

@app.before_request
def _ensure_db():
    global _db_ready
    if not _db_ready:
        try:
            db.create_all()
            _migrate_db()
            _db_ready = True
        except Exception as e:
            app.logger.error(f"DB init error (will retry): {e}")

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
WORD_EXTENSIONS    = {".docx", ".doc"}
ALLOWED_EXTENSIONS = {".pdf"} | IMAGE_EXTENSIONS | WORD_EXTENSIONS

JURISDICTIONS = {
    "Canada": [
        {"value": "ON",     "label": "Ontario",                     "court": "SUPERIOR COURT OF JUSTICE",               "rule_body": "Rules of Civil Procedure, R.R.O. 1990, Reg. 194"},
        {"value": "ON_CA",  "label": "Ontario — Court of Appeal",   "court": "COURT OF APPEAL FOR ONTARIO",             "rule_body": "Rules of Civil Procedure, R.R.O. 1990, Reg. 194"},
        {"value": "BC",     "label": "British Columbia",             "court": "SUPREME COURT OF BRITISH COLUMBIA",       "rule_body": "Supreme Court Civil Rules, B.C. Reg. 168/2009"},
        {"value": "AB",     "label": "Alberta",                     "court": "COURT OF KING'S BENCH OF ALBERTA",        "rule_body": "Alberta Rules of Court, Alta. Reg. 124/2010"},
        {"value": "QC",     "label": "Québec",                      "court": "SUPERIOR COURT",                          "rule_body": "Code of Civil Procedure, CQLR c. C-25.01"},
        {"value": "MB",     "label": "Manitoba",                    "court": "COURT OF KING'S BENCH OF MANITOBA",       "rule_body": "King's Bench Rules, Man. Reg. 553/88"},
        {"value": "SK",     "label": "Saskatchewan",                "court": "COURT OF KING'S BENCH FOR SASKATCHEWAN",  "rule_body": "King's Bench Rules (Sask.)"},
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
        "title": "", "court_file": "", "place": "", "parties": "", "recitals": "",
        "country": "", "jurisdiction": "",
        "custom_court": "", "custom_rules": "",
        "col_header": "",      # optional extra column header (e.g. "Date", "Reference")
        "tab_prefix": "Tab",   # label prefix for groups (e.g. "Tab", "Exhibit", "Schedule")
        "col_item_header": "",   # TOC column: item/number header (default "#" / "Item")
        "col_doc_header": "",    # TOC column: document name header (default "Document")
        "col_page_header": "",   # TOC column: page(s) header (default "Page(s)")
    }

def get_session(sid):
    path = session_path(sid)
    if os.path.exists(path):
        try:
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, ValueError):
            app.logger.warning(f"Corrupted session {sid}, resetting")
            return _default_session()
        # Migrate: old flat-only sessions (items but no tabs)
        if "items" not in data:
            data["items"] = []
        if "tabs" not in data:
            data["tabs"] = []
        if "use_dividers" not in data:
            data["use_dividers"] = True
        if "col_header" not in data:
            data["col_header"] = ""
        if "tab_prefix" not in data:
            data["tab_prefix"] = "Tab"
        for _k in ("col_item_header", "col_doc_header", "col_page_header"):
            if _k not in data:
                data[_k] = ""
        # Remove legacy bundle_mode if present
        data.pop("bundle_mode", None)
        return data
    return _default_session()

def save_session(sid, data):
    path = session_path(sid)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


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


def word_to_pdf(word_path, pdf_path):
    """Convert a .docx (or .doc) file to PDF using python-docx + ReportLab."""
    from docx import Document as DocxDocument
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.lib.pagesizes import letter

    doc = DocxDocument(word_path)
    styles = getSampleStyleSheet()
    normal = styles["Normal"]
    normal.fontName = "Times-Roman"
    normal.fontSize = 11
    normal.leading  = 16

    heading_st = ParagraphStyle("wh", parent=normal, fontName="Times-Bold", fontSize=12, spaceAfter=6, spaceBefore=10)

    story = []
    for para in doc.paragraphs:
        text = para.text
        if not text.strip():
            story.append(Spacer(1, 6))
            continue
        style = heading_st if para.style.name.startswith("Heading") else normal
        # Preserve bold runs
        parts = []
        for run in para.runs:
            t = run.text.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
            if run.bold:
                parts.append(f"<b>{t}</b>")
            elif run.italic:
                parts.append(f"<i>{t}</i>")
            else:
                parts.append(t)
        html = "".join(parts) or text.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
        story.append(Paragraph(html, style))

    if not story:
        story.append(Paragraph("(empty document)", normal))

    doc_pdf = SimpleDocTemplate(pdf_path, pagesize=letter,
                                rightMargin=72, leftMargin=72,
                                topMargin=72, bottomMargin=72)
    doc_pdf.build(story)


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
    elif ext in WORD_EXTENSIONS:
        pdf_dest = os.path.join(UPLOAD_FOLDER, f"{item_id}.pdf")
        word_to_pdf(raw_dest, pdf_dest)
        os.remove(raw_dest)
        filepath = pdf_dest
    else:
        filepath = raw_dest
    base_name  = os.path.splitext(f.filename)[0].replace("_", " ").replace("-", " ")
    page_count = get_pdf_page_count(filepath)
    file_type  = "image" if ext in IMAGE_EXTENSIONS else ("word" if ext in WORD_EXTENSIONS else "pdf")
    return {
        "id":           item_id,
        "filename":     base_name,
        "custom_name":  "",
        "filepath":     filepath,
        "page_count":   page_count,
        "file_type":    file_type,
        "original_ext": ext,
    }


# ── PDF generation ───────────────────────────────────────────────────────────

def generate_cover_toc(doc_type, items, tabs, title, court_file, parties,
                       output_path, country="Canada", jurisdiction="ON",
                       custom_court="", custom_rules="", recitals="",
                       use_dividers=True, col_header="", tab_prefix="Tab",
                       col_item_header="", col_doc_header="", col_page_header="",
                       place=""):
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
    if not court_name:
        court_name = custom_court or jurisdiction
    if not rule_body:
        rule_body = custom_rules

    story = []

    # Shared styles used in cover page
    right_st = ParagraphStyle("right_st", parent=normal,
        alignment=TA_RIGHT, fontSize=12, spaceAfter=6)
    left_st = ParagraphStyle("left_st", parent=normal,
        alignment=TA_LEFT, fontSize=12, spaceAfter=0)
    name_st = ParagraphStyle("name_st", parent=normal,
        alignment=TA_CENTER, fontName="Times-Bold", fontSize=12, spaceAfter=0, leading=18)
    role_st = ParagraphStyle("role_st", parent=normal,
        alignment=TA_RIGHT, fontSize=12, spaceAfter=0, leading=18)
    and_st  = ParagraphStyle("and_st",  parent=normal,
        alignment=TA_CENTER, fontSize=11, spaceAfter=0, leading=20)

    # ── Cover Page ──────────────────────────────────────────────────────────
    story.append(Spacer(1, 0.75*inch))

    # Place (province / state / country label) — shown in italics above court name
    if place:
        story.append(Paragraph(f"<i>{place.upper()}</i>", center_bold))
        story.append(Spacer(1, 0.15*inch))
    elif country and country != "Other / Custom":
        story.append(Paragraph(f"<i>{country.upper()}</i>", center_bold))
        story.append(Spacer(1, 0.15*inch))

    if court_name:
        story.append(Paragraph(court_name, center_bold))
    story.append(Spacer(1, 0.4*inch))

    if court_file:
        story.append(Paragraph(f"Court File No.: {court_file}", right_st))
        story.append(Spacer(1, 0.2*inch))

    if parties:
        # Accept both new structured list and legacy plain string
        if isinstance(parties, list):
            structured = [p for p in parties if p.get("name") or p.get("role")]
        else:
            structured = None

        if structured:
            # Render in proper legal style:
            #   BETWEEN:  [blank]
            #             PARTY NAME (centered)           Role (right)
            #             — and —   (centered)
            #             PARTY NAME (centered)           Role (right)

            # "BETWEEN:" row
            between_st = ParagraphStyle("between_st", parent=normal,
                fontName="Times-Bold", fontSize=12, alignment=TA_LEFT, spaceAfter=0)
            story.append(Table(
                [[Paragraph("B E T W E E N :", between_st), ""]],
                colWidths=[1.5*inch, 4.5*inch],
                style=[("VALIGN",(0,0),(-1,-1),"TOP"), ("BOTTOMPADDING",(0,0),(-1,-1),8)],
            ))

            for i, party in enumerate(structured):
                name = party.get("name", "").strip().upper()
                role = party.get("role", "").strip()

                # Name (center col) + Role (right col) on same row
                row_tbl = Table(
                    [[Paragraph(name, name_st), Paragraph(role, role_st)]],
                    colWidths=[4.0*inch, 2.0*inch],
                    style=[
                        ("VALIGN",  (0,0), (-1,-1), "MIDDLE"),
                        ("TOPPADDING",    (0,0), (-1,-1), 2),
                        ("BOTTOMPADDING", (0,0), (-1,-1), 2),
                    ],
                )
                story.append(row_tbl)

                # "- and -" separator between parties (not after last)
                if i < len(structured) - 1:
                    story.append(Spacer(1, 0.05*inch))
                    story.append(Table(
                        [["", Paragraph("- and -", and_st), ""]],
                        colWidths=[1.5*inch, 3.0*inch, 1.5*inch],
                        style=[("VALIGN",(0,0),(-1,-1),"MIDDLE"), ("TOPPADDING",(0,0),(-1,-1),2), ("BOTTOMPADDING",(0,0),(-1,-1),2)],
                    ))
                    story.append(Spacer(1, 0.05*inch))
        else:
            # Legacy plain text
            for line in str(parties).strip().split("\n"):
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

    # Detect if any document has an extra column value — if so, add the column
    has_col = bool(col_header) or \
              any(i.get("doc_date") for i in items) or \
              any(i.get("doc_date") for t in tabs for i in t.get("items", []))
    col_label = col_header or "Date"
    date_st  = ParagraphStyle("date",  parent=normal, fontSize=10, leading=15)
    date_rt  = ParagraphStyle("dater", parent=normal, fontSize=10, alignment=TA_RIGHT, leading=15)

    h_item = col_item_header or "Item"
    h_doc  = col_doc_header  or "Document"
    h_page = col_page_header or "Page(s)"

    def make_header_row():
        if has_col:
            return [Paragraph(f"<b>{h_item}</b>", th_st),
                    Paragraph(f"<b>{h_doc}</b>",  th_st),
                    Paragraph(f"<b>{col_label}</b>", th_st),
                    Paragraph(f"<b>{h_page}</b>", th_rt)]
        return [Paragraph(f"<b>{h_item}</b>", th_st),
                Paragraph(f"<b>{h_doc}</b>",  th_st),
                Paragraph(f"<b>{h_page}</b>", th_rt)]

    def make_row(label_para, name_para, date_val, page_para):
        if has_col:
            return [label_para, name_para, Paragraph(date_val or "", date_st), page_para]
        return [label_para, name_para, page_para]

    toc_data = [make_header_row()]

    current_page = 1  # logical page number in the body (excluding dividers)
    item_num = 1      # global item counter — continues across individual items and tab sub-items

    # Individual items — numbered 1, 2, 3… — no tab dividers for individual items
    for item in items:
        name     = item.get("custom_name") or item.get("filename", "Document")
        pc       = item.get("page_count", 1)
        page_str = str(current_page) if pc == 1 else f"{current_page}–{current_page+pc-1}"
        toc_data.append(make_row(
            Paragraph(str(item_num), row_st),
            Paragraph(name, row_st),
            item.get("doc_date", ""),
            Paragraph(page_str, row_rt),
        ))
        current_page += pc  # individual items never have divider pages
        item_num += 1

    # Grouped tabs — Tab A, Tab B… sub-items continue global numbering
    tab_shade = colors.Color(0.93, 0.91, 0.87)
    shaded_rows = []  # row indices for shading

    for grp_idx, tab in enumerate(tabs):
        alpha_lbl      = alpha_label(grp_idx)
        tab_full_label = tab.get("label") or f"{tab_prefix} {alpha_lbl}"
        tab_name       = tab.get("name") or tab_full_label
        tab_items      = tab.get("items", [])
        total_pc       = sum(i.get("page_count", 1) for i in tab_items) or 1
        tab_pg_str     = str(current_page) if total_pc == 1 else f"{current_page}–{current_page+total_pc-1}"

        shaded_rows.append(len(toc_data))
        toc_data.append(make_row(
            Paragraph(f"<b>{tab_full_label}</b>", grp_st),
            Paragraph(f"<b>{tab_name}</b>",  grp_st),
            "",
            Paragraph(tab_pg_str, grp_rt),
        ))

        doc_page = current_page
        for item in tab_items:
            pc   = item.get("page_count", 1)
            name = item.get("custom_name") or item.get("filename", "Document")
            ps   = str(doc_page) if pc == 1 else f"{doc_page}–{doc_page+pc-1}"
            toc_data.append(make_row(
                Paragraph(str(item_num), sub_st),
                Paragraph(f"  {name}", sub_st),
                item.get("doc_date", ""),
                Paragraph(ps, sub_rt),
            ))
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

    # Fixed row heights so link Y-positions can be calculated exactly
    _ROW_H = 30
    _SUB_H = 25
    _HDR_H = 28
    row_heights = [_HDR_H]
    for _item in items:
        row_heights.append(_ROW_H)
    for _tab in tabs:
        row_heights.append(_ROW_H)
        for _ in _tab.get("items", []):
            row_heights.append(_SUB_H)

    max_label_len = max((len(t.get("label") or f"{tab_prefix} A") for t in tabs), default=len(tab_prefix) + 2)
    prefix_w = max(0.9, 0.55 + max_label_len * 0.065) * inch
    if has_col:
        col_widths = [prefix_w, 3.3*inch - (prefix_w - 0.9*inch), 1.4*inch, 0.8*inch]
    else:
        col_widths = [prefix_w, 4.5*inch - (prefix_w - 0.9*inch), 0.8*inch]
    toc_table = Table(toc_data, colWidths=col_widths, rowHeights=row_heights)
    toc_table.setStyle(TableStyle(ts))
    story.append(toc_table)
    story.append(PageBreak())

    doc.build(story)


def generate_divider_page(tab_full_label, name, output_path):
    doc = SimpleDocTemplate(output_path, pagesize=letter,
        rightMargin=1*inch, leftMargin=1.25*inch,
        topMargin=2*inch, bottomMargin=1*inch)
    styles = getSampleStyleSheet()
    normal = styles["Normal"]
    story = [
        Spacer(1, 1.5*inch),
        Paragraph(tab_full_label.upper(), ParagraphStyle("big", parent=normal,
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

    # Y of first data row: page_height minus top-margin(90) minus TOC-heading(49) minus header-row(28)
    # These constants must match the rowHeights and ParagraphStyle values in generate_cover_toc
    _TOP_MARGIN = 90
    _TOC_HDG_H  = 49   # spaceBefore(16) + leading(~17) + spaceAfter(16)
    _HDR_ROW_H  = 28   # table header row fixed height
    first_row_top = page_height - _TOP_MARGIN - _TOC_HDG_H - _HDR_ROW_H
    ROW_H = 30   # individual item rows and group summary rows (matches rowHeights above)
    SUB_H = 25   # sub-document rows within a group (matches rowHeights above)

    link_rows = []      # [(y_offset_from_first_row_top, row_height, target_pdf_page)]
    y = 0
    current_pdf = first_page_index

    # Individual items — no divider pages, link directly to document
    for item in items:
        link_rows.append((y, ROW_H, current_pdf))
        y += ROW_H
        pc = item.get("page_count", 1)
        current_pdf += pc  # individual items never have divider pages

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
        country=session_data.get("country", ""),
        jurisdiction=session_data.get("jurisdiction", ""),
        custom_court=session_data.get("custom_court", ""),
        custom_rules=session_data.get("custom_rules", ""),
        recitals=session_data.get("recitals", ""),
        use_dividers=use_dividers,
        col_header=session_data.get("col_header", ""),
        tab_prefix=session_data.get("tab_prefix", "Tab"),
        col_item_header=session_data.get("col_item_header", ""),
        col_doc_header=session_data.get("col_doc_header", ""),
        col_page_header=session_data.get("col_page_header", ""),
        place=session_data.get("place", ""),
    )
    rdr = PdfReader(toc_path)
    cover_count = len(rdr.pages)
    for pg in rdr.pages:
        writer.add_page(pg)
    os.remove(toc_path)

    toc_page_index = cover_count - 1
    first_page_idx = cover_count
    # 2. Individual items — no divider pages, just the document pages
    for item in items:
        doc_path = item.get("filepath")
        if doc_path and os.path.exists(doc_path):
            for pg in PdfReader(doc_path).pages:
                writer.add_page(pg)

    # 3. Grouped tabs — Tab A, Tab B… (independent alpha sequence)
    for grp_idx, tab in enumerate(tabs):
        alpha_lbl      = alpha_label(grp_idx)
        global_prefix  = session_data.get("tab_prefix", "Tab")
        tab_full_label = tab.get("label") or f"{global_prefix} {alpha_lbl}"
        tab_name       = tab.get("name") or tab_full_label

        if use_dividers:
            div_path = os.path.join(OUTPUT_FOLDER, f"_div_{uuid.uuid4().hex}.pdf")
            generate_divider_page(tab_full_label, tab_name, div_path)
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
        elif db.session.execute(db.select(User).filter_by(email=email)).scalar_one_or_none():
            error = "An account with that email already exists."
        else:
            user = User(email=email)
            user.set_password(password)
            token = user.generate_verify_token()
            db.session.add(user)
            db.session.commit()
            _send_verification_email(user, token)
            login_user(user, remember=True)
            flash("Account created! Please check your email to verify your address before generating bundles.", "info")
            return redirect(url_for("home"))
    return render_template("register.html", error=error)


def _send_verification_email(user, token):
    verify_url = url_for("verify_email", token=token, _external=True)
    recipient = user.email
    html = f"""
    <p>Welcome to BundleMaker!</p>
    <p>Please click the link below to verify your email address and unlock your 3 free bundle generations:</p>
    <p><a href="{verify_url}" style="background:#c9a84c;color:#fff;padding:12px 24px;border-radius:6px;text-decoration:none;font-weight:600;">Verify Email Address</a></p>
    <p>Or copy this link: {verify_url}</p>
    <p>If you did not create a BundleMaker account, you can safely ignore this email.</p>
    """
    def _send(app_):
        with app_.app_context():
            try:
                resp = http_requests.post(
                    "https://api.brevo.com/v3/smtp/email",
                    headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
                    json={
                        "sender": {"name": MAIL_FROM_NAME, "email": MAIL_FROM_EMAIL},
                        "to": [{"email": recipient}],
                        "subject": "Verify your BundleMaker email",
                        "htmlContent": html,
                    },
                    timeout=15,
                )
                if resp.status_code >= 300:
                    app_.logger.error(f"Brevo API error: {resp.status_code} {resp.text}")
            except Exception as e:
                app_.logger.error(f"Failed to send verification email: {e}")
    import threading
    threading.Thread(target=_send, args=(app,), daemon=True).start()


@app.route("/verify-email/<token>")
def verify_email(token):
    user = db.session.execute(db.select(User).filter_by(email_verify_token=token)).scalar_one_or_none()
    if not user:
        flash("Invalid or expired verification link.", "error")
        return redirect(url_for("login"))
    user.email_verified = True
    user.email_verify_token = None
    db.session.commit()
    flash("Email verified! You can now generate bundles.", "success")
    return redirect(url_for("home"))


@app.route("/resend-verification")
@login_required
def resend_verification():
    if current_user.email_verified:
        return redirect(url_for("home"))
    token = current_user.generate_verify_token()
    db.session.commit()
    _send_verification_email(current_user, token)
    flash("Verification email resent. Please check your inbox.", "info")
    return redirect(url_for("home"))



@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if current_user.is_authenticated:
        return redirect(url_for("home"))
    sent = False
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user  = db.session.execute(db.select(User).filter_by(email=email)).scalar_one_or_none()
        if user:
            token = user.generate_reset_token()
            db.session.commit()
            reset_url = url_for("reset_password", token=token, _external=True)
            html = f"""
            <p>You requested a password reset for your BundleMaker account.</p>
            <p><a href="{reset_url}" style="background:#c9a84c;color:#fff;padding:12px 24px;border-radius:6px;text-decoration:none;font-weight:600;">Reset Password</a></p>
            <p>Or copy this link: {reset_url}</p>
            <p>This link expires in 1 hour. If you did not request this, you can safely ignore this email.</p>
            """
            def _send(app_):
                with app_.app_context():
                    try:
                        http_requests.post(
                            "https://api.brevo.com/v3/smtp/email",
                            headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
                            json={
                                "sender": {"name": MAIL_FROM_NAME, "email": MAIL_FROM_EMAIL},
                                "to": [{"email": email}],
                                "subject": "Reset your BundleMaker password",
                                "htmlContent": html,
                            },
                            timeout=15,
                        )
                    except Exception as e:
                        app_.logger.error(f"Failed to send reset email: {e}")
            import threading
            threading.Thread(target=_send, args=(app,), daemon=True).start()
        # Always show the same message so we don't reveal whether the email exists
        sent = True
    return render_template("forgot_password.html", sent=sent)


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    import datetime
    if current_user.is_authenticated:
        return redirect(url_for("home"))
    user = db.session.execute(db.select(User).filter_by(password_reset_token=token)).scalar_one_or_none()
    if not user or not user.password_reset_expires or user.password_reset_expires < datetime.datetime.utcnow():
        return render_template("reset_password.html", invalid=True)
    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm", "")
        if len(password) < 8:
            error = "Password must be at least 8 characters."
        elif password != confirm:
            error = "Passwords do not match."
        else:
            user.set_password(password)
            user.password_reset_token   = None
            user.password_reset_expires = None
            db.session.commit()
            flash("Password reset successfully. You can now log in.", "success")
            return redirect(url_for("login"))
    return render_template("reset_password.html", invalid=False, error=error, token=token)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("home"))
    error = None
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user     = db.session.execute(db.select(User).filter_by(email=email)).scalar_one_or_none()
        if user and user.check_password(password):
            login_user(user, remember=True)
            next_url = request.args.get("next", "")
            if next_url and not next_url.startswith("/"):
                next_url = ""
            return redirect(next_url or url_for("home"))
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
    return render_template("account.html", plans=PLANS, plan_limits=PLAN_LIMITS)


@app.route("/pricing")
def pricing():
    return render_template("pricing.html", plans=PLANS)


@app.route("/upgrade/<plan>")
@login_required
def upgrade(plan):
    period = request.args.get("period", "monthly")  # monthly | annual
    if plan not in PLANS or period not in ("monthly", "annual"):
        return redirect(url_for("account"))
    price_id = PLANS[plan][period]["price_id"]
    if not price_id or not stripe.api_key:
        flash("Stripe is not configured yet. Please contact support.", "error")
        return redirect(url_for("account"))

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
        cancel_url=f"{base_url}/pricing",
        metadata={"user_id": str(current_user.id), "plan": plan, "period": period},
    )
    return redirect(checkout.url, code=303)


@app.route("/upgrade/success")
@login_required
def upgrade_success():
    plan = current_user.plan
    plan_data = PLANS.get(plan, {})
    bundles = plan_data.get("bundles")  # None = unlimited
    plan_name = plan_data.get("name", plan.capitalize())
    return render_template("upgrade_success.html",
        plan_name=plan_name,
        bundles=bundles,
    )


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
    import datetime
    payload    = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")

    if not STRIPE_WEBHOOK_SECRET:
        app.logger.error("STRIPE_WEBHOOK_SECRET is not set — cannot verify webhook")
        return "Webhook secret not configured", 500

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except ValueError as e:
        app.logger.error(f"Stripe webhook bad payload: {e}")
        return "Invalid payload", 400
    except stripe.error.SignatureVerificationError as e:
        app.logger.error(f"Stripe webhook signature failed: {e}")
        return "Invalid signature", 400

    app.logger.info(f"Stripe webhook received: {event['type']}")
    obj = event["data"]["object"]

    if event["type"] == "checkout.session.completed":
        user_id = obj.get("metadata", {}).get("user_id")
        plan    = obj.get("metadata", {}).get("plan")
        period  = obj.get("metadata", {}).get("period", "monthly")
        sub_id  = obj.get("subscription")
        app.logger.info(f"checkout.session.completed: user_id={user_id} plan={plan} period={period}")
        if user_id and plan:
            user = db.session.get(User, int(user_id))
            if user:
                user.plan                  = plan
                user.plan_period           = period
                user.stripe_subscription_id = sub_id
                user.email_verified        = True   # paying users are always verified
                user.bundles_used          = 0
                user.bundles_reset_date    = datetime.datetime.utcnow() + datetime.timedelta(days=30)
                db.session.commit()
                app.logger.info(f"Updated user {user.email} to plan={plan}")
            else:
                app.logger.error(f"checkout.session.completed: no user found for id={user_id}")
        else:
            app.logger.error(f"checkout.session.completed: missing metadata user_id or plan in {obj.get('metadata')}")

    elif event["type"] in ("customer.subscription.updated", "customer.subscription.deleted"):
        sub     = obj
        status  = sub.get("status")
        cust_id = sub.get("customer")
        app.logger.info(f"{event['type']}: customer={cust_id} status={status}")
        user = db.session.execute(db.select(User).filter_by(stripe_customer_id=cust_id)).scalar_one_or_none()
        if user:
            if status in ("canceled", "unpaid", "incomplete_expired"):
                user.plan = "free"
                user.plan_period = "monthly"
                user.stripe_subscription_id = None
                app.logger.info(f"Reverted {user.email} to free (status={status})")
            elif status == "active":
                price_id = sub["items"]["data"][0]["price"]["id"]
                for plan_key, plan_data in PLANS.items():
                    for pd in ("monthly", "annual"):
                        if plan_data[pd]["price_id"] == price_id:
                            if user.plan != plan_key:
                                user.bundles_used = 0
                                user.bundles_reset_date = datetime.datetime.utcnow() + datetime.timedelta(days=30)
                            user.plan = plan_key
                            user.plan_period = pd
                            app.logger.info(f"Updated {user.email} to {plan_key} ({pd}) via subscription event")
                            break
            db.session.commit()
        else:
            app.logger.warning(f"subscription event: no user found for stripe_customer_id={cust_id}")

    return "OK", 200


# ── Owner admin ───────────────────────────────────────────────────────────────

@app.route("/admin/set-plan", methods=["GET", "POST"])
@login_required
def admin_set_plan():
    if not is_owner():
        return "Forbidden", 403
    import datetime
    message = None
    users = db.session.execute(db.select(User).order_by(User.id)).scalars().all()
    if request.method == "POST":
        target_email = request.form.get("email", "").strip().lower()
        new_plan     = request.form.get("plan", "free")
        period       = request.form.get("period", "monthly")
        target = db.session.execute(db.select(User).filter_by(email=target_email)).scalar_one_or_none()
        if not target:
            message = f"No user found with email: {target_email}"
        else:
            target.plan        = new_plan
            target.plan_period = period
            target.email_verified = True
            if new_plan != "free":
                target.bundles_used = 0
                target.bundles_reset_date = datetime.datetime.utcnow() + datetime.timedelta(days=30)
            db.session.commit()
            message = f"Updated {target_email} → {new_plan} ({period})"
    html = f"""<!DOCTYPE html><html><head><title>Admin — Set Plan</title>
    <style>body{{font-family:sans-serif;max-width:520px;margin:40px auto;padding:0 20px}}
    input,select{{width:100%;padding:8px;margin:6px 0 14px;box-sizing:border-box;border:1px solid #ccc;border-radius:4px}}
    button{{background:#0d1b2a;color:#c9a84c;padding:10px 24px;border:none;border-radius:4px;cursor:pointer;font-weight:700}}
    .msg{{background:#e8f5e9;padding:10px;border-radius:4px;margin-bottom:16px;color:#2e7d32}}
    table{{width:100%;border-collapse:collapse;margin-top:24px;font-size:0.85rem}}
    td,th{{padding:6px 8px;border:1px solid #ddd;text-align:left}}</style></head><body>
    <h2>Admin — Set User Plan</h2>
    {'<div class="msg">'+message+'</div>' if message else ''}
    <form method="POST">
      <label>User email</label>
      <input name="email" type="email" required placeholder="user@example.com"/>
      <label>Plan</label>
      <select name="plan">
        <option value="free">Free</option>
        <option value="solo" selected>Solo</option>
        <option value="professional">Professional</option>
        <option value="firm">Firm</option>
      </select>
      <label>Period</label>
      <select name="period">
        <option value="monthly" selected>Monthly</option>
        <option value="annual">Annual</option>
      </select>
      <button type="submit">Update Plan</button>
    </form>
    <table><tr><th>Email</th><th>Plan</th><th>Bundles used</th><th>Verified</th></tr>
    {''.join(f"<tr><td>{escape(u.email)}</td><td>{escape(u.plan)}</td><td>{u.bundles_used}</td><td>{'✓' if u.email_verified else '✗'}</td></tr>" for u in users)}
    </table></body></html>"""
    return html


# ── Public pages ─────────────────────────────────────────────────────────────

@app.route("/")
def home():
    if current_user.is_authenticated:
        if "sid" not in session:
            session["sid"] = uuid.uuid4().hex
        return render_template("index.html")
    return render_template("landing.html")

@app.errorhandler(500)
def internal_error(e):
    import traceback
    orig = getattr(e, "original_exception", e)
    tb = traceback.format_exc()
    app.logger.error(f"500 error: {orig}\n{tb}")
    try:
        db.session.rollback()
    except Exception:
        pass
    return render_template("error.html", message="Something went wrong on our end. Please try again or contact support@bundlemaker.app."), 500

@app.errorhandler(413)
def file_too_large(e):
    return jsonify({"error": "File too large. Maximum upload size is 50 MB per request."}), 413


@app.route("/health")
def health():
    status = {"app": "ok", "db": "unknown", "db_url_type": "unknown"}
    db_url = app.config.get("SQLALCHEMY_DATABASE_URI", "")
    status["db_url_type"] = "postgresql" if "postgresql" in db_url else "sqlite"
    try:
        db.session.execute(db.text("SELECT 1"))
        status["db"] = "connected"
    except Exception as e:
        status["db"] = f"error: {e}"
    from flask import jsonify
    return jsonify(status)

@app.route("/privacy")
def privacy():
    return render_template("privacy.html")

@app.route("/terms")
def terms():
    return render_template("terms.html")


def _get_sid():
    """Always returns a valid sid, initializing the session if needed."""
    if "sid" not in session:
        session["sid"] = uuid.uuid4().hex
    return session["sid"]


@app.route("/api/session", methods=["GET"])
@login_required
def get_session_data():
    sid = _get_sid()
    return jsonify(get_session(sid))


@app.route("/api/session", methods=["POST"])
@login_required
def update_session():
    if "sid" not in session:
        session["sid"] = uuid.uuid4().hex
    sid  = _get_sid()
    data = request.json
    sess = get_session(sid)
    for key in ("doc_type","title","court_file","place","parties","recitals",
                "country","jurisdiction","custom_court","custom_rules","use_dividers","col_header","tab_prefix",
                "col_item_header","col_doc_header","col_page_header"):
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
    if "sid" not in session:
        session["sid"] = uuid.uuid4().hex
    sid  = _get_sid()
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
    sid = _get_sid()
    return jsonify(get_session(sid).get("items", []))


@app.route("/api/items/reorder", methods=["POST"])
@login_required
def reorder_items():
    sid  = _get_sid()
    sess = get_session(sid)
    order = request.json.get("order", [])
    id_map = {i["id"]: i for i in sess["items"]}
    sess["items"] = [id_map[x] for x in order if x in id_map]
    save_session(sid, sess)
    return jsonify({"ok": True})


@app.route("/api/items/transfer", methods=["POST"])
@login_required
def transfer_item():
    sid  = _get_sid()
    sess = get_session(sid)
    data      = request.json or {}
    item_id   = data.get("item_id")
    to_section = data.get("to_section")   # "individual" or "tab"
    to_tab_id  = data.get("to_tab_id")
    before_id  = data.get("before_id")

    # Find and remove from current location
    item = None
    for i, it in enumerate(sess["items"]):
        if it["id"] == item_id:
            item = sess["items"].pop(i)
            break
    if item is None:
        for tab in sess["tabs"]:
            for i, it in enumerate(tab["items"]):
                if it["id"] == item_id:
                    item = tab["items"].pop(i)
                    break
            if item is not None:
                break

    if item is None:
        return jsonify({"error": "not found"}), 404

    # Insert at destination
    if to_section == "individual":
        dest = sess["items"]
        idx  = next((i for i, it in enumerate(dest) if it["id"] == before_id), len(dest))
        dest.insert(idx, item)
    else:
        for tab in sess["tabs"]:
            if tab["id"] == to_tab_id:
                dest = tab["items"]
                idx  = next((i for i, it in enumerate(dest) if it["id"] == before_id), len(dest))
                dest.insert(idx, item)
                break

    save_session(sid, sess)
    return jsonify({"ok": True, "item": item})


@app.route("/api/items/<item_id>", methods=["PATCH"])
@login_required
def update_item(item_id):
    sid  = _get_sid()
    sess = get_session(sid)
    data = request.json or {}
    for item in sess["items"]:
        if item["id"] == item_id:
            if "custom_name" in data:
                item["custom_name"] = data["custom_name"]
            if "doc_date" in data:
                item["doc_date"] = data["doc_date"]
            break
    save_session(sid, sess)
    return jsonify({"ok": True})


@app.route("/api/items/<item_id>", methods=["DELETE"])
@login_required
def delete_item(item_id):
    sid  = _get_sid()
    sess = get_session(sid)
    sess["items"] = [i for i in sess["items"] if i["id"] != item_id]
    save_session(sid, sess)
    return jsonify({"ok": True})


# ── Grouped-tab routes ───────────────────────────────────────────────────────

@app.route("/api/tabs", methods=["GET"])
@login_required
def get_tabs():
    sid = _get_sid()
    return jsonify(get_session(sid).get("tabs", []))


@app.route("/api/tabs", methods=["POST"])
@login_required
def create_tab():
    sid  = _get_sid()
    sess = get_session(sid)
    data = request.json or {}
    tab  = {"id": uuid.uuid4().hex, "name": data.get("name", ""), "label": "", "items": []}
    sess["tabs"].append(tab)
    save_session(sid, sess)
    return jsonify(tab)


@app.route("/api/tabs/reorder", methods=["POST"])
@login_required
def reorder_tabs():
    sid  = _get_sid()
    sess = get_session(sid)
    order = request.json.get("order", [])
    id_map = {t["id"]: t for t in sess["tabs"]}
    sess["tabs"] = [id_map[x] for x in order if x in id_map]
    save_session(sid, sess)
    return jsonify({"ok": True})


@app.route("/api/tabs/<tab_id>", methods=["PATCH"])
@login_required
def update_tab(tab_id):
    sid  = _get_sid()
    sess = get_session(sid)
    data = request.json or {}
    for tab in sess["tabs"]:
        if tab["id"] == tab_id:
            if "name" in data:
                tab["name"] = data["name"]
            if "label" in data:
                tab["label"] = data["label"]
            break
    save_session(sid, sess)
    return jsonify({"ok": True})


@app.route("/api/tabs/<tab_id>", methods=["DELETE"])
@login_required
def delete_tab(tab_id):
    sid  = _get_sid()
    sess = get_session(sid)
    sess["tabs"] = [t for t in sess["tabs"] if t["id"] != tab_id]
    save_session(sid, sess)
    return jsonify({"ok": True})


@app.route("/api/tabs/<tab_id>/upload", methods=["POST"])
@login_required
def upload_to_tab(tab_id):
    if "sid" not in session:
        session["sid"] = uuid.uuid4().hex
    sid  = _get_sid()
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
    sid  = _get_sid()
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
    sid  = _get_sid()
    sess = get_session(sid)
    data = request.json or {}
    for tab in sess["tabs"]:
        if tab["id"] == tab_id:
            for item in tab["items"]:
                if item["id"] == item_id:
                    if "custom_name" in data:
                        item["custom_name"] = data["custom_name"]
                    if "doc_date" in data:
                        item["doc_date"] = data["doc_date"]
                    break
            break
    save_session(sid, sess)
    return jsonify({"ok": True})


@app.route("/api/tabs/<tab_id>/items/<item_id>", methods=["DELETE"])
@login_required
def delete_tab_item(tab_id, item_id):
    sid  = _get_sid()
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
    # Reset monthly counter if due
    current_user.reset_monthly_bundles_if_due()
    db.session.commit()

    if not current_user.email_verified and not is_owner():
        return jsonify({
            "error": "Please verify your email address before generating bundles. Check your inbox for a verification link.",
            "verify": True
        }), 403

    if not current_user.can_generate() and not is_owner():
        limit = PLAN_LIMITS.get(current_user.plan, 0)
        if current_user.plan == "free":
            msg = "You have used all 3 free bundles. Upgrade to continue."
        else:
            msg = f"You have reached your {limit} bundle limit for this month. Upgrade for more."
        return jsonify({"error": msg, "upgrade": True}), 403

    sid  = _get_sid()
    sess = get_session(sid)
    total = len(sess.get("items", [])) + sum(len(t.get("items", [])) for t in sess.get("tabs", []))
    if total == 0:
        return jsonify({"error": "No documents added yet. Please upload at least one file."}), 400
    out_name = f"bundle_{uuid.uuid4().hex[:8]}.pdf"
    out_path = os.path.join(OUTPUT_FOLDER, out_name)
    try:
        merge_pdfs(sess, out_path)
        if not is_owner():
            current_user.bundles_used += 1
            db.session.commit()
        # Clean up uploaded source files now that the bundle is built
        all_items = list(sess.get("items", []))
        for t in sess.get("tabs", []):
            all_items.extend(t.get("items", []))
        for item in all_items:
            fp = item.get("filepath")
            if fp and os.path.exists(fp):
                try:
                    os.remove(fp)
                except OSError:
                    pass
    except Exception as e:
        app.logger.error(f"Generate error: {e}", exc_info=True)
        return jsonify({"error": "Failed to generate bundle. Please try again."}), 500
    session["last_bundle"] = out_name
    return jsonify({
        "filename": out_name,
        "bundles_used": current_user.bundles_used,
        "plan": current_user.plan,
    })


@app.route("/api/download/<filename>")
@login_required
def download(filename):
    safe_name = os.path.basename(filename)
    # Only allow downloading the bundle this user just generated
    if session.get("last_bundle") != safe_name:
        return "Forbidden", 403
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
    sid = _get_sid()
    save_session(sid, _default_session())
    return jsonify({"ok": True})


if __name__ == "__main__":
    os.makedirs(UPLOAD_FOLDER,   exist_ok=True)
    os.makedirs(OUTPUT_FOLDER,   exist_ok=True)
    os.makedirs(SESSIONS_FOLDER, exist_ok=True)
    port = int(os.environ.get("PORT", 5050))
    app.run(debug=False, port=port)

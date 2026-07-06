import os
import io
import json
import uuid
import shutil
import threading
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
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB max upload
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

# One-time top-up: 20 bundles for $19
TOPUP_PRICE_ID = os.environ.get("STRIPE_TOPUP_PRICE_ID", "")
TOPUP_BUNDLES  = 20
TOPUP_PRICE    = "$19"

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

# ── Background job tracker (file-based so all Gunicorn workers can share) ────
import json as _json

def _job_path(job_id):
    return os.path.join(OUTPUT_FOLDER, f"job_{job_id}.json")

def _job_set(job_id, **kwargs):
    p = _job_path(job_id)
    try:
        existing = _json.loads(open(p).read()) if os.path.exists(p) else {}
    except Exception:
        existing = {}
    existing.update(kwargs)
    with open(p, "w") as f:
        _json.dump(existing, f)

def _job_get(job_id):
    p = _job_path(job_id)
    try:
        return _json.loads(open(p).read())
    except Exception:
        return {}

def _job_delete(job_id):
    p = _job_path(job_id)
    try:
        os.remove(p)
    except OSError:
        pass
# ─────────────────────────────────────────────────────────────────────────────

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
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS topup_bundles INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_topup_session VARCHAR(255)",
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
        "entries": [],         # flat entries list (docs + dividers interleaved)
        "use_dividers": True,
        "doc_type": "application_record",
        "title": "", "court_file": "", "place": "", "region": "", "parties": "", "recitals": "",
        "country": "", "jurisdiction": "",
        "custom_court": "", "custom_rules": "",
        "col_header": "",      # optional extra column header (e.g. "Date", "Reference")
        "tab_prefix": "Tab",   # label prefix for groups (e.g. "Tab", "Exhibit", "Schedule")
        "col_item_header": "",   # TOC column: item/number header (default "#" / "Item")
        "col_doc_header": "",    # TOC column: document name header (default "Document")
        "col_page_header": "",   # TOC column: page(s) header (default "Page(s)")
        "counsel": "",           # your counsel block (right side of cover)
        "opp_counsel": "",       # opposing counsel block (left side / TO:)
        "page_break_after_recital": False,
        "section_title": "",     # optional divider title before individual documents
        "section_desc": "",      # optional description below section title
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
        if "entries" not in data:
            data["entries"] = []
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
    # Re-ensure directory exists (Railway ephemeral FS can lose subdirs)
    dir_path = os.path.dirname(path)
    if dir_path:
        os.makedirs(dir_path, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    try:
        os.replace(tmp, path)
    except OSError:
        # Fallback: write directly if rename fails (cross-device or missing dir)
        os.makedirs(dir_path, exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f)
        try:
            os.remove(tmp)
        except OSError:
            pass


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
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.lib.utils import ImageReader

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
    draw_w = iw * scale
    draw_h = ih * scale
    x = (page_w - draw_w) / 2
    y = (page_h - draw_h) / 2

    img_buf = io.BytesIO()
    img.save(img_buf, "JPEG", quality=92)
    img_buf.seek(0)

    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=letter)
    c.drawImage(ImageReader(img_buf), x, y, width=draw_w, height=draw_h)
    c.showPage()
    c.save()

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


class UploadRejected(Exception):
    """Raised when an uploaded file can't be used, with a user-facing reason."""
    pass


def _decrypt_pdf_if_needed(filepath):
    """Court-filed and bank PDFs are often AES-"secured" with an empty user
    password. Rewrite them decrypted so merging never hits encryption.
    Raises UploadRejected if the PDF is unreadable or needs a real password."""
    try:
        rdr = PdfReader(filepath)
    except Exception:
        raise UploadRejected("The file is damaged or is not a valid PDF. Try re-saving or re-exporting it.")
    if not rdr.is_encrypted:
        return
    try:
        if not rdr.decrypt(""):
            raise UploadRejected("The PDF is password-protected. Remove the password (open it, enter the password, then print/save as a new PDF) and upload again.")
        w = PdfWriter()
        for pg in rdr.pages:
            w.add_page(pg)
        tmp = filepath + ".dec"
        with open(tmp, "wb") as fh:
            w.write(fh)
        os.replace(tmp, filepath)
    except UploadRejected:
        raise
    except Exception as e:
        app.logger.warning(f"Could not decrypt {filepath}: {e}")
        raise UploadRejected("The PDF uses encryption that could not be removed. Print/save it as a new PDF and upload again.")


def _make_file_item(f, ext):
    """Save an uploaded file, convert images, return item dict."""
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)  # re-ensure dir on ephemeral FS
    os.makedirs(OUTPUT_FOLDER, exist_ok=True)
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
        try:
            _decrypt_pdf_if_needed(filepath)
        except UploadRejected:
            try: os.remove(raw_dest)
            except OSError: pass
            raise
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
                       place="", page_offset=0, entries=None,
                       counsel="", opp_counsel="",
                       page_break_after_recital=False):
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
        alignment=TA_CENTER, fontName="Times-Italic", fontSize=10, spaceAfter=6, leading=15)
    toc_header_st = ParagraphStyle("toc_header", parent=normal,
        alignment=TA_CENTER, fontName="Times-Bold",
        fontSize=14, leading=18, spaceAfter=16, spaceBefore=16)

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
        alignment=TA_RIGHT, fontName="Times-Italic", fontSize=12, spaceAfter=0, leading=18)
    and_st  = ParagraphStyle("and_st",  parent=normal,
        alignment=TA_CENTER, fontSize=11, spaceAfter=0, leading=20)

    # ── Cover Page ──────────────────────────────────────────────────────────
    story.append(Spacer(1, 0.75*inch))

    # Country/place label above court name — plain (not italic)
    if place:
        story.append(Paragraph(place.upper(), center_bold))
        story.append(Spacer(1, 0.15*inch))
    elif country and country != "Other / Custom":
        story.append(Paragraph(country.upper(), center_bold))
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

                # Name on its own line (centered bold), role on next line (right-aligned)
                name_tbl = Table(
                    [[Paragraph(name, name_st)]],
                    colWidths=[6.0*inch],
                    style=[
                        ("TOPPADDING",    (0,0), (-1,-1), 2),
                        ("BOTTOMPADDING", (0,0), (-1,-1), 0),
                    ],
                )
                story.append(name_tbl)
                if role:
                    role_tbl = Table(
                        [[Paragraph(role, role_st)]],
                        colWidths=[6.0*inch],
                        style=[
                            ("TOPPADDING",    (0,0), (-1,-1), 0),
                            ("BOTTOMPADDING", (0,0), (-1,-1), 2),
                        ],
                    )
                    story.append(role_tbl)

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

    # ── Applicable Rules — 1 line space below title/subtitle ─────────────────
    if rule_body:
        story.append(Spacer(1, 0.25*inch))
        story.append(Paragraph(rule_body, small_center))

    # ── Counsel block — below rules ───────────────────────────────────────────
    if counsel.strip() or opp_counsel.strip():
        TEXT_W  = 6.25 * inch
        HALF_W  = TEXT_W / 2
        BLOCK_W = HALF_W - 0.1 * inch

        def _counsel_paras(text):
            paras = []
            first_content = True
            for line in text.strip().split("\n"):
                stripped = line.strip()
                if not stripped:
                    paras.append(Spacer(1, 5))
                    continue
                if first_content:
                    st = ParagraphStyle("cb", parent=normal,
                        fontName="Times-Bold", fontSize=10, leading=15, spaceAfter=1)
                    first_content = False
                else:
                    st = ParagraphStyle("cn", parent=normal,
                        fontSize=10, leading=15, spaceAfter=1)
                paras.append(Paragraph(stripped, st))
            return paras

        story.append(Spacer(1, 0.35*inch))

        if counsel.strip():
            R_SPACER = TEXT_W * 0.62
            R_CONTENT = TEXT_W - R_SPACER
            story.append(Table(
                [[" ", _counsel_paras(counsel)]],
                colWidths=[R_SPACER, R_CONTENT],
                style=[("VALIGN",(0,0),(-1,-1),"TOP"),
                       ("LEFTPADDING",(0,0),(-1,-1),0),("RIGHTPADDING",(0,0),(-1,-1),0),
                       ("TOPPADDING",(0,0),(-1,-1),0),("BOTTOMPADDING",(0,0),(-1,-1),0)],
            ))

        if counsel.strip() and opp_counsel.strip():
            story.append(Spacer(1, 0.25*inch))

        if opp_counsel.strip():
            story.append(Table(
                [[_counsel_paras(opp_counsel), " "]],
                colWidths=[BLOCK_W, HALF_W + 0.1*inch],
                style=[("VALIGN",(0,0),(-1,-1),"TOP"),
                       ("LEFTPADDING",(0,0),(-1,-1),0),("RIGHTPADDING",(0,0),(-1,-1),0),
                       ("TOPPADDING",(0,0),(-1,-1),0),("BOTTOMPADDING",(0,0),(-1,-1),0)],
            ))

    # ── Written Recitals — 2 line spaces below counsel (or rules/title) ───────
    if recitals and recitals.strip():
        recital_st = ParagraphStyle("recital", parent=normal,
            alignment=TA_LEFT, fontSize=11, leading=20, spaceAfter=10)
        story.append(Spacer(1, 0.5*inch))   # 2 line spaces below counsel
        for para in recitals.strip().split("\n"):
            if para.strip():
                story.append(Paragraph(para.strip(), recital_st))
                story.append(Spacer(1, 0.1*inch))
        if not page_break_after_recital:
            story.append(Spacer(1, 0.35*inch))  # 2 line spaces before the page break

    # TOC always starts at the top of its own page so hyperlink Y positions are correct.
    # Toggle ON:  PageBreak right after recitals → TOC at top of new page.
    # Toggle OFF: 2-line spacer above, then PageBreak → TOC at top of new page.
    story.append(PageBreak())

    # ── Table of Contents ───────────────────────────────────────────────────
    # Invisible marker records the 1-based page number the TOC starts on —
    # needed by add_toc_links because a long TOC spans multiple pages, so
    # "last page of the cover doc" is not where the TOC begins.
    from reportlab.platypus import Flowable
    class _TocPageMarker(Flowable):
        def __init__(self, sink):
            Flowable.__init__(self)
            self.sink = sink
            self.width = self.height = 0
        def draw(self):
            self.sink.append(self.canv.getPageNumber())
    toc_start_sink = []
    story.append(_TocPageMarker(toc_start_sink))
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
    h_page = col_page_header or "Page Number"

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

    current_page = 1 + page_offset  # absolute PDF page number of first body page
    item_num = 1      # global item counter — continues across individual items and tab sub-items

    # Grouped tabs — Tab A, Tab B… sub-items continue global numbering
    tab_shade = colors.Color(0.93, 0.91, 0.87)
    shaded_rows = []  # row indices for shading

    if entries:
        # New flat entries model: docs and dividers interleaved
        for entry in entries:
            if entry.get("type") == "divider":
                title_d = entry.get("title", "").strip()
                restart = entry.get("restart_num", False)
                if restart:
                    item_num = 1
                if title_d:
                    div_page = current_page  # page the divider occupies
                    if use_dividers:
                        current_page += 1  # divider page takes 1 page
                    shaded_rows.append(len(toc_data))
                    toc_data.append(make_row(
                        Paragraph(f"<b>{title_d.upper()}</b>", grp_st),
                        Paragraph(f"<b>{entry.get('desc','')}</b>", grp_st) if entry.get('desc') else Paragraph("", grp_st),
                        "",
                        Paragraph(str(div_page), grp_rt),
                    ))
            else:
                name     = entry.get("custom_name") or entry.get("filename", "Document")
                pc       = entry.get("page_count", 1)
                page_str = str(current_page) if pc == 1 else f"{current_page}–{current_page+pc-1}"
                toc_data.append(make_row(
                    Paragraph(str(item_num), row_st),
                    Paragraph(name, row_st),
                    entry.get("doc_date", ""),
                    Paragraph(page_str, row_rt),
                ))
                current_page += pc
                item_num += 1
    else:
        # Legacy: individual items + grouped tabs
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
    if entries:
        for entry in entries:
            if entry.get("type") == "divider":
                if entry.get("title", "").strip():
                    row_heights.append(_ROW_H)
            else:
                row_heights.append(_ROW_H)
    else:
        for _item in items:
            row_heights.append(_ROW_H)
        for _tab in tabs:
            row_heights.append(_ROW_H)
            for _ in _tab.get("items", []):
                row_heights.append(_SUB_H)

    if entries:
        div_title_lens = [len(e.get("title", "")) for e in entries if e.get("type") == "divider" and e.get("title", "").strip()]
        max_label_len = max(div_title_lens) if div_title_lens else 0
    else:
        max_label_len = max((len(t.get("label") or f"{tab_prefix} A") for t in tabs), default=len(tab_prefix) + 2)
    max_label_len = max(max_label_len, 18)  # always fit at least 18 characters
    prefix_w = max(0.9, 0.55 + max_label_len * 0.065) * inch
    TEXT_W = 6.25 * inch  # 8.5 - 1.25 left - 1.0 right
    PAGE_COL = 1.1 * inch  # wide enough for 12 characters ("Page Number")
    if has_col:
        prefix_w = min(prefix_w, TEXT_W - 1.4*inch - PAGE_COL - 0.5*inch)
        col2 = max(0.5*inch, TEXT_W - prefix_w - 1.4*inch - PAGE_COL)
        col_widths = [prefix_w, col2, 1.4*inch, PAGE_COL]
    else:
        prefix_w = min(prefix_w, TEXT_W - PAGE_COL - 0.5*inch)
        col2 = max(0.5*inch, TEXT_W - prefix_w - PAGE_COL)
        col_widths = [prefix_w, col2, PAGE_COL]
    toc_table = Table(toc_data, colWidths=col_widths)
    toc_table.setStyle(TableStyle(ts))
    # Force layout so we can read the actual computed row heights (including wrapped rows)
    toc_table.wrap(TEXT_W, 10000)
    actual_row_heights = list(toc_table._rowHeights)  # index 0 = header, 1..n = data rows
    story.append(toc_table)
    story.append(PageBreak())

    doc.build(story)
    toc_start_page = toc_start_sink[0] if toc_start_sink else None  # 1-based
    return actual_row_heights, toc_start_page


def generate_divider_page(tab_full_label, name, output_path):
    doc = SimpleDocTemplate(output_path, pagesize=letter,
        rightMargin=1*inch, leftMargin=1.25*inch,
        topMargin=2*inch, bottomMargin=1*inch)
    styles = getSampleStyleSheet()
    normal = styles["Normal"]
    story = [
        Spacer(1, 1.5*inch),
        Paragraph(tab_full_label.upper(), ParagraphStyle("big", parent=normal,
            fontName="Times-Bold", fontSize=36, leading=48,
            alignment=TA_CENTER, spaceAfter=0)),
    ]
    if name:
        story.append(Spacer(1, 0.25*inch))
        story.append(Paragraph(name, ParagraphStyle("nm", parent=normal,
            fontName="Times-Roman", fontSize=13, alignment=TA_CENTER, leading=18, spaceAfter=0)))
    story.append(PageBreak())
    doc.build(story)


def add_toc_links(writer, toc_page_index, items, tabs,
                  first_page_index, use_dividers=True, entries=None,
                  entry_page_map=None, actual_row_heights=None):
    """Stamp clickable links on TOC rows for items, tab groups, or flat entries."""
    page        = writer.pages[toc_page_index]
    page_height = float(page.mediabox.height)
    page_width  = float(page.mediabox.width)
    left  = 1.0 * 72
    right = page_width - 1.0 * 72

    # Y of first data row: page_height minus top-margin(90) minus TOC-heading(34) minus header-row
    # TOC always starts at top of its own page (forced PageBreak), so spaceBefore is suppressed
    # by ReportLab. Actual heading height = leading(18) + spaceAfter(16) = 34.
    _TOP_MARGIN = 90
    _TOC_HDG_H  = 34   # leading(18) + spaceAfter(16); spaceBefore suppressed at page top
    # Header row height comes from actual_row_heights[0] if available, else fallback
    _HDR_ROW_H  = float(actual_row_heights[0]) if actual_row_heights else 28
    first_row_top = page_height - _TOP_MARGIN - _TOC_HDG_H - _HDR_ROW_H

    # Data row heights: actual_row_heights[1:] if available, else fixed fallbacks
    ROW_H = 30   # fallback for divider/doc rows
    SUB_H = 25   # fallback for sub-rows
    data_heights = [float(h) for h in actual_row_heights[1:]] if actual_row_heights and len(actual_row_heights) > 1 else []

    # Page geometry for multi-page TOCs: the doc uses 1.25" top/bottom margins.
    _BOTTOM_MARGIN = 90
    # Continuation TOC pages have no heading — rows start at the top margin.
    cont_row_top = page_height - _TOP_MARGIN
    # Row capacity per page (rows that don't fit move whole to the next page,
    # matching ReportLab's table splitting)
    cap_first = first_row_top - _BOTTOM_MARGIN
    cap_cont  = cont_row_top - _BOTTOM_MARGIN
    # TOC occupies pages toc_page_index .. first_page_index-1
    max_toc_pages = max(1, first_page_index - toc_page_index)

    link_rows = []      # [(y_offset_from_first_row_top, row_height, target_pdf_page)]
    y = 0
    row_idx = 0         # index into data_heights
    current_pdf = first_page_index

    def next_h(fallback):
        """Return actual height for the current data row, or fallback."""
        if row_idx < len(data_heights):
            return data_heights[row_idx]
        return fallback

    if entries:
        # Flat entries model: docs and dividers interleaved
        for entry in entries:
            if entry.get("type") == "divider":
                title_d = entry.get("title", "").strip()
                if title_d:
                    h = next_h(ROW_H); row_idx += 1
                    if entry_page_map and entry.get("id") in entry_page_map:
                        target = entry_page_map[entry["id"]]
                    else:
                        target = current_pdf
                    link_rows.append((y, h, target))
                    y += h
                    if use_dividers:
                        current_pdf += 1
            else:
                h = next_h(ROW_H); row_idx += 1
                if entry_page_map and entry.get("id") in entry_page_map:
                    target = entry_page_map[entry["id"]]
                else:
                    target = current_pdf
                link_rows.append((y, h, target))
                y += h
                pc = entry.get("page_count", 1)
                current_pdf += pc
    else:
        # Legacy: individual items + grouped tabs
        for item in items:
            h = next_h(ROW_H); row_idx += 1
            link_rows.append((y, h, current_pdf))
            y += h
            pc = item.get("page_count", 1)
            current_pdf += pc

        for tab in tabs:
            target = current_pdf
            h = next_h(ROW_H); row_idx += 1
            link_rows.append((y, h, target))
            y += h
            doc_pdf = current_pdf + (1 if use_dividers else 0)
            for item in tab.get("items", []):
                h = next_h(SUB_H); row_idx += 1
                link_rows.append((y, h, doc_pdf))
                y += h
                pc      = item.get("page_count", 1)
                doc_pdf += pc
            total = sum(i.get("page_count", 1) for i in tab.get("items", []))
            current_pdf += (1 + total) if use_dividers else total

    from pypdf.generic import (DictionaryObject, NameObject, ArrayObject,
                               NumberObject)

    def _annotate(page_idx, row_top, h, target):
        rect = RectangleObject([left, row_top - h, right, row_top])
        # Build the annotation manually: pypdf's Link(target_page_index=…)
        # writes /Dest [<number> /Fit], which Preview/Acrobat treat as a
        # dead link. A proper /Dest needs the page *object reference*.
        annot = DictionaryObject({
            NameObject("/Type"):    NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Link"),
            NameObject("/Rect"):    rect,
            NameObject("/Border"):  ArrayObject([NumberObject(0), NumberObject(0), NumberObject(0)]),
            NameObject("/Dest"):    ArrayObject([writer.pages[target].indirect_reference,
                                                 NameObject("/Fit")]),
        })
        annot_ref = writer._add_object(annot)
        pg = writer.pages[page_idx]
        if "/Annots" in pg:
            pg["/Annots"].append(annot_ref)
        else:
            pg[NameObject("/Annots")] = ArrayObject([annot_ref])

    # Walk rows across TOC pages the same way ReportLab splits the table:
    # fill a page until the next row wouldn't fit, then continue at the top
    # of the next TOC page.
    toc_pg   = 0                    # 0-based TOC page within the TOC block
    used     = 0.0                  # height consumed on current TOC page
    for (_, h, target) in link_rows:
        cap = cap_first if toc_pg == 0 else cap_cont
        if used + h > cap + 0.5:    # row moves whole to the next page
            toc_pg += 1
            used = 0.0
            if toc_pg >= max_toc_pages:
                break
            cap = cap_cont
        page_top = first_row_top if toc_pg == 0 else cont_row_top
        row_top  = page_top - used
        try:
            if 0 <= target < len(writer.pages):
                _annotate(toc_page_index + toc_pg, row_top, h, target)
        except Exception:
            pass
        used += h


def stamp_page_numbers(writer, position, skip_first=False):
    """Overlay page numbers on every page of the writer.

    skip_first=True: page 1 is omitted from the cover; numbering still starts at 1
    so the first document page shows its correct sequential number.
    """
    from reportlab.pdfgen import canvas as rl_canvas
    from io import BytesIO

    total = len(writer.pages)
    if total == 0:
        return

    width  = float(writer.pages[0].mediabox.width)
    height = float(writer.pages[0].mediabox.height)
    margin = 36  # 0.5 inch

    buf = BytesIO()
    c   = rl_canvas.Canvas(buf, pagesize=(width, height))
    c.setFont("Times-Roman", 10)

    for i in range(total):
        # Always advance the page number counter even when skipping page 1
        page_num = i + 1
        draw = not (skip_first and i == 0)
        if draw:
            text = str(page_num)
            if position == "bottom_center":
                c.drawCentredString(width / 2, margin, text)
            elif position == "bottom_right":
                c.drawRightString(width - margin, margin, text)
            elif position == "bottom_left":
                c.drawString(margin, margin, text)
            elif position == "top_center":
                c.drawCentredString(width / 2, height - margin, text)
            elif position == "top_right":
                c.drawRightString(width - margin, height - margin, text)
            elif position == "top_left":
                c.drawString(margin, height - margin, text)
        c.showPage()

    c.save()
    buf.seek(0)

    overlay = PdfReader(buf)
    for i in range(total):
        writer.pages[i].merge_page(overlay.pages[i])


def merge_pdfs(session_data, output_path):
    doc_type     = session_data["doc_type"]
    items        = session_data.get("items", [])
    tabs         = session_data.get("tabs", [])
    entries      = session_data.get("entries", [])
    use_dividers = session_data.get("use_dividers", True)
    tmpl         = TEMPLATES.get(doc_type, {"header": doc_type.upper(), "tab_style": "alpha"})
    tab_fn       = alpha_label

    writer = PdfWriter()

    # 1. Cover + TOC — two-pass so page numbers reflect actual PDF positions.
    #    Pass 1: generate with offset=0 just to learn how many pages cover+TOC takes.
    #    Pass 2: regenerate with offset=cover_count so TOC page numbers are correct.
    toc_kwargs = dict(
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
        counsel=session_data.get("counsel", ""),
        opp_counsel=session_data.get("opp_counsel", ""),
        page_break_after_recital=session_data.get("page_break_after_recital", False),
    )
    toc_args = (doc_type, items, tabs,
                session_data.get("title", ""),
                session_data.get("court_file", ""),
                session_data.get("parties", ""))

    toc_path = os.path.join(OUTPUT_FOLDER, f"_toc_{uuid.uuid4().hex}.pdf")
    generate_cover_toc(*toc_args, toc_path, **toc_kwargs, page_offset=0, entries=entries)
    cover_count = len(PdfReader(toc_path).pages)
    os.remove(toc_path)

    # Pass 2: correct page numbers — capture actual row heights for link stamping
    toc_path = os.path.join(OUTPUT_FOLDER, f"_toc_{uuid.uuid4().hex}.pdf")
    actual_row_heights, toc_start_page = generate_cover_toc(*toc_args, toc_path, **toc_kwargs, page_offset=cover_count, entries=entries)
    rdr = PdfReader(toc_path)
    cover_count = len(rdr.pages)
    for pg in rdr.pages:
        writer.add_page(pg)
    os.remove(toc_path)

    # TOC may span multiple pages: it STARTS at toc_start_page (1-based),
    # not necessarily on the last page of the cover document.
    toc_page_index = (toc_start_page - 1) if toc_start_page else cover_count - 1
    first_page_idx = cover_count

    entry_page_map = {}  # entry id → 0-indexed page in final writer
    if entries:
        # New flat entries model: docs and dividers interleaved
        item_num = 1
        current_actual = first_page_idx
        for entry in entries:
            if entry.get("type") == "divider":
                title_d = entry.get("title", "").strip()
                desc_d  = entry.get("desc", "").strip()
                restart = entry.get("restart_num", False)
                if restart:
                    item_num = 1
                if title_d and use_dividers:
                    entry_page_map[entry["id"]] = current_actual
                    div_path = os.path.join(OUTPUT_FOLDER, f"_div_{uuid.uuid4().hex}.pdf")
                    generate_divider_page(title_d, desc_d, div_path)
                    div_rdr = PdfReader(div_path)
                    for pg in div_rdr.pages:
                        writer.add_page(pg)
                    current_actual += len(div_rdr.pages)
                    os.remove(div_path)
            else:
                entry_page_map[entry["id"]] = current_actual
                doc_path = entry.get("filepath")
                if doc_path and os.path.exists(doc_path):
                    doc_rdr = PdfReader(doc_path)
                    for pg in doc_rdr.pages:
                        writer.add_page(pg)
                    current_actual += len(doc_rdr.pages)
                else:
                    current_actual += entry.get("page_count", 1)
                item_num += 1
    else:
        # 2. Optional section divider before individual documents
        section_title = session_data.get("section_title", "").strip()
        section_desc  = session_data.get("section_desc",  "").strip()
        if section_title:
            div_path = os.path.join(OUTPUT_FOLDER, f"_div_{uuid.uuid4().hex}.pdf")
            generate_divider_page(section_title, section_desc, div_path)
            for pg in PdfReader(div_path).pages:
                writer.add_page(pg)
            os.remove(div_path)
            first_page_idx += 1  # divider page shifts body start

        # 3. Individual items — no divider pages, just the document pages
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
    add_toc_links(writer, toc_page_index, items, tabs, first_page_idx, use_dividers,
                  entries=entries, entry_page_map=entry_page_map,
                  actual_row_heights=actual_row_heights)

    # 5. Optionally overlay page numbers on every page
    if session_data.get("page_numbers"):
        position   = session_data.get("page_number_position", "bottom_right")
        skip_first = bool(session_data.get("page_number_skip_first", False))
        stamp_page_numbers(writer, position, skip_first=skip_first)

    with open(output_path, "wb") as f:
        writer.write(f)


@app.after_request
def _no_cache_html(resp):
    # Safari aggressively caches HTML (and the inline JS in it), which kept
    # serving stale upload/download code after deployments.
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


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


def _send_receipt_email(recipient_email, subject, html):
    """Send a transactional receipt email via Brevo in a background thread."""
    def _send(app_):
        with app_.app_context():
            if not BREVO_API_KEY:
                app_.logger.warning("BREVO_API_KEY not set — receipt email not sent")
                return
            try:
                resp = http_requests.post(
                    "https://api.brevo.com/v3/smtp/email",
                    headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
                    json={
                        "sender": {"name": MAIL_FROM_NAME, "email": MAIL_FROM_EMAIL},
                        "to": [{"email": recipient_email}],
                        "subject": subject,
                        "htmlContent": html,
                    },
                    timeout=15,
                )
                if resp.status_code >= 300:
                    app_.logger.error(f"Brevo receipt error: {resp.status_code} {resp.text}")
                else:
                    app_.logger.info(f"Receipt sent to {recipient_email}")
            except Exception as e:
                app_.logger.error(f"Failed to send receipt email to {recipient_email}: {e}")
    import threading
    threading.Thread(target=_send, args=(app,), daemon=True).start()


def _topup_receipt_html(user_email, bundles, total_remaining):
    return f"""
    <div style="font-family:'Inter',Arial,sans-serif;max-width:540px;margin:0 auto;background:#f5f2eb;padding:32px 16px">
      <div style="background:#fff;border-radius:12px;padding:40px 36px;box-shadow:0 2px 12px rgba(0,0,0,0.07)">
        <div style="text-align:center;margin-bottom:28px">
          <div style="font-size:2.4rem;margin-bottom:8px">✅</div>
          <h1 style="font-family:'Georgia',serif;font-size:1.6rem;color:#0d1b2a;margin:0">Top-Up Confirmed</h1>
        </div>
        <p style="color:#444;font-size:0.92rem;line-height:1.7;margin-bottom:24px">
          Hi {user_email},<br/><br/>
          Your one-time top-up purchase was successful. Your BundleMaker account has been updated.
        </p>
        <table style="width:100%;border-collapse:collapse;margin-bottom:24px;font-size:0.9rem">
          <tr style="border-bottom:1px solid #f0ece3">
            <td style="padding:10px 0;color:#888">Purchase type</td>
            <td style="padding:10px 0;text-align:right;font-weight:700;color:#c9a84c">One-time · Non-recurring</td>
          </tr>
          <tr style="border-bottom:1px solid #f0ece3">
            <td style="padding:10px 0;color:#888">Bundles added</td>
            <td style="padding:10px 0;text-align:right;font-weight:700;color:#0d1b2a">{bundles}</td>
          </tr>
          <tr>
            <td style="padding:10px 0;color:#888">Bundles now available</td>
            <td style="padding:10px 0;text-align:right;font-weight:700;color:#0d1b2a">{total_remaining}</td>
          </tr>
        </table>
        <p style="color:#888;font-size:0.8rem;line-height:1.6;margin-bottom:28px">
          These bundles carry over and do not reset monthly. You will not be charged again unless you make another purchase.
        </p>
        <div style="text-align:center">
          <a href="https://www.bundlemaker.app/account" style="display:inline-block;background:#0d1b2a;color:#c9a84c;padding:12px 32px;border-radius:7px;font-weight:700;font-size:0.9rem;text-decoration:none;letter-spacing:0.04em">View My Account</a>
        </div>
        <p style="text-align:center;margin-top:24px;font-size:0.75rem;color:#bbb">BundleMaker · support@bundlemaker.app</p>
      </div>
    </div>
    """


def _subscription_receipt_html(user_email, plan_name, bundles, period):
    bundle_line = f"{bundles} bundles per month" if bundles else "Unlimited bundles per month"
    billing_line = "Billed annually" if period == "annual" else "Billed monthly — cancel anytime"
    return f"""
    <div style="font-family:'Inter',Arial,sans-serif;max-width:540px;margin:0 auto;background:#f5f2eb;padding:32px 16px">
      <div style="background:#fff;border-radius:12px;padding:40px 36px;box-shadow:0 2px 12px rgba(0,0,0,0.07)">
        <div style="text-align:center;margin-bottom:28px">
          <div style="font-size:2.4rem;margin-bottom:8px">🎉</div>
          <h1 style="font-family:'Georgia',serif;font-size:1.6rem;color:#0d1b2a;margin:0">Subscription Active</h1>
        </div>
        <p style="color:#444;font-size:0.92rem;line-height:1.7;margin-bottom:24px">
          Hi {user_email},<br/><br/>
          Your BundleMaker subscription is now active. Here's a summary of your plan.
        </p>
        <table style="width:100%;border-collapse:collapse;margin-bottom:24px;font-size:0.9rem">
          <tr style="border-bottom:1px solid #f0ece3">
            <td style="padding:10px 0;color:#888">Plan</td>
            <td style="padding:10px 0;text-align:right;font-weight:700;color:#c9a84c">{plan_name}</td>
          </tr>
          <tr style="border-bottom:1px solid #f0ece3">
            <td style="padding:10px 0;color:#888">Bundles</td>
            <td style="padding:10px 0;text-align:right;font-weight:700;color:#0d1b2a">{bundle_line}</td>
          </tr>
          <tr>
            <td style="padding:10px 0;color:#888">Billing</td>
            <td style="padding:10px 0;text-align:right;font-weight:700;color:#0d1b2a">{billing_line}</td>
          </tr>
        </table>
        <p style="color:#888;font-size:0.8rem;line-height:1.6;margin-bottom:28px">
          Your bundle counter resets every 30 days. You can manage or cancel your subscription at any time from My Account.
        </p>
        <div style="text-align:center">
          <a href="https://www.bundlemaker.app/account" style="display:inline-block;background:#0d1b2a;color:#c9a84c;padding:12px 32px;border-radius:7px;font-weight:700;font-size:0.9rem;text-decoration:none;letter-spacing:0.04em">View My Account</a>
        </div>
        <p style="text-align:center;margin-top:24px;font-size:0.75rem;color:#bbb">BundleMaker · support@bundlemaker.app</p>
      </div>
    </div>
    """


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
    user_at_limit = not current_user.can_generate() and not is_owner()
    return render_template("account.html", plans=PLANS, plan_limits=PLAN_LIMITS,
                           user_at_limit=user_at_limit, topup_price=TOPUP_PRICE, topup_bundles=TOPUP_BUNDLES)


@app.route("/pricing")
def pricing():
    user_plan     = current_user.plan if current_user.is_authenticated else "free"
    user_at_limit = False
    if current_user.is_authenticated and not is_owner():
        user_at_limit = not current_user.can_generate()
    plan_order = ["solo", "professional", "firm"]
    user_plan_rank = plan_order.index(user_plan) if user_plan in plan_order else -1
    return render_template("pricing.html", plans=PLANS,
                           user_plan=user_plan, user_at_limit=user_at_limit,
                           user_plan_rank=user_plan_rank, plan_order=plan_order,
                           topup_bundles=TOPUP_BUNDLES, topup_price=TOPUP_PRICE)


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


@app.route("/topup")
@login_required
def topup():
    if not TOPUP_PRICE_ID or not stripe.api_key:
        flash("Top-up is not available yet. Please contact support.", "error")
        return redirect(url_for("pricing"))
    if not current_user.stripe_customer_id:
        customer = stripe.Customer.create(email=current_user.email)
        current_user.stripe_customer_id = customer.id
        db.session.commit()
    base_url = request.host_url.rstrip("/")
    checkout = stripe.checkout.Session.create(
        customer=current_user.stripe_customer_id,
        payment_method_types=["card"],
        line_items=[{"price": TOPUP_PRICE_ID, "quantity": 1}],
        mode="payment",
        success_url=f"{base_url}/topup/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{base_url}/pricing",
        metadata={"user_id": str(current_user.id), "topup": "1", "bundles": str(TOPUP_BUNDLES)},
    )
    return redirect(checkout.url, code=303)


def _fulfill_topup(user, session_id, added):
    """Apply top-up bundles idempotently. Returns True if applied, False if already done."""
    if user.last_topup_session == session_id:
        return False  # already fulfilled by webhook or a previous page load
    user.topup_bundles     = (user.topup_bundles or 0) + added
    user.last_topup_session = session_id
    user.email_verified    = True
    db.session.commit()
    app.logger.info(f"Top-up fulfilled: {user.email} +{added} bundles (session {session_id})")
    total_remaining = user.bundles_remaining()
    _send_receipt_email(
        user.email,
        f"Your BundleMaker top-up receipt — {added} bundles added",
        _topup_receipt_html(user.email, added, total_remaining),
    )
    return True


def _fulfill_subscription(user, plan, period, sub_id):
    """Apply subscription plan. Idempotent — safe to call multiple times."""
    import datetime
    user.plan                   = plan
    user.plan_period            = period
    user.stripe_subscription_id = sub_id or user.stripe_subscription_id
    user.email_verified         = True
    user.bundles_used           = 0
    user.bundles_reset_date     = datetime.datetime.utcnow() + datetime.timedelta(days=30)
    db.session.commit()
    app.logger.info(f"Subscription fulfilled: {user.email} → {plan} ({period})")
    plan_data = PLANS.get(plan, {})
    plan_name = plan_data.get("name", plan.capitalize())
    bundles   = plan_data.get("bundles")
    _send_receipt_email(
        user.email,
        f"Your BundleMaker {plan_name} subscription is active",
        _subscription_receipt_html(user.email, plan_name, bundles, period),
    )


@app.route("/topup/success")
@login_required
def topup_success():
    session_id = request.args.get("session_id", "")
    added = TOPUP_BUNDLES
    error = None
    if session_id and stripe.api_key:
        try:
            cs = stripe.checkout.Session.retrieve(session_id)
            meta = cs.get("metadata", {})
            if str(current_user.id) == meta.get("user_id") and cs.payment_status == "paid":
                added = int(meta.get("bundles", TOPUP_BUNDLES))
                _fulfill_topup(current_user, session_id, added)
        except Exception as e:
            app.logger.error(f"topup_success fulfillment error: {e}")
            error = True
    return render_template("topup_success.html",
        bundles=added,
        topup_total=current_user.topup_bundles or 0,
        remaining=current_user.bundles_remaining(),
        error=error,
    )


@app.route("/upgrade/success")
@login_required
def upgrade_success():
    session_id = request.args.get("session_id", "")
    plan_key  = current_user.plan
    period    = current_user.plan_period or "monthly"
    if session_id and stripe.api_key:
        try:
            cs = stripe.checkout.Session.retrieve(session_id)
            meta = cs.get("metadata", {})
            if str(current_user.id) == meta.get("user_id"):
                plan_key = meta.get("plan", plan_key)
                period   = meta.get("period", period)
                sub_id   = cs.get("subscription")
                _fulfill_subscription(current_user, plan_key, period, sub_id)
        except Exception as e:
            app.logger.error(f"upgrade_success fulfillment error: {e}")
    plan_data = PLANS.get(plan_key, {})
    plan_name = plan_data.get("name", plan_key.capitalize())
    bundles   = plan_data.get("bundles")
    return render_template("upgrade_success.html",
        plan_name=plan_name,
        bundles=bundles,
        period=period,
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
        meta    = obj.get("metadata", {})
        user_id = meta.get("user_id")
        plan    = meta.get("plan")
        period  = meta.get("period", "monthly")
        is_topup = meta.get("topup") == "1"
        sub_id  = obj.get("subscription")
        app.logger.info(f"checkout.session.completed: user_id={user_id} plan={plan} topup={is_topup}")
        if user_id and is_topup:
            user = db.session.get(User, int(user_id))
            if user:
                added      = int(meta.get("bundles", TOPUP_BUNDLES))
                session_id = obj.get("id", "")
                try:
                    applied = _fulfill_topup(user, session_id, added)
                    if not applied:
                        app.logger.info(f"Top-up webhook: session {session_id} already fulfilled, skipping")
                except Exception as e:
                    db.session.rollback()
                    app.logger.error(f"Top-up DB commit failed for user {user_id}: {e}")
                    return "DB error", 500
            else:
                app.logger.error(f"checkout.session.completed: no user found for top-up id={user_id}")
        elif user_id and plan:
            user = db.session.get(User, int(user_id))
            if user:
                try:
                    _fulfill_subscription(user, plan, period, sub_id)
                    app.logger.info(f"Subscription webhook: updated {user.email} to plan={plan}")
                except Exception as e:
                    db.session.rollback()
                    app.logger.error(f"Subscription DB commit failed for user {user_id}: {e}")
                    return "DB error", 500
            else:
                app.logger.error(f"checkout.session.completed: no user found for id={user_id}")
        else:
            app.logger.error(f"checkout.session.completed: missing metadata in {meta}")

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
        action       = request.form.get("action", "set_plan")
        target = db.session.execute(db.select(User).filter_by(email=target_email)).scalar_one_or_none()
        if not target:
            message = f"No user found with email: {target_email}"
        elif action == "grant_topup":
            added = int(request.form.get("bundles", TOPUP_BUNDLES))
            target.topup_bundles   = (target.topup_bundles or 0) + added
            target.email_verified  = True
            db.session.commit()
            message = f"Granted {added} top-up bundles to {target_email} (total topup: {target.topup_bundles})"
        elif action == "revoke_topup":
            prev = target.topup_bundles or 0
            target.topup_bundles    = 0
            target.last_topup_session = None
            db.session.commit()
            message = f"Revoked top-up bundles from {target_email} ({prev} bundles removed)"
        elif action == "revert_free":
            target.plan                   = "free"
            target.plan_period            = "monthly"
            target.stripe_subscription_id = None
            target.bundles_used           = 0
            target.bundles_reset_date     = None
            target.topup_bundles          = 0
            target.last_topup_session     = None
            db.session.commit()
            message = f"Reverted {target_email} to Free plan (all bundles cleared)"
        else:
            new_plan = request.form.get("plan", "free")
            period   = request.form.get("period", "monthly")
            target.plan        = new_plan
            target.plan_period = period
            target.email_verified = True
            if new_plan != "free":
                target.bundles_used = 0
                target.bundles_reset_date = datetime.datetime.utcnow() + datetime.timedelta(days=30)
            db.session.commit()
            message = f"Updated {target_email} → {new_plan} ({period})"
    html = f"""<!DOCTYPE html><html><head><title>Admin — Set Plan</title>
    <style>body{{font-family:sans-serif;max-width:620px;margin:40px auto;padding:0 20px}}
    input,select{{width:100%;padding:8px;margin:6px 0 14px;box-sizing:border-box;border:1px solid #ccc;border-radius:4px}}
    button{{background:#0d1b2a;color:#c9a84c;padding:10px 24px;border:none;border-radius:4px;cursor:pointer;font-weight:700}}
    .msg{{background:#e8f5e9;padding:10px;border-radius:4px;margin-bottom:16px;color:#2e7d32}}
    .err{{background:#fdecea;padding:10px;border-radius:4px;margin-bottom:16px;color:#c0392b}}
    table{{width:100%;border-collapse:collapse;margin-top:24px;font-size:0.85rem}}
    td,th{{padding:6px 8px;border:1px solid #ddd;text-align:left}}
    h3{{margin:32px 0 8px;border-top:1px solid #eee;padding-top:24px}}</style></head><body>
    <h2>Admin — BundleMaker Users</h2>
    {'<div class="msg">'+message+'</div>' if message else ''}
    <h3>Set / Change Plan</h3>
    <form method="POST">
      <input type="hidden" name="action" value="set_plan"/>
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
    <h3>Grant Top-Up Bundles</h3>
    <form method="POST">
      <input type="hidden" name="action" value="grant_topup"/>
      <label>User email</label>
      <input name="email" type="email" required placeholder="user@example.com"/>
      <label>Bundles to add</label>
      <input name="bundles" type="number" value="20" min="1" max="500"/>
      <button type="submit">Grant Top-Up</button>
    </form>
    <h3 style="color:#c0392b">Refund Actions</h3>
    <p style="font-size:0.82rem;color:#888;margin-bottom:12px">Issue the money refund in Stripe first, then use these to revoke the bundles.</p>
    <form method="POST" onsubmit="return confirm('Revoke ALL top-up bundles for this user?')">
      <input type="hidden" name="action" value="revoke_topup"/>
      <label>Revoke Top-Up — User email</label>
      <input name="email" type="email" required placeholder="user@example.com"/>
      <button type="submit" style="background:#c0392b">Revoke Top-Up Bundles</button>
    </form>
    <form method="POST" style="margin-top:16px" onsubmit="return confirm('Revert this user to Free plan and clear all bundles?')">
      <input type="hidden" name="action" value="revert_free"/>
      <label>Revert to Free — User email</label>
      <input name="email" type="email" required placeholder="user@example.com"/>
      <button type="submit" style="background:#c0392b">Revert to Free Plan</button>
    </form>
    <table><tr><th>Email</th><th>Plan</th><th>Used</th><th>Top-up</th><th>Verified</th></tr>
    {''.join(f"<tr><td>{escape(u.email)}</td><td>{escape(u.plan)}</td><td>{u.bundles_used}</td><td>{u.topup_bundles or 0}</td><td>{'✓' if u.email_verified else '✗'}</td></tr>" for u in users)}
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
    return jsonify({"error": "File too large. Maximum upload size is 500 MB per file."}), 413


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
    for key in ("doc_type","title","court_file","place","region","parties","recitals",
                "country","jurisdiction","custom_court","custom_rules","use_dividers","col_header","tab_prefix",
                "col_item_header","col_doc_header","col_page_header","section_title","section_desc","entries",
                "counsel","opp_counsel","page_break_after_recital",
                "page_numbers","page_number_position","page_number_skip_first"):
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
    try:
        for f in request.files.getlist("files"):
            ext = os.path.splitext(f.filename.lower())[1]
            if ext not in ALLOWED_EXTENSIONS:
                app.logger.info(f"Skipping disallowed extension: {ext}")
                continue
            item = _make_file_item(f, ext)
            sess["items"].append(item)
            added.append(item)
        save_session(sid, sess)
    except Exception as e:
        app.logger.error(f"Upload error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
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
    tab  = {"id": uuid.uuid4().hex, "name": data.get("name", ""), "label": "", "tab_type": "tab", "items": []}
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
            if "tab_type" in data:
                tab["tab_type"] = data["tab_type"]
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


# ── Entries routes (flat docs + dividers) ────────────────────────────────────

# Parallel uploads hit this route concurrently; without a lock the
# read-modify-write of the session file loses entries.
_session_write_lock = threading.Lock()

@app.route("/api/entries/upload", methods=["POST"])
@login_required
def upload_entries():
    sid = _get_sid()
    added, rejected = [], []
    # Save files to disk first (slow part, safe to run concurrently)
    for f in request.files.getlist("files"):
        ext = os.path.splitext(f.filename.lower())[1]
        if ext not in ALLOWED_EXTENSIONS:
            rejected.append({"name": f.filename, "reason": "This file type is not supported. Accepted: PDF, Word, and image files."})
            continue
        try:
            item = _make_file_item(f, ext)
            item["type"] = "doc"
            item["doc_date"] = ""
            added.append(item)
        except UploadRejected as e:
            rejected.append({"name": f.filename, "reason": str(e)})
        except Exception as e:
            import traceback as _tb
            app.logger.error(f"Upload failed for {f.filename}: {_tb.format_exc()}")
            rejected.append({"name": f.filename, "reason": "The file could not be processed. It may be damaged — try re-saving it and uploading again."})
    # Then append to the session under a lock (fast part)
    if added:
        try:
            with _session_write_lock:
                sess = get_session(sid)
                sess["entries"].extend(added)
                save_session(sid, sess)
        except Exception as e:
            import traceback as _tb
            app.logger.error(_tb.format_exc())
            return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
    return jsonify({"added": added, "rejected": rejected})


@app.route("/api/entries/divider", methods=["POST"])
@login_required
def add_divider_entry():
    sid = _get_sid()
    sess = get_session(sid)
    data = request.json or {}
    entry = {"type": "divider", "id": uuid.uuid4().hex,
             "title": data.get("title", ""), "desc": data.get("desc", ""),
             "restart_num": bool(data.get("restart_num", False))}
    sess["entries"].append(entry)
    save_session(sid, sess)
    return jsonify(entry)


@app.route("/api/entries/<entry_id>", methods=["PATCH"])
@login_required
def update_entry(entry_id):
    sid = _get_sid()
    sess = get_session(sid)
    data = request.json or {}
    for e in sess["entries"]:
        if e["id"] == entry_id:
            for key in ("title", "desc", "restart_num", "custom_name", "doc_date"):
                if key in data:
                    e[key] = data[key]
            break
    save_session(sid, sess)
    return jsonify({"ok": True})


@app.route("/api/entries/<entry_id>", methods=["DELETE"])
@login_required
def delete_entry(entry_id):
    sid = _get_sid()
    sess = get_session(sid)
    for e in sess["entries"]:
        if e["id"] == entry_id and e.get("type") == "doc":
            fp = e.get("filepath")
            if fp and os.path.exists(fp):
                try: os.remove(fp)
                except: pass
            break
    sess["entries"] = [e for e in sess["entries"] if e["id"] != entry_id]
    save_session(sid, sess)
    return jsonify({"ok": True})


@app.route("/api/entries/reorder", methods=["POST"])
@login_required
def reorder_entries():
    sid = _get_sid()
    sess = get_session(sid)
    order = request.json.get("order", [])
    id_map = {e["id"]: e for e in sess["entries"]}
    sess["entries"] = [id_map[x] for x in order if x in id_map]
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
        limit     = current_user._effective_limit()
        plan_data = PLANS.get(current_user.plan, {})
        plan_name = plan_data.get("name", current_user.plan.capitalize())
        if current_user.plan == "free":
            msg = f"You've used all {limit} free bundles."
        else:
            msg = f"You've used all {limit} bundles included in your {plan_name} plan for this month."
        return jsonify({"error": msg, "upgrade": True, "plan_name": plan_name, "limit": limit, "plan": current_user.plan}), 403

    sid  = _get_sid()
    sess = get_session(sid)
    entries_docs = [e for e in sess.get("entries", []) if e.get("type") == "doc"]
    total = len(entries_docs) if entries_docs else (len(sess.get("items", [])) + sum(len(t.get("items", [])) for t in sess.get("tabs", [])))
    if total == 0:
        return jsonify({"error": "No documents added yet. Please upload at least one file."}), 400

    # The server's disk is wiped on redeploys/restarts — uploaded files can
    # vanish while their entries survive in the session. Never generate a
    # hollow bundle; tell the user to re-upload instead.
    missing = [e.get("custom_name") or e.get("filename") or "document"
               for e in entries_docs
               if not (e.get("filepath") and os.path.exists(e["filepath"]))]
    if missing:
        return jsonify({
            "error": ("The server was updated and your uploaded files need to be re-uploaded: "
                      + ", ".join(missing[:5])
                      + (f" and {len(missing)-5} more" if len(missing) > 5 else "")
                      + ". Your bundle layout was kept — please re-upload these documents."),
            "missing_files": missing,
        }), 409

    out_name = f"bundle_{uuid.uuid4().hex[:8]}.pdf"
    out_path = os.path.join(OUTPUT_FOLDER, out_name)
    job_id   = uuid.uuid4().hex
    user_id  = current_user.id
    owner    = is_owner()

    _job_set(job_id, status="pending", filename=None, error=None)

    def _run(app_, sess_, out_path_, out_name_, job_id_, user_id_, owner_):
        with app_.app_context():
            try:
                merge_pdfs(sess_, out_path_)
                # Increment bundle counter
                if not owner_:
                    user = db.session.get(User, user_id_)
                    if user:
                        user.bundles_used += 1
                        db.session.commit()
                # Clean up source files
                all_items = list(sess_.get("items", []))
                for t in sess_.get("tabs", []):
                    all_items.extend(t.get("items", []))
                all_items.extend([e for e in sess_.get("entries", []) if e.get("type") == "doc"])
                for item in all_items:
                    fp = item.get("filepath")
                    if fp and os.path.exists(fp):
                        try: os.remove(fp)
                        except OSError: pass
                _job_set(job_id_, status="done", filename=out_name_)
            except Exception as e:
                import traceback as _tb
                app_.logger.error(f"Background generate error (job {job_id_}): {_tb.format_exc()}")
                _job_set(job_id_, status="error", error=f"{type(e).__name__}: {e}")

    import threading
    threading.Thread(
        target=_run,
        args=(app, sess, out_path, out_name, job_id, user_id, owner),
        daemon=True,
    ).start()

    return jsonify({"job_id": job_id})


@app.route("/api/job/<job_id>")
@login_required
def job_status(job_id):
    info = _job_get(job_id)
    if not info:
        return jsonify({"status": "not_found"}), 404
    return jsonify(info)


def _purge_old_outputs(max_age_seconds=7200):
    """Delete bundles and job records older than 2 hours. Called lazily so
    downloads stay repeatable (Safari requests downloads twice)."""
    import time
    now = time.time()
    try:
        for name in os.listdir(OUTPUT_FOLDER):
            if not (name.startswith("bundle_") or name.startswith("job_")):
                continue
            path = os.path.join(OUTPUT_FOLDER, name)
            try:
                if now - os.path.getmtime(path) > max_age_seconds:
                    os.remove(path)
            except OSError:
                pass
    except OSError:
        pass


# No @login_required: Safari's download manager drops session cookies
# (WebKit bug), which redirected downloads to the login page. The job id
# is an unguessable 32-char token that expires with the file (2 h).
@app.route("/api/job/<job_id>/download")
def job_download(job_id):
    if not job_id.isalnum() or len(job_id) != 32:
        return "Not found", 404
    info = _job_get(job_id)
    if not info or info.get("status") != "done":
        return "Not ready", 404
    filename = info.get("filename", "")
    safe_name = os.path.basename(filename)
    path = os.path.join(OUTPUT_FOLDER, safe_name)
    if not os.path.exists(path):
        return "Not found", 404
    # Do NOT delete on download — Safari requests downloads twice and the
    # second request must still succeed. Old files are purged lazily instead.
    _purge_old_outputs()
    return send_file(path, as_attachment=True, download_name=safe_name)


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

import os
import io
import json
import uuid
import shutil
from flask import Flask, render_template, request, jsonify, send_file, session
from pypdf import PdfReader, PdfWriter
from PIL import Image
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.platypus import PageBreak
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT

app = Flask(__name__)
app.secret_key = "ontario-legal-pdf-secret"
UPLOAD_FOLDER = "uploads"
OUTPUT_FOLDER = "output"
SESSIONS_FOLDER = "sessions"

TEMPLATES = {
    "application_record": {
        "label": "Application Record",
        "header": "APPLICATION RECORD",
        "tab_style": "alpha",
    },
    "book_of_authorities": {
        "label": "Book of Authorities",
        "header": "BOOK OF AUTHORITIES",
        "tab_style": "numeric",
    },
    "index_of_materials": {
        "label": "Index of Materials",
        "header": "INDEX OF MATERIALS",
        "tab_style": "alpha",
    },
    "compendium": {
        "label": "Compendium",
        "header": "COMPENDIUM",
        "tab_style": "alpha",
    },
    "motion_record": {
        "label": "Motion Record",
        "header": "MOTION RECORD",
        "tab_style": "alpha",
    },
    "appeal_book": {
        "label": "Appeal Book",
        "header": "APPEAL BOOK",
        "tab_style": "numeric",
    },
    "factum": {
        "label": "Factum / Written Argument",
        "header": "FACTUM",
        "tab_style": "alpha",
    },
    "trial_record": {
        "label": "Trial Record",
        "header": "TRIAL RECORD",
        "tab_style": "alpha",
    },
    "exhibits": {
        "label": "Exhibits",
        "header": "EXHIBITS",
        "tab_style": "alpha",
    },
}

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp", ".heic"}
ALLOWED_EXTENSIONS = {".pdf"} | IMAGE_EXTENSIONS

# Jurisdiction data: country -> list of {value, label, court, rule_body}
JURISDICTIONS = {
    "Canada": [
        {"value": "ON", "label": "Ontario", "court": "SUPERIOR COURT OF JUSTICE", "rule_body": "Rules of Civil Procedure, R.R.O. 1990, Reg. 194"},
        {"value": "ON_CA", "label": "Ontario — Court of Appeal", "court": "COURT OF APPEAL FOR ONTARIO", "rule_body": "Rules of Civil Procedure, R.R.O. 1990, Reg. 194"},
        {"value": "BC", "label": "British Columbia", "court": "SUPREME COURT OF BRITISH COLUMBIA", "rule_body": "Supreme Court Civil Rules, B.C. Reg. 168/2009"},
        {"value": "AB", "label": "Alberta", "court": "COURT OF KING'S BENCH OF ALBERTA", "rule_body": "Alberta Rules of Court, Alta. Reg. 124/2010"},
        {"value": "QC", "label": "Québec", "court": "SUPERIOR COURT", "rule_body": "Code of Civil Procedure, CQLR c. C-25.01"},
        {"value": "MB", "label": "Manitoba", "court": "COURT OF KING'S BENCH OF MANITOBA", "rule_body": "Court of Queen's Bench Rules, Man. Reg. 553/88"},
        {"value": "SK", "label": "Saskatchewan", "court": "COURT OF KING'S BENCH FOR SASKATCHEWAN", "rule_body": "Queen's Bench Rules"},
        {"value": "NS", "label": "Nova Scotia", "court": "SUPREME COURT OF NOVA SCOTIA", "rule_body": "Nova Scotia Civil Procedure Rules"},
        {"value": "NB", "label": "New Brunswick", "court": "COURT OF KING'S BENCH OF NEW BRUNSWICK", "rule_body": "Rules of Court, NB Reg. 82-73"},
        {"value": "CA_FED", "label": "Federal Court of Canada", "court": "FEDERAL COURT", "rule_body": "Federal Courts Rules, SOR/98-106"},
        {"value": "CA_FCA", "label": "Federal Court of Appeal", "court": "FEDERAL COURT OF APPEAL", "rule_body": "Federal Courts Rules, SOR/98-106"},
        {"value": "SCC", "label": "Supreme Court of Canada", "court": "SUPREME COURT OF CANADA", "rule_body": "Rules of the Supreme Court of Canada, SOR/2002-156"},
    ],
    "United Kingdom": [
        {"value": "EW_HC", "label": "England & Wales — High Court", "court": "HIGH COURT OF JUSTICE", "rule_body": "Civil Procedure Rules 1998 (SI 1998/3132)"},
        {"value": "EW_CA", "label": "England & Wales — Court of Appeal", "court": "COURT OF APPEAL", "rule_body": "Civil Procedure Rules 1998 (SI 1998/3132)"},
        {"value": "UKSC", "label": "UK Supreme Court", "court": "THE SUPREME COURT OF THE UNITED KINGDOM", "rule_body": "Supreme Court Rules 2009 (SI 2009/1603)"},
        {"value": "SC_OS", "label": "Scotland — Outer House", "court": "COURT OF SESSION — OUTER HOUSE", "rule_body": "Rules of the Court of Session 1994 (SI 1994/1443)"},
        {"value": "SC_IH", "label": "Scotland — Inner House", "court": "COURT OF SESSION — INNER HOUSE", "rule_body": "Rules of the Court of Session 1994 (SI 1994/1443)"},
        {"value": "NI", "label": "Northern Ireland", "court": "HIGH COURT OF JUSTICE IN NORTHERN IRELAND", "rule_body": "Rules of the Court of Judicature (NI) 1980"},
    ],
    "United States": [
        {"value": "US_FED", "label": "Federal District Court", "court": "UNITED STATES DISTRICT COURT", "rule_body": "Federal Rules of Civil Procedure"},
        {"value": "US_CA", "label": "Federal Court of Appeals", "court": "UNITED STATES COURT OF APPEALS", "rule_body": "Federal Rules of Appellate Procedure"},
        {"value": "USSC", "label": "US Supreme Court", "court": "SUPREME COURT OF THE UNITED STATES", "rule_body": "Rules of the Supreme Court of the United States"},
        {"value": "US_NY", "label": "New York — Supreme Court", "court": "SUPREME COURT OF THE STATE OF NEW YORK", "rule_body": "New York Civil Practice Law and Rules"},
        {"value": "US_CA_ST", "label": "California — Superior Court", "court": "SUPERIOR COURT OF THE STATE OF CALIFORNIA", "rule_body": "California Rules of Court"},
        {"value": "US_TX", "label": "Texas — District Court", "court": "DISTRICT COURT OF TEXAS", "rule_body": "Texas Rules of Civil Procedure"},
        {"value": "US_FL", "label": "Florida — Circuit Court", "court": "CIRCUIT COURT OF FLORIDA", "rule_body": "Florida Rules of Civil Procedure"},
        {"value": "US_IL", "label": "Illinois — Circuit Court", "court": "CIRCUIT COURT OF COOK COUNTY, ILLINOIS", "rule_body": "Illinois Supreme Court Rules"},
    ],
    "Australia": [
        {"value": "AU_FED", "label": "Federal Court of Australia", "court": "FEDERAL COURT OF AUSTRALIA", "rule_body": "Federal Court Rules 2011 (Cth)"},
        {"value": "AU_HCA", "label": "High Court of Australia", "court": "HIGH COURT OF AUSTRALIA", "rule_body": "High Court Rules 2004 (Cth)"},
        {"value": "AU_NSW", "label": "New South Wales — Supreme Court", "court": "SUPREME COURT OF NEW SOUTH WALES", "rule_body": "Uniform Civil Procedure Rules 2005 (NSW)"},
        {"value": "AU_VIC", "label": "Victoria — Supreme Court", "court": "SUPREME COURT OF VICTORIA", "rule_body": "Supreme Court (General Civil Procedure) Rules 2015 (Vic)"},
        {"value": "AU_QLD", "label": "Queensland — Supreme Court", "court": "SUPREME COURT OF QUEENSLAND", "rule_body": "Uniform Civil Procedure Rules 1999 (Qld)"},
        {"value": "AU_WA", "label": "Western Australia — Supreme Court", "court": "SUPREME COURT OF WESTERN AUSTRALIA", "rule_body": "Rules of the Supreme Court 1971 (WA)"},
    ],
    "New Zealand": [
        {"value": "NZ_HC", "label": "High Court", "court": "HIGH COURT OF NEW ZEALAND", "rule_body": "High Court Rules 2016"},
        {"value": "NZ_CA", "label": "Court of Appeal", "court": "COURT OF APPEAL OF NEW ZEALAND", "rule_body": "Court of Appeal (Civil) Rules 2005"},
        {"value": "NZSC", "label": "Supreme Court", "court": "SUPREME COURT OF NEW ZEALAND", "rule_body": "Supreme Court Rules 2004"},
    ],
    "Ireland": [
        {"value": "IE_HC", "label": "High Court", "court": "HIGH COURT", "rule_body": "Rules of the Superior Courts (SI 15/1986)"},
        {"value": "IE_CA", "label": "Court of Appeal", "court": "COURT OF APPEAL", "rule_body": "Rules of the Superior Courts"},
        {"value": "IE_SC", "label": "Supreme Court", "court": "SUPREME COURT", "rule_body": "Rules of the Superior Courts"},
    ],
    "Singapore": [
        {"value": "SG_GD", "label": "General Division — High Court", "court": "GENERAL DIVISION OF THE HIGH COURT", "rule_body": "Rules of Court 2021"},
        {"value": "SG_CA", "label": "Court of Appeal", "court": "COURT OF APPEAL", "rule_body": "Rules of Court 2021"},
    ],
    "Other / Custom": [
        {"value": "CUSTOM", "label": "Custom / Other jurisdiction", "court": "", "rule_body": ""},
    ],
}


def session_path(sid):
    return os.path.join(SESSIONS_FOLDER, f"{sid}.json")

def get_session(sid):
    path = session_path(sid)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {"items": [], "doc_type": "application_record", "title": "", "court_file": "", "parties": "", "country": "Canada", "jurisdiction": "ON", "custom_court": "", "custom_rules": ""}

def save_session(sid, data):
    with open(session_path(sid), "w") as f:
        json.dump(data, f)


def alpha_label(n):
    """Return Tab A, Tab B... Tab Z, Tab AA..."""
    result = ""
    while n >= 0:
        result = chr(65 + (n % 26)) + result
        n = n // 26 - 1
    return result


def numeric_label(n):
    return str(n + 1)


def image_to_pdf(image_path, pdf_path):
    """Convert an image file to a single-page PDF, fitting within letter size."""
    img = Image.open(image_path)
    if img.mode in ("RGBA", "P", "LA"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")

    page_w, page_h = letter  # 612 x 792 pts
    margin = 36  # 0.5 inch
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
    """Return (court_name, rule_body) for a given country + jurisdiction value."""
    for j in JURISDICTIONS.get(country, []):
        if j["value"] == jurisdiction_value:
            return j["court"], j["rule_body"]
    return "", ""


def generate_cover_toc(doc_type, items, title, court_file, parties, output_path,
                       country="Canada", jurisdiction="ON", custom_court="", custom_rules=""):
    """Generate a PDF with cover page + TOC."""
    tmpl = TEMPLATES[doc_type]
    doc = SimpleDocTemplate(
        output_path,
        pagesize=letter,
        rightMargin=1 * inch,
        leftMargin=1.25 * inch,
        topMargin=1 * inch,
        bottomMargin=1 * inch,
    )

    styles = getSampleStyleSheet()
    normal = styles["Normal"]
    normal.fontName = "Times-Roman"
    normal.fontSize = 12

    center_bold = ParagraphStyle(
        "center_bold",
        parent=normal,
        alignment=TA_CENTER,
        fontName="Times-Bold",
        fontSize=14,
        spaceAfter=6,
    )
    center_normal = ParagraphStyle(
        "center_normal",
        parent=normal,
        alignment=TA_CENTER,
        fontSize=12,
        spaceAfter=4,
    )
    small_center = ParagraphStyle(
        "small_center",
        parent=normal,
        alignment=TA_CENTER,
        fontSize=10,
        spaceAfter=4,
    )
    toc_header = ParagraphStyle(
        "toc_header",
        parent=normal,
        alignment=TA_CENTER,
        fontName="Times-Bold",
        fontSize=13,
        spaceAfter=12,
        spaceBefore=12,
    )

    court_name, rule_body = resolve_jurisdiction(country, jurisdiction)
    if jurisdiction == "CUSTOM":
        court_name = custom_court
        rule_body = custom_rules

    story = []

    # ── Cover Page ──────────────────────────────────────────────────────────
    story.append(Spacer(1, 0.5 * inch))
    if country and country != "Other / Custom":
        story.append(Paragraph(country.upper(), center_bold))
        story.append(Spacer(1, 0.1 * inch))
    if court_name:
        story.append(Paragraph(court_name, center_bold))
    story.append(Spacer(1, 0.3 * inch))

    if court_file:
        story.append(Paragraph(f"Court File No.: {court_file}", center_normal))
        story.append(Spacer(1, 0.2 * inch))

    if parties:
        for line in parties.strip().split("\n"):
            story.append(Paragraph(line, center_normal))
        story.append(Spacer(1, 0.3 * inch))

    story.append(Spacer(1, 0.3 * inch))
    story.append(Paragraph(tmpl["header"], center_bold))

    if title:
        story.append(Spacer(1, 0.15 * inch))
        story.append(Paragraph(title, center_normal))

    if rule_body:
        story.append(Spacer(1, 0.3 * inch))
        story.append(Paragraph(rule_body, small_center))

    story.append(PageBreak())

    # ── Table of Contents ───────────────────────────────────────────────────
    story.append(Paragraph("TABLE OF CONTENTS", toc_header))

    tab_fn = alpha_label if tmpl["tab_style"] == "alpha" else numeric_label

    # Build TOC table
    toc_data = [
        [
            Paragraph("<b>Tab</b>", ParagraphStyle("th", parent=normal, fontName="Times-Bold", fontSize=11)),
            Paragraph("<b>Document</b>", ParagraphStyle("th", parent=normal, fontName="Times-Bold", fontSize=11)),
            Paragraph("<b>Page(s)</b>", ParagraphStyle("th", parent=normal, fontName="Times-Bold", fontSize=11, alignment=TA_RIGHT)),
        ]
    ]

    current_page = 1  # page 1 of the body starts after divider
    for i, item in enumerate(items):
        tab_label = tab_fn(i)
        name = item.get("custom_name") or item.get("filename", f"Document {i+1}")
        page_count = item.get("page_count", 1)
        page_str = str(current_page) if page_count == 1 else f"{current_page}–{current_page + page_count - 1}"

        row = [
            Paragraph(f"Tab {tab_label}", ParagraphStyle("td", parent=normal, fontSize=11)),
            Paragraph(name, ParagraphStyle("td", parent=normal, fontSize=11)),
            Paragraph(page_str, ParagraphStyle("td_r", parent=normal, fontSize=11, alignment=TA_RIGHT)),
        ]
        toc_data.append(row)
        current_page += page_count + 1  # +1 for the divider page

    toc_table = Table(toc_data, colWidths=[0.9 * inch, 4.5 * inch, 0.8 * inch])
    toc_table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, 0), "Times-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 11),
                ("LINEBELOW", (0, 0), (-1, 0), 1, colors.black),
                ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.lightgrey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.append(toc_table)
    story.append(PageBreak())

    doc.build(story)


def generate_divider_page(tab_label, doc_name, output_path):
    """Generate a single tab divider page."""
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
        "tab_big",
        parent=normal,
        fontName="Times-Bold",
        fontSize=36,
        alignment=TA_CENTER,
        spaceAfter=20,
    )
    name_style = ParagraphStyle(
        "tab_name",
        parent=normal,
        fontName="Times-Roman",
        fontSize=14,
        alignment=TA_CENTER,
    )

    story = [
        Spacer(1, 1.5 * inch),
        Paragraph(f"TAB {tab_label}", tab_style),
        Paragraph(doc_name, name_style),
        PageBreak(),
    ]
    doc.build(story)


def merge_pdfs(session_data, output_path):
    """Merge cover/TOC + dividers + documents into final PDF."""
    doc_type = session_data["doc_type"]
    items = session_data["items"]
    tmpl = TEMPLATES[doc_type]
    tab_fn = alpha_label if tmpl["tab_style"] == "alpha" else numeric_label

    writer = PdfWriter()

    # 1. Cover + TOC
    toc_path = os.path.join(OUTPUT_FOLDER, f"_toc_{uuid.uuid4().hex}.pdf")
    generate_cover_toc(
        doc_type,
        items,
        session_data.get("title", ""),
        session_data.get("court_file", ""),
        session_data.get("parties", ""),
        toc_path,
        country=session_data.get("country", "Canada"),
        jurisdiction=session_data.get("jurisdiction", "ON"),
        custom_court=session_data.get("custom_court", ""),
        custom_rules=session_data.get("custom_rules", ""),
    )
    reader = PdfReader(toc_path)
    for page in reader.pages:
        writer.add_page(page)
    os.remove(toc_path)

    # 2. For each item: divider + document
    for i, item in enumerate(items):
        tab_label = tab_fn(i)
        name = item.get("custom_name") or item.get("filename", f"Document {i+1}")

        div_path = os.path.join(OUTPUT_FOLDER, f"_div_{uuid.uuid4().hex}.pdf")
        generate_divider_page(tab_label, name, div_path)
        div_reader = PdfReader(div_path)
        for page in div_reader.pages:
            writer.add_page(page)
        os.remove(div_path)

        # Add the actual document
        doc_path = item.get("filepath")
        if doc_path and os.path.exists(doc_path):
            doc_reader = PdfReader(doc_path)
            for page in doc_reader.pages:
                writer.add_page(page)

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
    for key in ("doc_type", "title", "court_file", "parties", "country", "jurisdiction", "custom_court", "custom_rules"):
        if key in data:
            sess[key] = data[key]
    save_session(sid, sess)
    return jsonify({"ok": True})


@app.route("/api/jurisdictions", methods=["GET"])
def get_jurisdictions():
    return jsonify(JURISDICTIONS)


@app.route("/api/upload", methods=["POST"])
def upload():
    sid = session.get("sid")
    sess = get_session(sid)
    files = request.files.getlist("files")
    added = []
    for f in files:
        ext = os.path.splitext(f.filename.lower())[1]
        if ext not in ALLOWED_EXTENSIONS:
            continue
        item_id = uuid.uuid4().hex
        raw_dest = os.path.join(UPLOAD_FOLDER, f"{item_id}{ext}")
        f.save(raw_dest)

        # Convert images to PDF so the merge pipeline is uniform
        if ext in IMAGE_EXTENSIONS:
            pdf_dest = os.path.join(UPLOAD_FOLDER, f"{item_id}.pdf")
            image_to_pdf(raw_dest, pdf_dest)
            os.remove(raw_dest)
            filepath = pdf_dest
        else:
            filepath = raw_dest

        # Clean up original filename for display
        base_name = os.path.splitext(f.filename)[0].replace("_", " ").replace("-", " ")
        page_count = get_pdf_page_count(filepath)
        item = {
            "id": item_id,
            "filename": base_name,
            "custom_name": "",
            "filepath": filepath,
            "page_count": page_count,
            "file_type": "image" if ext in IMAGE_EXTENSIONS else "pdf",
            "original_ext": ext,
        }
        sess["items"].append(item)
        added.append(item)
    save_session(sid, sess)
    return jsonify(added)


@app.route("/api/items", methods=["GET"])
def get_items():
    sid = session.get("sid")
    sess = get_session(sid)
    return jsonify(sess["items"])


@app.route("/api/items/reorder", methods=["POST"])
def reorder_items():
    sid = session.get("sid")
    sess = get_session(sid)
    new_order = request.json.get("order", [])
    id_map = {item["id"]: item for item in sess["items"]}
    sess["items"] = [id_map[i] for i in new_order if i in id_map]
    save_session(sid, sess)
    return jsonify({"ok": True})


@app.route("/api/items/<item_id>", methods=["PATCH"])
def update_item(item_id):
    sid = session.get("sid")
    sess = get_session(sid)
    data = request.json
    for item in sess["items"]:
        if item["id"] == item_id:
            if "custom_name" in data:
                item["custom_name"] = data["custom_name"]
            break
    save_session(sid, sess)
    return jsonify({"ok": True})


@app.route("/api/items/<item_id>", methods=["DELETE"])
def delete_item(item_id):
    sid = session.get("sid")
    sess = get_session(sid)
    sess["items"] = [i for i in sess["items"] if i["id"] != item_id]
    save_session(sid, sess)
    return jsonify({"ok": True})


@app.route("/api/generate", methods=["POST"])
def generate():
    sid = session.get("sid")
    sess = get_session(sid)
    if not sess["items"]:
        return jsonify({"error": "No documents added yet. Please upload at least one PDF."}), 400
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
    save_session(sid, {"items": [], "doc_type": "application_record", "title": "", "court_file": "", "parties": "", "country": "Canada", "jurisdiction": "ON", "custom_court": "", "custom_rules": ""})
    return jsonify({"ok": True})


if __name__ == "__main__":
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(OUTPUT_FOLDER, exist_ok=True)
    os.makedirs(SESSIONS_FOLDER, exist_ok=True)
    port = int(os.environ.get("PORT", 5050))
    app.run(debug=False, port=port)

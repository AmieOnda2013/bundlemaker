"""TOC hyperlink regression tests.

The clickable Table of Contents is a core BundleMaker feature. This suite
generates real bundles and machine-verifies that every TOC row carries a
link annotation resolving to the exact page where its document starts.
Run: python tests/test_hyperlinks.py   (exits non-zero on failure)
"""
import io
import os
import sys
import time

os.environ["DATABASE_URL"] = ""          # sqlite
os.environ.setdefault("SECRET_KEY", "test")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, db                   # noqa: E402
from models import User                   # noqa: E402
from pypdf import PdfReader               # noqa: E402

app.config["TESTING"] = True
FAILURES = []


def check(name, ok, detail=""):
    print(("PASS" if ok else "FAIL"), name, detail, flush=True)
    if not ok:
        FAILURES.append(name)


def make_pdf(pages, text):
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for i in range(pages):
        c.drawString(72, 720, f"{text} :: page {i+1}")
        c.showPage()
    c.save()
    buf.seek(0)
    return buf.read()


with app.app_context():
    db.create_all()
    u = db.session.execute(db.select(User).filter_by(email="ci@test.local")).scalar_one_or_none()
    if not u:
        u = User(email="ci@test.local")
        u.set_password("test12345")
        db.session.add(u)
    u.email_verified = True
    u.plan = "firm"
    db.session.commit()

client = app.test_client()
client.post("/login", data={"email": "ci@test.local", "password": "test12345"})


def generate_bundle(docs, dividers_at=(), session_extra=None):
    """docs: list of (name, page_count). Returns PdfReader of the bundle."""
    client.post("/api/reset")
    for idx, (name, pages) in enumerate(docs):
        if idx in dividers_at:
            client.post("/api/entries/divider",
                        json={"title": f"PART {idx}", "desc": "", "restart_num": False})
        client.post("/api/entries/upload", data={
            "files": (io.BytesIO(make_pdf(pages, name)), f"{name}.pdf"),
        }, content_type="multipart/form-data")
    client.post("/api/session", json={"title": "CI TEST RECORD",
                                      "doc_type": "application_record",
                                      **(session_extra or {})})
    r = client.post("/api/generate")
    job_id = r.get_json()["job_id"]
    info = {}
    for _ in range(240):
        info = client.get(f"/api/job/{job_id}").get_json() or {}
        if info.get("status") in ("done", "error"):
            break
        time.sleep(0.5)
    assert info.get("status") == "done", f"generation failed: {info}"
    pdf = client.get(f"/api/job/{job_id}/download").data
    return PdfReader(io.BytesIO(pdf))


def collect_links(rdr):
    """[(annot_page, resolved_target_page)] for all Link annotations."""
    out = []
    for pi, page in enumerate(rdr.pages):
        for a in (page.get("/Annots") or []):
            obj = a.get_object()
            if obj.get("/Subtype") == "/Link":
                d = obj.get("/Dest")
                assert d is not None, f"link on page {pi} has no /Dest"
                target = rdr.get_page_number(d[0].get_object())
                out.append((pi, target))
    return out


def doc_start_pages(rdr, names):
    starts = {}
    for pi in range(len(rdr.pages)):
        txt = rdr.pages[pi].extract_text() or ""
        for n in names:
            if f"{n} :: page 1" in txt and n not in starts:
                starts[n] = pi
    return starts


# ── Case 1: small bundle — every doc row linked to its start page ────────────
docs = [("alpha", 3), ("bravo", 5), ("charlie", 2)]
rdr = generate_bundle(docs)
links = collect_links(rdr)
starts = doc_start_pages(rdr, [n for n, _ in docs])
check("small: one link per doc", len(links) == len(docs), f"{len(links)} links")
check("small: targets exact",
      [t for _, t in links] == [starts[n] for n, _ in docs],
      f"links={links} starts={starts}")

# ── Case 2: page numbering on — links unchanged ──────────────────────────────
rdr = generate_bundle(docs, session_extra={"page_numbers": True,
                                           "page_number_position": "bottom_right"})
links = collect_links(rdr)
starts = doc_start_pages(rdr, [n for n, _ in docs])
check("pagenum: targets exact",
      [t for _, t in links] == [starts[n] for n, _ in docs], f"links={links}")

# ── Case 3: multi-page TOC (40 docs) — all rows on all TOC pages linked ──────
docs = [(f"doc{i:02d}", 2) for i in range(40)]
rdr = generate_bundle(docs)
links = collect_links(rdr)
starts = doc_start_pages(rdr, [n for n, _ in docs])
toc_pages = sorted({p for p, _ in links})
check("multipage: all 40 rows linked", len(links) == 40, f"{len(links)} links")
check("multipage: TOC spans pages", len(toc_pages) >= 2, f"TOC link pages: {toc_pages}")
check("multipage: targets exact",
      [t for _, t in links] == [starts[n] for n, _ in docs],
      "mismatch" if [t for _, t in links] != [starts[n] for n, _ in docs] else "")

# ── Case 4: dividers — divider row links to divider page ─────────────────────
docs = [("first", 3), ("second", 4)]
rdr = generate_bundle(docs, dividers_at=(1,))
links = collect_links(rdr)
check("dividers: 3 links (2 docs + divider)", len(links) == 3, f"{len(links)} links")
# divider page contains its PART title
div_target = links[1][1]
div_text = rdr.pages[div_target].extract_text() or ""
check("dividers: divider link lands on divider page", "PART" in div_text,
      f"page {div_target}: {div_text[:40]!r}")

print("=" * 60)
if FAILURES:
    print("HYPERLINK REGRESSION:", FAILURES)
    sys.exit(1)
print("ALL HYPERLINK TESTS PASS")

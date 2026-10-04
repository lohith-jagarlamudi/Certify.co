from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timezone

import streamlit as st

# Keep the approved backend as the source of truth. The Streamlit layer only
# replaces the React/FastAPI presentation and request plumbing.
BACKEND_DIR = Path(__file__).resolve().parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app import main as engine

FIELD_LABELS = {
    "student_name": "Student name",
    "company_name": "Company name",
    "internship_role": "Internship role",
    "start_date": "Start date",
    "end_date": "End date",
    "certificate_number": "Certificate number",
    "duration": "Duration",
    "issue_date": "Issue date",
}

st.set_page_config(page_title="Certify.co", page_icon="✓", layout="wide")

st.markdown("""
<style>
:root { --green:#0f8a63; --dark:#12352b; --soft:#f4faf7; --border:#dcebe5; --red:#b42318; --amber:#a15c00; }
.stApp { background: #fbfdfc; }
.block-container { max-width: 1220px; padding-top: 1.5rem; padding-bottom: 3rem; }
.brand { display:flex; align-items:center; gap:12px; margin-bottom:4px; }
.brand-mark { width:42px; height:42px; border-radius:12px; background:#0f8a63; color:white; display:flex; align-items:center; justify-content:center; font-weight:800; font-size:22px; }
.brand h1 { margin:0; color:#12352b; font-size:30px; }
.brand p { margin:2px 0 0; color:#62766f; }
.section-card { border:1px solid #dcebe5; border-radius:16px; padding:20px; background:white; margin-bottom:16px; }
.metric-card { border:1px solid #dcebe5; border-radius:14px; padding:16px; background:white; }
.metric-card .label { color:#64756f; font-size:13px; }
.metric-card .value { color:#12352b; font-size:27px; font-weight:750; margin-top:4px; }
.status { border-radius:999px; padding:5px 11px; font-weight:700; display:inline-block; }
.status.verified { color:#067647; background:#ecfdf3; }
.status.needs { color:#a15c00; background:#fff7e8; }
.status.bad { color:#b42318; background:#fff0ee; }
.status.duplicate { color:#7a4e00; background:#fff5d9; }
.field-row { border:1px solid #e3ece8; border-radius:12px; padding:13px 14px; margin:8px 0; background:#fff; }
.field-title { font-weight:700; color:#173e33; margin-bottom:8px; }
.field-values { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
.field-value { background:#f7faf9; border-radius:9px; padding:9px; }
.field-value span { display:block; font-size:11px; color:#71817c; margin-bottom:3px; }
.match-ok { color:#067647; font-weight:700; }
.match-bad { color:#b42318; font-weight:700; }
.match-missing { color:#a15c00; font-weight:700; }
.small-muted { color:#6b7d77; font-size:13px; }
</style>
""", unsafe_allow_html=True)


def status_badge(status: str) -> str:
    s = str(status or "").lower()
    if s in {"verified", "valid", "almost"}:
        return '<span class="status verified">Verified</span>'
    if s in {"not_verified", "invalid", "verification_failed"}:
        return '<span class="status bad">Not verified</span>'
    if s == "duplicate":
        return '<span class="status duplicate">Duplicate</span>'
    return '<span class="status needs">Needs review</span>'


def sort_items(items):
    priority = lambda item: 0 if item.get("matched") is False else 1 if item.get("matched") is True else 2
    return sorted(items or [], key=priority)


def parse_match_report(record):
    raw = record.get("match_report_json")
    if not raw:
        return []
    try:
        return sort_items(json.loads(raw).get("items", []))
    except Exception:
        return []


def history_rows():
    return engine.get_history()


def ensure_browser():
    marker = Path.home() / ".certify_playwright_ready"
    if marker.exists():
        return True
    try:
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True, timeout=180)
        marker.write_text("ready", encoding="utf-8")
        return True
    except Exception:
        return False


def process_upload(uploaded_file):
    """Run the approved backend workflow directly, without changing its logic."""
    original_name = uploaded_file.name or "unknown"
    contents = uploaded_file.getvalue()
    suffix = Path(original_name).suffix.lower()
    content_type = {
        ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg", ".webp": "image/webp", ".bmp": "image/bmp",
        ".tif": "image/tiff", ".tiff": "image/tiff",
    }.get(suffix, "application/octet-stream")
    if not contents:
        raise ValueError("The uploaded file is empty.")
    if len(contents) > 10 * 1024 * 1024:
        raise ValueError("The uploaded file is larger than the 10 MB limit.")
    if content_type == "application/octet-stream":
        raise ValueError("Unsupported certificate format. Use PDF or a supported image format.")

    certificate_id = str(engine.uuid4())
    stored_name = f"{certificate_id}_{Path(original_name).name}"
    destination = engine.UPLOAD_DIR / stored_name
    destination.write_bytes(contents)
    file_hash = engine.hashlib.sha256(contents).hexdigest()

    text = ""
    extraction_note = ""
    if content_type == "application/pdf":
        try:
            text = engine.extract_pdf_text(destination)
            extraction_note = "PDF text extracted successfully." if text else "No selectable PDF text found. QR scanning was still attempted."
        except Exception as exc:
            extraction_note = f"Text extraction failed: {type(exc).__name__}. QR scanning was still attempted."
    else:
        try:
            text = engine.extract_image_text(destination)
            extraction_note = "Image OCR extracted successfully." if text else "No readable text detected in the image. QR scanning was still attempted."
        except Exception as exc:
            extraction_note = f"Image OCR failed: {type(exc).__name__}. QR scanning was still attempted."

    sources = engine.detect_verification_sources(destination, content_type, text)
    fields = engine.extract_certificate_fields(text, sources) if text or sources else {}
    certificate_number = engine.normalize_certificate_number(fields.get("certificate_number"))
    duplicate = engine.find_duplicate(certificate_number, original_name, file_hash)
    if duplicate:
        mt = duplicate.get("duplicate_match_type")
        message = {
            "certificate_number": "Duplicate certificate detected by certificate number.",
            "filename": "Duplicate upload detected by uploaded filename.",
            "file_hash": "Duplicate upload detected by file hash.",
        }.get(mt, "Duplicate upload detected.")
    else:
        message = "Uploaded; verification pending."

    engine.save_history((certificate_id, original_name, fields.get("student_name"), fields.get("issue_date"), certificate_number,
                         "needs_review", datetime.now(timezone.utc).isoformat(), sources[0]["url"] if sources else None,
                         message, file_hash))

    result = {
        "certificate_id": certificate_id, "filename": original_name, "stored_filename": stored_name,
        "content_type": content_type, "size_bytes": len(contents), "extraction_note": extraction_note,
        "verification_sources": sources, "extracted_fields": fields,
        "duplicate": duplicate is not None, "duplicate_of": duplicate,
        "duplicate_match_type": duplicate.get("duplicate_match_type") if duplicate else None,
    }

    # The approved React workflow verifies automatically after upload.
    if sources:
        if not ensure_browser():
            result["verification"] = {"status": "verification_unavailable", "message": "Browser verification is unavailable because Chromium could not be installed."}
            return result
        primary = [s for s in sources if s.get("source_type") == "qr"] or sources[:1]
        results = []
        for source in primary:
            browser_result = engine.verify_with_browser(source["url"])
            browser_text = browser_result.get("text") or ""
            browser_fields = engine.extract_certificate_fields(browser_text, [{"url": source["url"], "source_type": "link"}]) if browser_text else {}
            match_report = engine.compare_certificate_fields(fields, browser_fields)
            results.append({**source, **browser_result, "browser_text": browser_text, "browser_fields": browser_fields, "match_report": match_report})

        classifications = [engine.classify_match(fields, x.get("browser_fields", {}), x.get("status", "")) for x in results]
        actionable = [(s, r) for s, r in classifications if s != "ignore"]
        if any(s == "not_verified" for s, _ in actionable):
            overall, reason, msg = "not_verified", next(r for s, r in actionable if s == "not_verified"), "The uploaded certificate could not be accepted because the verification data does not match."
        elif any(s == "needs_review" for s, _ in actionable):
            overall, reason, msg = "needs_review", next(r for s, r in actionable if s == "needs_review"), "Faculty review is required because some mapped fields are missing or unavailable on one side."
        elif any(s == "verified" for s, _ in actionable):
            overall, reason, msg = "verified", "All available mapped fields matched exactly.", "All available mapped fields matched exactly. The certificate is verified."
        else:
            overall, reason, msg = "ignore", "No comparable mapped data was found.", "No comparable mapped data was found; the source was ignored."
        first = results[0] if results else {}
        browser_text = "\n\n".join(x.get("browser_text", "") for x in results if x.get("browser_text"))
        browser_fields = next((x.get("browser_fields", {}) for x in results if x.get("browser_fields")), {})
        report = first.get("match_report", {}) if first else {}
        with engine.sqlite3.connect(engine.DB_PATH) as db:
            db.execute("""UPDATE verification_history SET student_name=?, issue_date=?, certificate_number=?, status=?, verified_at=?, verification_url=?, message=?, reason=?, uploaded_fields_json=?, browser_fields_json=?, browser_text=?, match_report_json=? WHERE certificate_id=?""",
                       (browser_fields.get("student_name") or fields.get("student_name"), browser_fields.get("issue_date") or fields.get("issue_date"), certificate_number, overall,
                        datetime.now(timezone.utc).isoformat(), first.get("url"), msg, reason, json.dumps(fields), json.dumps(browser_fields), browser_text, json.dumps(report), certificate_id))
            db.commit()
        result["verification"] = {"status": overall, "message": msg, "browser_text": browser_text, "browser_fields": browser_fields, "uploaded_fields": fields, "match_report": report, "verification_results": results}
    else:
        result["verification"] = {"status": "needs_review", "message": "No QR code or verification link was found in the uploaded certificate.", "match_report": {"items": []}}
    return result


def render_comparison(items):
    for item in sort_items(items):
        matched = item.get("matched")
        if matched is True:
            cls, text = "match-ok", "✓ Matched"
        elif matched is False:
            cls, text = "match-bad", "✕ Does not match"
        else:
            cls, text = "match-missing", "Not enough data to compare"
        label = FIELD_LABELS.get(item.get("field"), item.get("field", "Field"))
        left = item.get("uploaded") or "Not enough data to compare"
        right = item.get("verification_page") or "Not enough data to compare"
        st.markdown(f'''<div class="field-row"><div class="field-title">{label}</div><div class="field-values"><div class="field-value"><span>Certificate</span>{left}</div><div class="field-value"><span>Verification page</span>{right}</div></div><div style="margin-top:8px" class="{cls}">{text}</div></div>''', unsafe_allow_html=True)


def export_csv(rows):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["filename", "student_name", "certificate_id", "certificate_number", "status", "verified_at", "duplicate_match_type"])
    for r in rows:
        writer.writerow([r.get("filename", ""), r.get("student_name", ""), r.get("certificate_id", ""), r.get("certificate_number", ""), r.get("status", ""), r.get("verified_at", ""), r.get("duplicate_match_type", "")])
    return output.getvalue()



# --- Certify.co interface -------------------------------------------------
st.markdown(r"""
<style>
*{box-sizing:border-box}
.stApp{background:radial-gradient(circle at top left,#eef8f2 0,#f7faf8 38%,#edf4f1 100%)}
.block-container{max-width:1120px;padding:2.9rem 1.5rem 5rem}
button,input,select,textarea{font:inherit}
.app-header{display:flex;align-items:center;justify-content:flex-start;margin-bottom:18px}
.brand-row{display:flex;align-items:center;gap:14px}.brand-mark{display:grid;place-items:center;width:48px;height:48px;border-radius:15px;background:linear-gradient(135deg,#2c9b5b,#1f7f4a);color:#fff;font-size:25px;font-weight:900;box-shadow:0 9px 22px rgba(35,131,74,.22)}
.brand-row h1{margin:0;color:#18392b;font-size:clamp(2rem,4vw,3rem);letter-spacing:-.04em}
.quick-nav{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:22px 0 20px;padding:10px 12px;border:1px solid rgba(214,224,239,.95);border-radius:18px;background:rgba(255,255,255,.88);box-shadow:0 10px 28px rgba(37,53,86,.08);backdrop-filter:blur(14px)}
.quick-nav-label{padding:0 8px;color:#65738c;font-size:.76rem;font-weight:850;text-transform:uppercase;letter-spacing:.08em}
.nav-spacer{flex:1}
div.stButton>button{border:1px solid #e0e6f2!important;border-radius:999px!important;background:#f8faff!important;color:#43536f!important;font-size:.82rem!important;font-weight:800!important;padding:.48rem .82rem!important;box-shadow:none!important;min-height:0!important}
div.stButton>button:hover{background:#eaf6ef!important;border-color:#bfe3ca!important;color:#176b3b!important}
div.stButton>button[kind="primary"]{background:linear-gradient(135deg,#2c9b5b,#23834a)!important;color:#fff!important;border-color:#23834a!important;box-shadow:0 6px 14px rgba(35,131,74,.22)!important}
.panel{border:1px solid #d8e8de;border-radius:22px;background:rgba(255,255,255,.94);box-shadow:0 14px 38px rgba(35,90,58,.08);padding:26px}
.upload-heading,.section-heading,.result-header{display:flex;align-items:center;justify-content:space-between;gap:18px}.upload-heading h2,.section-heading h2{margin:4px 0 0;color:#18392b;font-size:1.55rem}.upload-heading p,.section-heading p{margin:6px 0 0}.mini-label{color:#197443;font-size:.72rem;font-weight:850;text-transform:uppercase;letter-spacing:.08em}.muted{color:#6d7b73}.upload-icon{display:grid;place-items:center;width:56px;height:56px;border-radius:16px;background:#e9f7ee;color:#197443;font-size:1.9rem;font-weight:900}
.upload-form{margin-top:22px;padding:16px;border:1px dashed #bcdcc8;border-radius:16px;background:#fbfefb}
.stFileUploader{margin-bottom:0}.upload-help{margin:8px 0 0;color:#718078;font-size:.78rem}
.result{margin-top:24px;padding:22px;border:1px solid #d8e8de;border-radius:20px;background:#fff}.result-header h3{margin:4px 0 0;font-size:1.25rem;color:#18392b}.decision-pill{display:inline-flex;align-items:center;justify-content:center;min-width:112px;padding:9px 14px;border-radius:999px;font-weight:850;font-size:.84rem;border:1px solid transparent}.verified{background:#e5f7eb;color:#176b3b;border-color:#bfe4ca}.needs_review{background:#fff5dc;color:#875b00;border-color:#f0d99b}.not_verified{background:#fde9e9;color:#a52c2c;border-color:#f1bcbc}.verifying{background:#edf7f1;color:#2a7450;border-color:#cfe8da}.duplicate-note{margin:0 0 14px;padding:10px 12px;border-radius:11px;background:#fff5dc;color:#875b00;font-size:.82rem;font-weight:700}
.match-report{border:1px solid #e0e9e3;border-radius:15px;overflow:hidden;background:#fff}.match-row{display:flex;justify-content:space-between;align-items:flex-start;gap:18px;padding:15px 16px;border-top:1px solid #e6eee9}.match-row:first-child{border-top:0}.match-field{min-width:0;flex:1}.match-field>strong{display:block;margin-bottom:9px;color:#244d38;font-size:.92rem}.comparison-values{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.comparison-box{min-width:0;padding:9px 10px;border-radius:10px;background:#f7faf8;border:1px solid #e2ebe5}.comparison-box span{display:block;margin-bottom:3px;color:#718078;font-size:.72rem;font-weight:750;text-transform:uppercase;letter-spacing:.04em}.comparison-box b{display:block;overflow-wrap:anywhere;color:#26372e;font-size:.86rem;font-weight:650}.match-ok,.match-bad,.match-missing{align-self:center;white-space:nowrap;font-weight:800;padding:7px 10px;border-radius:999px;font-size:.76rem}.match-ok{background:#e5f7eb;color:#176b3b}.match-bad{background:#fde9e9;color:#a52c2c}.match-missing{background:#fff5dc;color:#875b00}
.summary-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin-top:20px}.summary-card{border:1px solid #dbe7df;border-radius:16px;padding:18px;background:#fbfefb}.summary-card span{display:block;color:#6d7b73;font-size:.78rem;font-weight:750}.summary-card strong{display:block;margin-top:5px;color:#18392b;font-size:1.9rem}.summary-card.verified{background:#f6fff8}.summary-card.review,.summary-card.review-pending{background:#fffaf0}.summary-card.invalid{background:#fff7f7}.summary-card.duplicate-card{background:#fffaf0}
.history-list{margin-top:20px}.history-controls{display:grid;grid-template-columns:1fr 220px;gap:10px;margin-bottom:14px}.history-record{border:1px solid #dbe7df;border-radius:16px;background:#fff;overflow:hidden;margin-bottom:10px}.history-item{width:100%;display:flex;justify-content:space-between;align-items:center;gap:14px;padding:15px 16px;border:0;border-radius:0;background:#fff;color:#24362c;box-shadow:none;text-align:left}.history-item:hover{background:#f8fcf9;transform:none;box-shadow:none}.history-item strong{display:block}.history-item span,.history-item small{display:block;color:#6d7b73;margin-top:3px}.history-item-badges{display:flex;gap:6px}.status-chip{padding:6px 10px;border-radius:999px;font-size:.75rem;font-weight:800}.history-detail{padding:18px;border-top:1px solid #e4eee7;background:#fbfefb}.review-workspace{margin-top:18px;padding:16px;border:1px solid #dbe8df;border-radius:15px;background:#fff}.review-workspace-title{display:flex;justify-content:space-between;gap:10px;margin-bottom:12px}.review-workspace-title span{font-weight:800;color:#197443}.review-workspace-title small{color:#6d7b73}.detail-actions{display:flex;gap:9px;flex-wrap:wrap;margin-top:12px}.preview-split{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:20px;margin-top:20px}.preview-frame{min-height:520px;border:1px solid #d8e1ef;border-radius:16px;overflow:hidden;background:#eef3f9;display:flex;align-items:center;justify-content:center}.preview-frame img{display:block;width:100%;height:100%;max-height:780px;object-fit:contain;background:#fff}.preview-text-panel{height:clamp(520px,72vh,780px);overflow:auto;border:1px solid #dbe7df;border-radius:18px;padding:20px;background:#fbfefb}.preview-text-panel h3{margin:6px 0 16px}.preview-help{margin:0 0 16px;color:#6d7b73;font-size:.88rem;line-height:1.55}.comparison-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-top:20px}.comparison-card{border:1px solid #dbe7df;border-radius:16px;padding:18px;background:#fbfefb}.message-box{margin-top:14px;padding:11px 13px;border-radius:11px;background:#f5faf7;color:#315744;border:1px solid #dbeae1}
.stDownloadButton>button{border-radius:999px!important}.stTextInput input,.stSelectbox div[data-baseweb="select"]>div,.stTextArea textarea{border-radius:12px!important;border-color:#dbe7df!important}.required-mark{color:#c62828;font-weight:700}
@media(max-width:800px){.summary-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.preview-split,.comparison-grid{grid-template-columns:1fr}.preview-frame,.preview-text-panel{height:auto;min-height:360px}.history-controls{grid-template-columns:1fr}.comparison-values{grid-template-columns:1fr}}
@media(max-width:620px){.block-container{padding-left:.8rem;padding-right:.8rem}.quick-nav-label{width:100%}.summary-grid{grid-template-columns:1fr}.match-row{flex-direction:column}.match-ok,.match-bad,.match-missing{align-self:flex-start}}
</style>
""", unsafe_allow_html=True)

# State used by the React-equivalent single-page workflow.
for key, default in {
    "nav_section": "Upload", "last_result": None, "preview_record": None,
    "compare_record": None, "selected_history_id": None, "review_notes": "",
}.items():
    if key not in st.session_state:
        st.session_state[key] = default

# Header
st.markdown('<header class="app-header"><div class="brand-row"><div class="brand-mark">✓</div><h1>Certify.co</h1></div></header>', unsafe_allow_html=True)

# Pill-style main menu matching the approved interface.
nav_items = [("↥ Upload", "Upload"), ("▣ Results", "Results"), ("◈ Overview", "Overview"), ("▤ History", "History"), ("▣ Preview", "Preview"), ("⇄ Compare", "Compare")]
st.markdown('<div class="quick-nav"><span class="quick-nav-label">Main menu</span></div>', unsafe_allow_html=True)
nav_cols = st.columns([1.05, .9, .9, .9, .9, .9, .9])
with nav_cols[0]:
    st.caption(" ")
for idx, (label, section) in enumerate(nav_items, start=1):
    if section == "Results" and not st.session_state.last_result: continue
    if section == "Preview" and not st.session_state.preview_record: continue
    if section == "Compare" and not st.session_state.compare_record: continue
    with nav_cols[idx]:
        if st.button(label, key=f"nav-{section}", type="primary" if st.session_state.nav_section == section else "secondary", use_container_width=True):
            st.session_state.nav_section = section
            if section == "History":
                st.session_state.selected_history_id = None
            st.rerun()

active = st.session_state.nav_section

# Upload + result workspace. The approved React design keeps the result directly below upload.
if active in {"Upload", "Results"}:
    st.markdown('<section class="panel"><div class="upload-heading"><div><span class="mini-label">Start here</span><h2>Upload a certificate</h2><p class="muted">Choose a PDF or image to begin verification.</p></div><span class="upload-icon">↥</span></div>', unsafe_allow_html=True)
    uploaded = st.file_uploader("Certificate", type=["pdf","png","jpg","jpeg","webp","bmp","tif","tiff"], label_visibility="collapsed", key="certificate_upload")
    st.markdown('<div class="upload-help">Verification starts automatically after you upload the certificate.</div>', unsafe_allow_html=True)
    if uploaded and st.button("Upload certificate", type="primary", key="upload-submit"):
        with st.spinner("Checking certificate details…"):
            try:
                st.session_state.last_result = process_upload(uploaded)
                st.session_state.nav_section = "Results"
                st.rerun()
            except Exception as exc:
                st.error(str(exc))

    result = st.session_state.last_result
    if result:
        verification = result.get("verification", {})
        status = verification.get("status", "needs_review")
        meta = {"verified":("Verified","verified"),"valid":("Verified","verified"),"almost":("Verified","verified"),"needs_review":("Needs review","needs_review"),"verification_unavailable":("Needs review","needs_review"),"not_verified":("Not verified","not_verified"),"invalid":("Not verified","not_verified"),"verification_failed":("Not verified","not_verified")}.get(status,("Verifying…","verifying"))
        st.markdown(f'<div class="result"><div class="result-header"><div><span class="mini-label">Verification result</span><h3>Certificate verification</h3></div><span class="decision-pill {meta[1]}">{meta[0]}</span></div>', unsafe_allow_html=True)
        if result.get("duplicate"):
            st.markdown('<div class="duplicate-note">Duplicate upload detected.</div>', unsafe_allow_html=True)
        report = verification.get("match_report", {})
        items = sort_items(report.get("items", []))
        if items:
            for item in items:
                matched = item.get("matched")
                cls, txt = (("match-ok","✓ Matched") if matched is True else ("match-bad","✕ Does not match") if matched is False else ("match-missing","Not enough data to compare"))
                label = item.get("label") or FIELD_LABELS.get(item.get("field"), item.get("field","Field"))
                left = item.get("uploaded") or "Not enough data to compare"; right = item.get("verification_page") or "Not enough data to compare"
                st.markdown(f'<div class="match-row"><div class="match-field"><strong>{label}</strong><div class="comparison-values"><div class="comparison-box"><span>Certificate</span><b>{left}</b></div><div class="comparison-box"><span>Verification page</span><b>{right}</b></div></div></div><span class="{cls}">{txt}</span></div>', unsafe_allow_html=True)
        else:
            st.markdown('<div class="message-box">No comparable fields were found.</div>', unsafe_allow_html=True)
        st.markdown('</div></section>', unsafe_allow_html=True)
    else:
        st.markdown('</section>', unsafe_allow_html=True)

elif active == "Overview":
    rows = history_rows()
    total = len(rows); verified = sum(str(r.get("status")).lower() in {"verified","valid","almost"} for r in rows)
    review = sum(str(r.get("status")).lower() in {"needs_review","verification_unavailable"} for r in rows)
    invalid = sum(str(r.get("status")).lower() in {"not_verified","invalid","verification_failed"} for r in rows)
    pending = sum(not r.get("reviewed") for r in rows); duplicates = sum("duplicate" in str(r.get("message","")).lower() for r in rows)
    st.markdown('<section class="panel"><div class="section-heading"><div><h2>Overview</h2><p class="muted">Verification activity summary.</p></div></div></section>', unsafe_allow_html=True)
    vals = [("Total",total,"total"),("Verified",verified,"verified"),("Needs review",review,"review"),("Not verified",invalid,"invalid"),("Pending faculty review",pending,"review-pending"),("Duplicate uploads",duplicates,"duplicate-card")]
    cols = st.columns(3)
    for i,(label,value,cls) in enumerate(vals):
        with cols[i%3]: st.markdown(f'<div class="summary-card {cls}"><span>{label}</span><strong>{value}</strong></div>', unsafe_allow_html=True)

elif active == "History":
    rows = history_rows()
    st.markdown('<section class="panel"><div class="section-heading"><div><h2>Verification history</h2><p class="muted">Recent certificates processed by faculty.</p></div></div>', unsafe_allow_html=True)
    c1,c2 = st.columns([3,1])
    with c1: search = st.text_input("Search verification history", placeholder="Search student or certificate ID…", label_visibility="collapsed")
    with c2: filt = st.selectbox("Filter", ["all","verified","needs_review","not_verified","duplicate"], format_func=lambda x: {"all":"All statuses","verified":"Verified","needs_review":"Needs review","not_verified":"Not verified","duplicate":"Duplicates"}[x], label_visibility="collapsed")
    filtered=[]
    for item in rows:
        hay=f"{item.get('filename','')} {item.get('student_name','')} {item.get('certificate_id','')}".lower(); status=str(item.get('status','')).lower(); dup="duplicate" in str(item.get('message','')).lower()
        if search.lower() in hay and (filt=="all" or (filt=="duplicate" and dup) or (filt!="duplicate" and status==filt)): filtered.append(item)
    st.download_button("Export CSV", export_csv(filtered), "certify.co-history.csv", "text/csv", disabled=not filtered)
    if not filtered: st.markdown('<p class="muted">No matching verification records.</p>', unsafe_allow_html=True)
    for item in filtered:
        cid=item.get("certificate_id"); expanded=st.session_state.selected_history_id==cid
        label=f"{item.get('filename') or 'unknown'} — {item.get('student_name') or 'Student not detected'}"
        if st.button(label, key=f"history-{cid}", use_container_width=True, type="secondary"):
            st.session_state.selected_history_id=None if expanded else cid; st.session_state.review_notes=item.get("review_notes") or ""; st.rerun()
        if expanded:
            st.markdown('<div class="history-detail">', unsafe_allow_html=True)
            st.markdown(f'<span class="mini-label">Certificate record</span><h3>Verification report</h3><p><strong>Student name:</strong> {item.get("student_name") or "Not detected"}</p><p><strong>Issue date:</strong> {item.get("issue_date") or "Not detected"}</p><p><strong>Status:</strong> {str(item.get("status") or "").replace("_"," ")}</p><p><strong>Verified at:</strong> {item.get("verified_at") or "Not detected"}</p>', unsafe_allow_html=True)
            if st.button("▣ Preview certificate", key=f"preview-{cid}"):
                st.session_state.preview_record=item; st.session_state.nav_section="Preview"; st.rerun()
            report=parse_match_report(item)
            if report:
                for x in report:
                    matched=x.get("matched"); cls,txt=(("match-ok","✓ Matched") if matched is True else ("match-bad","✕ Does not match") if matched is False else ("match-missing","Not enough data to compare"))
                    st.markdown(f'<div class="match-row"><div class="match-field"><strong>{FIELD_LABELS.get(x.get("field"),x.get("field","Field"))}</strong><div class="comparison-values"><div class="comparison-box"><span>Certificate</span><b>{x.get("uploaded") or "Not enough data to compare"}</b></div><div class="comparison-box"><span>Verification page</span><b>{x.get("verification_page") or "Not enough data to compare"}</b></div></div></div><span class="{cls}">{txt}</span></div>',unsafe_allow_html=True)
            st.markdown('<div class="review-workspace"><div class="review-workspace-title"><span>Faculty review</span><small>Add a note before completing your review.</small></div>', unsafe_allow_html=True)
            notes=st.text_area("Review remarks", value=st.session_state.review_notes, placeholder="Enter the reason for your manual verification…", key=f"review-notes-{cid}")
            a,b=st.columns(2)
            with a:
                if st.button("✓ Mark as reviewed", key=f"review-{cid}"):
                    if not notes.strip(): st.error("Review remarks are mandatory before marking a certificate as reviewed.")
                    else:
                        with engine.sqlite3.connect(engine.DB_PATH) as db:
                            db.execute("UPDATE verification_history SET review_notes=?, reviewed=1, status='verified', reason=?, reviewed_at=?, pre_review_status=? WHERE certificate_id=?",(notes.strip(),f"Faculty manually reviewed and approved: {notes.strip()}",datetime.now(timezone.utc).isoformat(),item.get("status"),cid)); db.commit()
                        st.success("Review notes saved."); st.rerun()
            with b:
                if "duplicate" in str(item.get("message","")).lower() and st.button("⇄ Compare duplicate", key=f"dup-{cid}"):
                    st.session_state.compare_record=item; st.session_state.nav_section="Compare"; st.rerun()
            st.markdown('</div></div>', unsafe_allow_html=True)
    st.markdown('</section>', unsafe_allow_html=True)

elif active == "Preview":
    item=st.session_state.preview_record
    if not item: st.info("Choose Preview certificate from a history record.")
    else:
        st.markdown('<section class="panel"><div class="section-heading"><div><span class="mini-label">Document viewer</span><h2>Certificate preview</h2><p class="muted">Compare the certificate text with the verification page.</p></div></div>',unsafe_allow_html=True)
        left,right=st.columns(2)
        with left:
            path=engine.locate_file(item["certificate_id"])
            if path.suffix.lower()==".pdf": st.download_button("Download certificate PDF",path.read_bytes(),path.name,"application/pdf")
            else: st.image(str(path),use_container_width=True)
        with right:
            st.markdown('<div class="preview-text-panel"><span class="mini-label">Field comparison</span><h3>Certificate vs verification page</h3><p class="preview-help">Only the fields used for verification are shown below, so the source of a mismatch is immediately visible.</p>',unsafe_allow_html=True)
            report=parse_match_report(item)
            if report:
                for x in report:
                    matched=x.get("matched"); cls,txt=(("match-ok","✓ Matched") if matched is True else ("match-bad","✕ Does not match") if matched is False else ("match-missing","Not enough data to compare"))
                    st.markdown(f'<div class="match-row"><div class="match-field"><strong>{FIELD_LABELS.get(x.get("field"),x.get("field","Field"))}</strong><div class="comparison-values"><div class="comparison-box"><span>Certificate</span><b>{x.get("uploaded") or "Not enough data to compare"}</b></div><div class="comparison-box"><span>Verification page</span><b>{x.get("verification_page") or "Not enough data to compare"}</b></div></div></div><span class="{cls}">{txt}</span></div>',unsafe_allow_html=True)
            else: st.markdown('<div class="message-box">No stored field comparison is available for this record.</div>',unsafe_allow_html=True)
            st.markdown('</div>',unsafe_allow_html=True)
        if st.button("Back to history", key="back-history"): st.session_state.nav_section="History"; st.rerun()
        st.markdown('</section>',unsafe_allow_html=True)

elif active == "Compare":
    current=st.session_state.compare_record
    if not current: st.info("Choose Compare duplicate from a duplicate record in History.")
    else:
        st.markdown('<section class="panel"><h2>Certificate comparison</h2>',unsafe_allow_html=True)
        current_number=engine._row_certificate_number(current); current_filename=engine.normalize_filename(current.get("filename")); current_hash=current.get("file_hash")
        matches=[]
        for r in history_rows():
            if r.get("certificate_id")==current.get("certificate_id"): continue
            mt=None
            if current_number and engine._row_certificate_number(r)==current_number: mt="certificate_number"
            elif current_filename and engine.normalize_filename(r.get("filename"))==current_filename: mt="filename"
            elif current_hash and r.get("file_hash")==current_hash: mt="file_hash"
            if mt: matches.append((r,mt))
        left,right=st.columns(2)
        with left:
            st.markdown('<div class="comparison-card"><h3>Selected upload</h3>',unsafe_allow_html=True); st.write(f"**Filename:** {current.get('filename') or 'Not detected'}"); st.write(f"**Student:** {current.get('student_name') or 'Not detected'}"); st.write(f"**Issue date:** {current.get('issue_date') or 'Not detected'}"); st.write(f"**Status:** {current.get('status')}"); st.markdown('</div>',unsafe_allow_html=True)
        with right:
            st.markdown('<div class="comparison-card"><h3>Previous matching upload</h3>',unsafe_allow_html=True)
            if matches:
                for r,mt in matches: st.write(f"**Filename:** {r.get('filename') or 'Not detected'}"); st.write(f"**Student:** {r.get('student_name') or 'Not detected'}"); st.write(f"**Issue date:** {r.get('issue_date') or 'Not detected'}"); st.write(f"**Match:** {mt}"); st.write(f"**Status:** {r.get('status')}")
            else: st.write("No previous matching record found.")
            st.markdown('</div>',unsafe_allow_html=True)
        if st.button("Close comparison", key="close-comparison"): st.session_state.compare_record=None; st.session_state.nav_section="History"; st.rerun()
        st.markdown('</section>',unsafe_allow_html=True)
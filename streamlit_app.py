from __future__ import annotations
import csv, io, json, hashlib, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import streamlit as st

BACKEND_DIR=Path(__file__).resolve().parent/"backend"
if str(BACKEND_DIR) not in sys.path: sys.path.insert(0,str(BACKEND_DIR))
from app import main as engine

st.set_page_config(page_title="Certify.co",page_icon="✓",layout="wide")
st.markdown("""
<style>
.stApp{background:radial-gradient(circle at top left,#eef8f2 0,#f7faf8 38%,#edf4f1 100%)}
.block-container{max-width:1120px;padding-top:2.2rem;padding-bottom:4rem}
.brand{display:flex;align-items:center;gap:12px;margin-bottom:8px}.brand-mark{display:grid;place-items:center;width:42px;height:42px;border-radius:13px;background:linear-gradient(135deg,#2c9b5b,#23834a);color:white;font-size:22px;font-weight:900}.brand h1{font-size:2.8rem;letter-spacing:-.055em;margin:0}.intro{color:#657087;font-size:1rem;margin:0 0 18px}
div[data-testid="stRadio"]>div{gap:8px}div[data-testid="stRadio"] label{border:1px solid #e0e6f2;border-radius:999px;padding:7px 13px;background:#f8faff;font-weight:800;color:#43536f}div[data-testid="stRadio"] label:has(input:checked){background:linear-gradient(135deg,#2c9b5b,#23834a);color:#fff}
.panel{padding:28px;border:1px solid #dce5df;border-radius:24px;background:rgba(255,255,255,.94);box-shadow:0 18px 55px rgba(37,53,86,.08);margin-bottom:24px}.muted{color:#667085}
.pill{display:inline-block;padding:8px 13px;border-radius:999px;font-weight:850;font-size:.8rem}.verified{background:#e5f7eb;color:#176b3b;border:1px solid #bfe4ca}.review{background:#fff5dc;color:#875b00;border:1px solid #f0d99b}.bad{background:#fde9e9;color:#a52c2c;border:1px solid #f1bcbc}
.compare{border:1px solid #e0e9e3;border-radius:15px;overflow:hidden;background:#fff}.row{padding:15px 16px;border-top:1px solid #e6eee9}.row:first-child{border-top:0}.field{font-weight:800;color:#244d38;margin-bottom:9px}.values{display:grid;grid-template-columns:1fr 1fr;gap:12px}.value{padding:10px;border-radius:10px;background:#f7faf8;border:1px solid #e2ebe5;overflow-wrap:anywhere}.value small{display:block;color:#718078;font-weight:750;text-transform:uppercase;font-size:.68rem;margin-bottom:4px}.value b{font-size:.86rem;color:#26372e}.match{margin-top:10px;display:inline-block;padding:6px 10px;border-radius:999px;font-size:.75rem;font-weight:850}.ok{background:#e5f7eb;color:#176b3b}.no{background:#fde9e9;color:#a52c2c}.miss{background:#fff5dc;color:#875b00}
.metric-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:12px}.metric{padding:17px;border-radius:16px;background:#fff;border:1px solid #e0e9e3}.metric span{display:block;color:#718078;font-size:.75rem;font-weight:750}.metric strong{display:block;font-size:1.7rem;margin-top:5px}.history-card{padding:16px;border:1px solid #dfe9e2;border-radius:16px;background:#fff;margin-bottom:12px}
@media(max-width:800px){.metric-grid{grid-template-columns:repeat(2,1fr)}.values{grid-template-columns:1fr}}
</style>
""",unsafe_allow_html=True)

LABELS={"student_name":"Student name","company_name":"Company name","internship_role":"Internship role","start_date":"Start date","end_date":"End date","certificate_number":"Certificate number","duration":"Duration","issue_date":"Issue date"}

def status_label(s):
    return {"verified":"Verified","not_verified":"Not verified","needs_review":"Needs review","verification_unavailable":"Needs review","verification_failed":"Not verified","almost":"Verified","ignore":"Ignored"}.get(s or "","Unknown")
def status_cls(s):
    return "verified" if s in {"verified","almost"} else "bad" if s in {"not_verified","verification_failed"} else "review"

def render_report(items):
    if not items:
        st.info("No comparable fields were found.")
        return
    items=sorted(items,key=lambda x:0 if x.get("matched") is False else 1 if x.get("matched") is True else 2)
    html=['<div class="compare">']
    for x in items:
        m=x.get("matched"); cls="ok" if m is True else "no" if m is False else "miss"
        label=x.get("label") or LABELS.get(x.get("field"),x.get("field","Field"))
        left=x.get("uploaded") or "Not enough data to compare"; right=x.get("verification_page") or "Not enough data to compare"
        badge="✓ Matched" if m is True else "✕ Does not match" if m is False else "Not enough data to compare"
        html.append(f'<div class="row"><div class="field">{label}</div><div class="values"><div class="value"><small>Certificate</small><b>{left}</b></div><div class="value"><small>Verification page</small><b>{right}</b></div></div><span class="match {cls}">{badge}</span></div>')
    html.append("</div>")
    st.markdown("".join(html),unsafe_allow_html=True)

def ensure_browser():
    marker=Path.home()/".certify_playwright_ready"
    if marker.exists(): return
    try:
        r=subprocess.run([sys.executable,"-m","playwright","install","chromium"],capture_output=True,text=True,timeout=240)
        if r.returncode==0: marker.write_text("ok",encoding="utf-8")
    except Exception: pass

def verify_file(f):
    data=f.getvalue(); name=Path(f.name or "certificate").name
    ctype=engine.validate_upload(data,name,f.type or "application/octet-stream")
    cid=str(uuid4()); path=engine.UPLOAD_DIR/f"{cid}_{name}"; path.write_bytes(data)
    file_hash=hashlib.sha256(data).hexdigest(); duplicate=engine.find_duplicate(file_hash)
    text=engine.extract_pdf_text(path) if ctype=="application/pdf" else engine.extract_image_text(path)
    raw=engine.detect_verification_sources(path,ctype,text)
    sources=[]; seen=set()
    for s in sorted(raw,key=lambda x:0 if x.get("source_type")=="qr" else 1):
        u=str(s.get("url") or "").strip().rstrip("/")
        p=engine.urlparse(u)
        if p.scheme not in {"http","https"} or not p.netloc or not p.path or u in seen: continue
        seen.add(u); sources.append({**s,"url":u})
    uploaded=engine.extract_certificate_fields(text,sources)
    certificate_number=engine.normalize_certificate_number(uploaded.get("certificate_number"))
    duplicate=engine.find_duplicate(certificate_number,name,file_hash)
    duplicate_message = {
        "certificate_number":"Duplicate certificate detected by certificate number.",
        "filename":"Duplicate upload detected by uploaded filename.",
        "file_hash":"Duplicate upload detected by file hash.",
    }.get(duplicate.get("duplicate_match_type") if duplicate else None, "Duplicate upload detected.")
    engine.save_history((cid,name,uploaded.get("student_name"),uploaded.get("issue_date"),certificate_number,"needs_review",datetime.now(timezone.utc).isoformat(),sources[0]["url"] if sources else None,duplicate_message if duplicate else "Uploaded; verification pending.",file_hash))
    if not sources:
        return {"certificate_id":cid,"status":"needs_review","message":"No QR code or verification link was found in the uploaded certificate.","uploaded_fields":uploaded,"match_report":{},"verification_results":[],"duplicate":bool(duplicate),"file_path":str(path)}
    ensure_browser()
    primary=[s for s in sources if s.get("source_type")=="qr"] or sources[:1]
    results=[]
    for s in primary:
        br=engine.verify_with_browser(s["url"]); bt=br.get("text") or ""
        bf=engine.extract_certificate_fields(bt,[{"url":s["url"],"source_type":"link"}]) if bt else {}
        results.append({**s,**br,"browser_text":bt,"browser_fields":bf,"match_report":engine.compare_certificate_fields(uploaded,bf)})
    classes=[engine.classify_match(uploaded,x.get("browser_fields",{}),x.get("status","")) for x in results]
    actionable=[x for x in classes if x[0]!="ignore"]
    if any(x[0]=="not_verified" for x in actionable): status,reason="not_verified",next(x[1] for x in actionable if x[0]=="not_verified"); msg="The uploaded certificate could not be accepted because the verification data does not match."
    elif any(x[0]=="needs_review" for x in actionable): status,reason="needs_review",next(x[1] for x in actionable if x[0]=="needs_review"); msg="Faculty review is required because some mapped fields are missing or unavailable on one side."
    elif any(x[0]=="verified" for x in actionable): status,reason="verified","All available mapped fields matched exactly."; msg="All available mapped fields matched exactly. The certificate is verified."
    else: status,reason="ignore","No comparable mapped data was found."; msg="No comparable mapped data was found; the source was ignored."
    first=results[0]; bf=first.get("browser_fields",{})
    with engine.sqlite3.connect(engine.DB_PATH) as db:
        db.execute("""UPDATE verification_history SET student_name=?,issue_date=?,status=?,verified_at=?,verification_url=?,message=?,reason=?,uploaded_fields_json=?,browser_fields_json=?,browser_text=?,match_report_json=? WHERE certificate_id=?""",(bf.get("student_name") or uploaded.get("student_name"),bf.get("issue_date") or uploaded.get("issue_date"),status,datetime.now(timezone.utc).isoformat(),first.get("url"),msg,reason,json.dumps(uploaded),json.dumps(bf),"\n\n".join(x.get("browser_text","") for x in results if x.get("browser_text")),json.dumps(first.get("match_report",{})),cid)); db.commit()
    return {"certificate_id":cid,"status":status,"message":msg,"uploaded_fields":uploaded,"browser_fields":bf,"match_report":first.get("match_report",{}),"verification_results":results,"duplicate":bool(duplicate),"file_path":str(path)}

def history(): return engine.get_history()
def csv_export(rows):
    fields=["filename","student_name","certificate_id","status","pre_review_status","reviewed","review_notes","reviewed_at","verified_at","reason","verification_url","message"]
    out=io.StringIO(); w=csv.DictWriter(out,fieldnames=fields,extrasaction="ignore"); w.writeheader()
    for r in rows: w.writerow(r)
    return out.getvalue().encode("utf-8-sig")

st.markdown('<div class="brand"><div class="brand-mark">✓</div><h1>Certify.co</h1></div>',unsafe_allow_html=True)
st.markdown('<p class="intro">Internship certificate verification made simple. Upload a certificate and Certify.co checks the certificate details against its verification source.</p>',unsafe_allow_html=True)

if "result" not in st.session_state: st.session_state.result=None
nav=st.radio("Main menu",["↥ Upload","▣ Results","◈ Overview","▤ History","▣ Preview","⇄ Compare"],horizontal=True,label_visibility="collapsed")

if nav=="↥ Upload":
    st.markdown('<div class="panel"><h2>Upload a certificate</h2><p class="muted">Choose a PDF or image to begin verification.</p>',unsafe_allow_html=True)
    f=st.file_uploader("Choose certificate",type=["pdf","png","jpg","jpeg","webp","bmp","tif","tiff"],label_visibility="collapsed")
    if f:
        st.caption(f"{f.name} · {f.size/1024:.1f} KB")
        if st.button("Upload certificate",type="primary",use_container_width=True):
            with st.spinner("Reading certificate and checking the verification source…"):
                try: st.session_state.result=verify_file(f)
                except Exception as e: st.error(str(e))
    st.markdown("</div>",unsafe_allow_html=True)
    if st.session_state.result:
        r=st.session_state.result
        st.markdown('<div class="panel">',unsafe_allow_html=True)
        st.markdown(f'<span class="pill {status_cls(r.get("status"))}">{status_label(r.get("status"))}</span><p class="muted">{r.get("message","")}</p>',unsafe_allow_html=True)
        if r.get("duplicate"): st.warning("This certificate file matches a previous upload in history.")
        render_report(r.get("match_report",{}).get("items",[]))
        st.markdown("</div>",unsafe_allow_html=True)

elif nav=="▣ Results":
    r=st.session_state.result
    if not r: st.info("Upload a certificate first.")
    else:
        st.markdown('<div class="panel"><h2>Certificate verification</h2>',unsafe_allow_html=True)
        st.markdown(f'<span class="pill {status_cls(r.get("status"))}">{status_label(r.get("status"))}</span><p class="muted">{r.get("message","")}</p>',unsafe_allow_html=True)
        for x in r.get("verification_results",[]): st.code(x.get("url",""),language=None)
        render_report(r.get("match_report",{}).get("items",[]))
        st.markdown("</div>",unsafe_allow_html=True)

elif nav=="◈ Overview":
    rows=history(); verified=sum(str(x.get("status","")) in {"verified","valid","almost"} for x in rows); review=sum(str(x.get("status","")) in {"needs_review","verification_unavailable"} for x in rows); bad=sum(str(x.get("status","")) in {"not_verified","invalid","verification_failed"} for x in rows); pending=sum(not x.get("reviewed") for x in rows)
    st.markdown('<div class="panel"><h2>Overview</h2><p class="muted">Verification activity and faculty review status.</p><div class="metric-grid">',unsafe_allow_html=True)
    for a,b in [("Total",len(rows)),("Verified",verified),("Needs review",review),("Not verified",bad),("Pending faculty review",pending)]: st.markdown(f'<div class="metric"><span>{a}</span><strong>{b}</strong></div>',unsafe_allow_html=True)
    st.markdown("</div></div>",unsafe_allow_html=True)

elif nav=="▤ History":
    rows=history()
    st.markdown('<div class="panel"><h2>Verification history</h2><p class="muted">Recent certificates processed by faculty.</p>',unsafe_allow_html=True)
    st.download_button("Export CSV",csv_export(rows),"certify.co-history.csv","text/csv",disabled=not rows)
    q=st.text_input("Search",placeholder="Search student or certificate ID…"); filt=st.selectbox("Filter",["all","verified","needs_review","not_verified","duplicate"])
    for row in rows:
        status=str(row.get("status") or "").lower(); hay=f"{row.get('student_name','')} {row.get('certificate_id','')} {row.get('filename','')}".lower(); dup="duplicate" in str(row.get("message","")).lower() or "duplicate" in str(row.get("reason","")).lower()
        if q.lower() not in hay or (filt!="all" and ((filt=="duplicate" and not dup) or (filt!="duplicate" and status!=filt))): continue
        with st.expander(f"{status_label(status)} · {row.get('filename') or 'Certificate'}"):
            st.write(f"**Student:** {row.get('student_name') or 'Not enough data to compare'}")
            st.write(f"**Issue date:** {row.get('issue_date') or 'Not enough data to compare'}")
            try: report=json.loads(row.get("match_report_json") or "{}")
            except Exception: report={}
            render_report(report.get("items",[]))
            if row.get("reviewed"): st.success(f"Faculty reviewed: {row.get('review_notes') or ''}")
            notes=st.text_area("Review remarks *",value=row.get("review_notes") or "",key=f"notes_{row['certificate_id']}")
            if st.button("Mark as reviewed and approved",key=f"approve_{row['certificate_id']}"):
                if not notes.strip(): st.error("Review remarks are mandatory before marking a certificate as reviewed.")
                else:
                    with engine.sqlite3.connect(engine.DB_PATH) as db:
                        cur=db.execute("SELECT status,pre_review_status FROM verification_history WHERE certificate_id=?",(row["certificate_id"],)).fetchone(); original=(cur[1] if cur else None) or (cur[0] if cur else status)
                        db.execute("UPDATE verification_history SET review_notes=?,reviewed=1,reviewed_at=?,pre_review_status=?,status='verified',reason=? WHERE certificate_id=?",(notes.strip(),datetime.now(timezone.utc).isoformat(),original,f"Faculty manually reviewed and approved: {notes.strip()}",row["certificate_id"])); db.commit()
                    st.success("Certificate marked as reviewed and approved."); st.rerun()
    st.markdown("</div>",unsafe_allow_html=True)

elif nav=="▣ Preview":
    r=st.session_state.result
    if not r: st.info("Upload a certificate first.")
    else:
        st.markdown('<div class="panel"><h2>Certificate preview</h2>',unsafe_allow_html=True)
        p=Path(r.get("file_path",""))
        if p.exists() and p.suffix.lower() in {".png",".jpg",".jpeg",".webp",".bmp",".tif",".tiff"}: st.image(str(p),use_container_width=True)
        elif p.exists(): st.download_button("Open / download certificate",p.read_bytes(),p.name)
        st.markdown("### Extracted certificate details")
        for k,v in (r.get("uploaded_fields") or {}).items():
            if v: st.write(f"**{LABELS.get(k,k)}:** {v}")
        st.markdown("</div>",unsafe_allow_html=True)

else:
    r=st.session_state.result
    if not r: st.info("Upload a certificate first.")
    else:
        st.markdown('<div class="panel"><h2>Certificate comparison</h2><p class="muted">Field-by-field comparison between the uploaded certificate and the verification page.</p>',unsafe_allow_html=True)
        render_report(r.get("match_report",{}).get("items",[]))
        for x in r.get("verification_results",[]):
            with st.expander("Rendered verification-page text"):
                st.text_area("Browser text",x.get("browser_text",""),height=300,label_visibility="collapsed")
        st.markdown("</div>",unsafe_allow_html=True)

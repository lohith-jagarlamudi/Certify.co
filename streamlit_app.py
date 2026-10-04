from __future__ import annotations
import csv, io, json, subprocess, sys
from datetime import datetime
from pathlib import Path
import streamlit as st

BACKEND_DIR=Path(__file__).resolve().parent/"backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0,str(BACKEND_DIR))
from app import main as engine

st.set_page_config(page_title="Certify.co",page_icon="✓",layout="wide")
st.markdown("""<style>
.stApp{background:#f6fbfa}.brand{font-size:2.2rem;font-weight:800;color:#0b6b62}
.sub{color:#55706c;margin-bottom:22px}.card{border:1px solid #dceae7;border-radius:14px;padding:18px;background:white}
</style>""",unsafe_allow_html=True)

def label(s):
    return {"verified":"VERIFIED","not_verified":"NOT VERIFIED","needs_review":"NEEDS REVIEW","ignore":"IGNORED"}.get(s,str(s or "UNKNOWN").replace("_"," ").upper())

def show_status(s,msg):
    if s=="verified": st.success(msg or label(s))
    elif s=="not_verified": st.error(msg or label(s))
    elif s=="needs_review": st.warning(msg or label(s))
    else: st.info(msg or label(s))

def ensure_browser():
    marker=Path.home()/".certify_playwright_ready"
    if marker.exists(): return
    try:
        r=subprocess.run([sys.executable,"-m","playwright","install","chromium"],capture_output=True,text=True,timeout=240)
        if r.returncode==0: marker.write_text("ok",encoding="utf-8")
        else: st.warning("Chromium could not be prepared. QR/OCR checks can still run, but browser verification may require review.")
    except Exception:
        st.warning("Chromium could not be prepared. QR/OCR checks can still run, but browser verification may require review.")

def ctype(name,supplied):
    v=(supplied or "").lower()
    if v and v!="application/octet-stream": return v
    return {".pdf":"application/pdf",".png":"image/png",".jpg":"image/jpeg",".jpeg":"image/jpeg",".webp":"image/webp",".bmp":"image/bmp",".tif":"image/tiff",".tiff":"image/tiff"}.get(Path(name).suffix.lower(),v or "application/octet-stream")

def validate(f):
    if f.size>10*1024*1024: return "File size must be 10 MB or smaller."
    if Path(f.name or "").suffix.lower() not in {".pdf",".png",".jpg",".jpeg",".webp",".bmp",".tif",".tiff"}: return "Unsupported certificate format."
    return None

def verify_upload(f):
    err=validate(f)
    if err: raise ValueError(err)
    import hashlib, uuid, cv2
    name=Path(f.name).name
    cid=str(uuid.uuid4())
    data=f.getvalue()
    path=engine.UPLOAD_DIR/f"{cid}_{name}"
    path.write_bytes(data)
    h=hashlib.sha256(data).hexdigest()
    typ=ctype(name,f.type)
    if typ=="application/pdf":
        from pypdf import PdfReader
        try:
            if not PdfReader(str(path)).pages: raise ValueError("The PDF does not contain a readable page.")
        except Exception as e:
            path.unlink(missing_ok=True); raise ValueError(f"The uploaded PDF could not be read: {e}")
        text=engine.extract_pdf_text(path)
    elif typ.startswith("image/"):
        if cv2.imread(str(path)) is None:
            path.unlink(missing_ok=True); raise ValueError("The uploaded image could not be decoded.")
        text=engine.extract_image_text(path)
    else:
        raise ValueError("Unsupported file type.")
    duplicate=engine.find_duplicate(h)
    sources=engine.detect_verification_sources(path,typ,text)
    clean=[]; seen=set()
    for s in sorted(sources,key=lambda x:0 if x.get("source_type")=="qr" else 1):
        u=str(s.get("url") or "").strip().rstrip("/")
        p=engine.urlparse(u)
        if p.scheme not in {"http","https"} or not p.netloc or not p.path or u in seen: continue
        seen.add(u); clean.append({**s,"url":u})
    uploaded=engine.extract_certificate_fields(text,clean)
    engine.save_history((cid,name,uploaded.get("student_name"),uploaded.get("issue_date"),"needs_review",datetime.now(engine.timezone.utc).isoformat(),clean[0]["url"] if clean else None,"Duplicate certificate detected." if duplicate else "Uploaded; verification pending.",h))
    if not clean:
        return {"certificate_id":cid,"status":"needs_review","message":"No QR code or verification link was found in the uploaded certificate.","uploaded_fields":uploaded,"browser_fields":{},"match_report":{},"verification_results":[],"duplicate":bool(duplicate)}
    ensure_browser()
    primary=[s for s in clean if s.get("source_type")=="qr"] or clean[:1]
    results=[]
    for s in primary:
        br=engine.verify_with_browser(s["url"])
        bt=br.get("text") or ""
        bf=engine.extract_certificate_fields(bt,[{"url":s["url"],"source_type":"link"}]) if bt else {}
        results.append({**s,**br,"browser_text":bt,"browser_fields":bf,"match_report":engine.compare_certificate_fields(uploaded,bf)})
    classes=[engine.classify_match(uploaded,r.get("browser_fields",{}),r.get("status","")) for r in results]
    actionable=[x for x in classes if x[0]!="ignore"]
    if any(x[0]=="not_verified" for x in actionable):
        status,reason="not_verified",next(x[1] for x in actionable if x[0]=="not_verified")
        msg="The uploaded certificate could not be accepted because the verification data does not match."
    elif any(x[0]=="needs_review" for x in actionable):
        status,reason="needs_review",next(x[1] for x in actionable if x[0]=="needs_review")
        msg="Faculty review is required because some mapped fields are missing or unavailable on one side."
    elif any(x[0]=="verified" for x in actionable):
        status,reason="verified","All available mapped fields matched exactly."
        msg="All available mapped fields matched exactly. The certificate is verified."
    else:
        status,reason="ignore","No comparable mapped data was found."
        msg="No comparable mapped data was found; the source was ignored."
    first=results[0]
    bf=first.get("browser_fields",{})
    with engine.sqlite3.connect(engine.DB_PATH) as db:
        db.execute("""UPDATE verification_history SET student_name=?,issue_date=?,status=?,verified_at=?,verification_url=?,message=?,reason=?,uploaded_fields_json=?,browser_fields_json=?,browser_text=?,match_report_json=? WHERE certificate_id=?""",(bf.get("student_name") or uploaded.get("student_name"),bf.get("issue_date") or uploaded.get("issue_date"),status,datetime.now(engine.timezone.utc).isoformat(),first.get("url"),msg,reason,json.dumps(uploaded),json.dumps(bf), "\n\n".join(r.get("browser_text","") for r in results if r.get("browser_text")),json.dumps(first.get("match_report",{})),cid))
        db.commit()
    return {"certificate_id":cid,"status":status,"message":msg,"uploaded_fields":uploaded,"browser_fields":bf,"match_report":first.get("match_report",{}),"verification_results":results,"duplicate":bool(duplicate)}

def render_report(report):
    items=(report or {}).get("items",[])
    if not items: st.info("No comparable mapped fields were available."); return
    items=sorted(items,key=lambda x:0 if x.get("matched") is False else 1 if x.get("matched") is True else 2)
    for x in items:
        icon="✅" if x.get("matched") is True else "❌" if x.get("matched") is False else "⚠️"
        a=x.get("uploaded") or "Not enough data to compare"; b=x.get("verification_page") or "Not enough data to compare"
        st.markdown(f"**{icon} {x.get('label',x.get('field'))}**")
        c1,c2=st.columns(2); c1.write(f"**Certificate:** {a}"); c2.write(f"**Verification page:** {b}")
        st.divider()

def csv_data(rows):
    fields=["certificate_id","filename","student_name","issue_date","status","reviewed","review_notes","verified_at","verification_url","reason","message"]
    out=io.StringIO(); w=csv.DictWriter(out,fieldnames=fields,extrasaction="ignore"); w.writeheader()
    for r in rows: w.writerow(r)
    return out.getvalue().encode("utf-8-sig")

st.markdown('<div class="brand">Certify.co</div>',unsafe_allow_html=True)
st.markdown('<div class="sub">Internship certificate verification made simple.</div>',unsafe_allow_html=True)
page=st.sidebar.radio("Navigate",["Verify certificate","History"])
if page=="Verify certificate":
    st.subheader("Upload certificate")
    f=st.file_uploader("Choose a certificate",type=["pdf","png","jpg","jpeg","webp","bmp","tif","tiff"],help="Maximum 10 MB.")
    if f:
        st.caption(f"{f.name} · {f.size/1024:.1f} KB")
        if st.button("Verify automatically",type="primary",use_container_width=True):
            with st.spinner("Reading certificate and checking the verification source…"):
                try: st.session_state["result"]=verify_upload(f)
                except Exception as e: st.error(str(e))
    r=st.session_state.get("result")
    if r:
        st.markdown('<div class="card">',unsafe_allow_html=True)
        show_status(r["status"],r["message"])
        if r.get("duplicate"): st.warning("This certificate file matches a previous upload in history.")
        st.markdown("### Verification comparison"); render_report(r.get("match_report"))
        if r.get("verification_results"):
            with st.expander("Verification source"):
                for x in r["verification_results"]: st.write(x.get("url"))
        st.markdown("</div>",unsafe_allow_html=True)
else:
    st.subheader("Verification history")
    rows=engine.get_history()
    if not rows: st.info("No verification history yet.")
    else:
        st.download_button("Export CSV",csv_data(rows),"certify.co-history.csv","text/csv")
        for row in rows:
            with st.expander(f"{label(row.get('status'))} · {row.get('filename') or 'Certificate'}"):
                st.write(f"**Student:** {row.get('student_name') or 'Not enough data to compare'}")
                st.write(f"**Issue date:** {row.get('issue_date') or 'Not enough data to compare'}")
                try: report=json.loads(row.get("match_report_json") or "{}")
                except Exception: report={}
                render_report(report)
                if row.get("reviewed"): st.success(f"Faculty reviewed: {row.get('review_notes') or ''}")
                notes=st.text_area("Review remarks *",value=row.get("review_notes") or "",key=f"notes_{row['certificate_id']}")
                if st.button("Mark as reviewed and approved",key=f"approve_{row['certificate_id']}"):
                    if not notes.strip():
                        st.error("Review remarks are mandatory before marking a certificate as reviewed.")
                    else:
                        with engine.sqlite3.connect(engine.DB_PATH) as db:
                            db.execute("UPDATE verification_history SET review_notes=?,reviewed=1,status='verified',reason=? WHERE certificate_id=?",(notes.strip(),f"Faculty manually reviewed and approved: {notes.strip()}",row["certificate_id"]))
                            db.commit()
                        st.success("Certificate marked as reviewed and approved."); st.rerun()

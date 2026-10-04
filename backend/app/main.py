from pathlib import Path
import asyncio
import subprocess
import sys
import json
import sqlite3
import hashlib
from datetime import datetime, timezone
import re
from urllib.parse import urlparse
from uuid import uuid4

import numpy as np

import cv2
import fitz
import httpx
if hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pypdf import PdfReader
try:
    import pytesseract
except ImportError:
    pytesseract = None

app = FastAPI(title="InternVerify API", version="0.9.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).resolve().parents[2]
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = BASE_DIR / "internverify.db"

def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS verification_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, certificate_id TEXT UNIQUE NOT NULL,
            filename TEXT, student_name TEXT, issue_date TEXT, status TEXT NOT NULL,
            verified_at TEXT NOT NULL, verification_url TEXT, message TEXT, review_notes TEXT, reviewed INTEGER NOT NULL DEFAULT 0, file_hash TEXT, reason TEXT, uploaded_fields_json TEXT, browser_fields_json TEXT, browser_text TEXT, match_report_json TEXT
        )""")
        try:
            db.execute("ALTER TABLE verification_history ADD COLUMN review_notes TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            db.execute("ALTER TABLE verification_history ADD COLUMN reviewed INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            db.execute("ALTER TABLE verification_history ADD COLUMN file_hash TEXT")
        except sqlite3.OperationalError:
            pass
        for column, definition in (("reason", "TEXT"), ("uploaded_fields_json", "TEXT"), ("browser_fields_json", "TEXT"), ("browser_text", "TEXT"), ("match_report_json", "TEXT")):
            try:
                db.execute(f"ALTER TABLE verification_history ADD COLUMN {column} {definition}")
            except sqlite3.OperationalError:
                pass
        db.commit()

def save_history(record):
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""INSERT OR REPLACE INTO verification_history
        (certificate_id, filename, student_name, issue_date, status, verified_at, verification_url, message, file_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", record)
        db.commit()

def find_duplicate(file_hash):
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM verification_history WHERE file_hash = ? ORDER BY id DESC LIMIT 1", (file_hash,)).fetchone()
        return dict(row) if row else None

def get_history():
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT * FROM verification_history ORDER BY id DESC LIMIT 200").fetchall()]

init_db()


# Streamlit and API uploads share this validation path. Keep the checks
# lightweight enough for Community Cloud while rejecting unsupported or
# obviously corrupt uploads before OCR/QR processing.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ALLOWED_UPLOADS = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}


def validate_upload(data: bytes, filename: str, content_type: str | None = None) -> str:
    """Validate a certificate upload and return its canonical MIME type."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise HTTPException(status_code=400, detail="The uploaded certificate is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Certificate upload exceeds the 10 MB limit.")

    safe_name = Path(filename or "certificate").name
    suffix = Path(safe_name).suffix.lower()
    canonical = ALLOWED_UPLOADS.get(suffix)
    if not canonical:
        raise HTTPException(
            status_code=400,
            detail="Unsupported certificate format. Upload PDF, PNG, JPG, JPEG, WEBP, BMP, TIF or TIFF.",
        )

    supplied = (content_type or "").lower().split(";", 1)[0].strip()
    # Browsers/Streamlit may report application/octet-stream; extension and
    # file signatures are authoritative for those uploads.
    if supplied and supplied not in {canonical, "application/octet-stream"}:
        if not (supplied.startswith("image/") and canonical.startswith("image/")):
            raise HTTPException(status_code=400, detail="The uploaded file type does not match its extension.")

    if canonical == "application/pdf":
        if not data.startswith(b"%PDF-"):
            raise HTTPException(status_code=400, detail="The uploaded PDF is not a valid PDF file.")
        try:
            from io import BytesIO
            reader = PdfReader(BytesIO(bytes(data)))
            if len(reader.pages) == 0:
                raise ValueError("PDF contains no pages")
        except Exception:
            raise HTTPException(status_code=400, detail="The uploaded PDF could not be read.")
    else:
        # OpenCV validates common raster formats without trusting the filename.
        image = cv2.imdecode(np.frombuffer(bytes(data), dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None or image.size == 0:
            raise HTTPException(status_code=400, detail="The uploaded image could not be decoded.")

    return canonical



def extract_pdf_text(path: Path) -> str:
    """Extract selectable text and ALWAYS supplement it with OCR.

    Many certificates contain a small amount of selectable text (for example a
    QR URL or certificate id) while the recipient name and issue date are
    rendered as an image. Returning early when any text exists caused those
    important fields to remain undetected.
    """
    reader = PdfReader(str(path))
    selectable = "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    if pytesseract is None:
        return selectable

    # Make the Windows installation explicit when it exists, while still
    # allowing PATH-based installations on other systems.
    if sys.platform.startswith("win"):
        tess = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
        if tess.exists():
            pytesseract.pytesseract.tesseract_cmd = str(tess)
        else:
            for candidate in (Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"), Path.home() / "AppData/Local/Tesseract-OCR/tesseract.exe"):
                if candidate.exists():
                    pytesseract.pytesseract.tesseract_cmd = str(candidate)
                    break

    document = fitz.open(str(path))
    ocr_chunks = []
    try:
        for page in document:
            pix = page.get_pixmap(matrix=fitz.Matrix(4, 4), alpha=False)
            image = cv2.imdecode(
                np.frombuffer(pix.tobytes("png"), dtype="uint8"),
                cv2.IMREAD_COLOR,
            )
            if image is None:
                continue
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            variants = [image, gray]
            try:
                variants.append(cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1])
            except Exception:
                pass
            page_text = []
            for variant in variants:
                for psm in (3, 6, 11, 12):
                    try:
                        value = pytesseract.image_to_string(variant, config=f"--oem 1 --psm {psm}").strip()
                        if value:
                            page_text.append(value)
                    except Exception:
                        pass
            # Keep OCR text, including repeated variants, because later field
            # extraction can choose the clearest labelled occurrence.
            if page_text:
                ocr_chunks.append("\n".join(dict.fromkeys(page_text)))
    finally:
        document.close()

    return "\n".join(x for x in [selectable, *ocr_chunks] if x).strip()


def extract_image_text(path: Path) -> str:
    """Extract OCR text from uploaded certificate images."""
    if pytesseract is None:
        return ""

    if sys.platform.startswith("win"):
        tess = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
        if tess.exists():
            pytesseract.pytesseract.tesseract_cmd = str(tess)
        else:
            for candidate in (
                Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
                Path.home() / "AppData/Local/Tesseract-OCR/tesseract.exe",
            ):
                if candidate.exists():
                    pytesseract.pytesseract.tesseract_cmd = str(candidate)
                    break

    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        return ""

    # Upscale small phone/scanned images before OCR.
    height, width = image.shape[:2]
    if max(height, width) < 2200:
        scale = 2200 / max(height, width)
        image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    variants = [image, gray]
    variants.append(cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1])
    variants.append(cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 11))

    outputs = []
    for variant in variants:
        for psm in (3, 6, 11, 12):
            try:
                value = pytesseract.image_to_string(variant, config=f"--oem 1 --psm {psm}").strip()
                if value:
                    outputs.append(value)
            except Exception:
                pass

    return "\n".join(dict.fromkeys(outputs)).strip()


def clean_url(value: str) -> str:
    return value.rstrip(".,;)]}>")


def extract_urls(text: str) -> list[str]:
    return list(dict.fromkeys(clean_url(x) for x in re.findall(r"https?://[^\s<>\"']+", text)))


def scan_image(image, detector, page_number: int | None = None) -> list[dict]:
    """Robust QR detection for scanned/printed certificates.

    QR codes can be small, low-contrast, JPEG-compressed, slightly rotated, or
    embedded in a large certificate. The detector may find the QR corners but
    still fail to decode the payload at the certificate's native resolution.
    We therefore try enhanced full-image variants and, when corners are found,
    rectify the detected QR region and decode that region at high resolution.
    """
    found = []
    seen = set()

    def add_value(value: str):
        value = clean_url((value or "").strip())
        if not value or value in seen:
            return
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return
        seen.add(value)
        found.append({"source_type": "qr", "url": value, "page": page_number})

    if image is None or image.size == 0:
        return found

    candidates = []

    # Fast path for the common case where OpenCV can locate the QR corners but
    # cannot decode the payload from the full certificate. Crop the detected
    # quadrilateral tightly and upscale it before decoding. This is especially
    # important for compressed WhatsApp/JPEG certificate images.
    try:
        ok, points = detector.detect(image)
        if ok and points is not None:
            pts = np.asarray(points, dtype=np.float32).reshape(-1, 2)
            if len(pts) >= 4:
                pts = pts[:4]
                x0, y0 = np.floor(pts.min(axis=0)).astype(int)
                x1, y1 = np.ceil(pts.max(axis=0)).astype(int)
                pad = max(2, int(max(x1 - x0, y1 - y0) * 0.02))
                x0 = max(0, x0 - pad)
                y0 = max(0, y0 - pad)
                x1 = min(image.shape[1], x1 + pad + 1)
                y1 = min(image.shape[0], y1 + pad + 1)
                qr_crop = image[y0:y1, x0:x1]
                for scale in (6, 8, 10):
                    enlarged = cv2.resize(
                        qr_crop, None, fx=scale, fy=scale,
                        interpolation=cv2.INTER_LINEAR
                    )
                    qr_gray = cv2.cvtColor(enlarged, cv2.COLOR_BGR2GRAY)
                    qr_variants = [
                        enlarged,
                        qr_gray,
                        cv2.threshold(
                            qr_gray, 0, 255,
                            cv2.THRESH_BINARY + cv2.THRESH_OTSU
                        )[1],
                    ]
                    for qr_variant in qr_variants:
                        try:
                            value, _, _ = detector.detectAndDecode(qr_variant)
                            add_value(value)
                        except Exception:
                            pass
                        if found:
                            return found
    except Exception:
        pass

    # Work on a reasonably large image. This is particularly important when a
    # QR occupies only a small area of a certificate.
    height, width = image.shape[:2]
    scale = 1.0
    if max(height, width) < 2400:
        scale = min(4.0, 2400.0 / max(height, width))
    elif min(height, width) < 900:
        scale = 2.0
    working = cv2.resize(
        image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC
    ) if scale != 1.0 else image

    gray = cv2.cvtColor(working, cv2.COLOR_BGR2GRAY)
    enhanced = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)

    variants = [
        working,
        gray,
        enhanced,
        cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1],
        cv2.adaptiveThreshold(
            enhanced, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY, 31, 7
        ),
    ]

    # Try a modest sharpening pass for photographed/scanned certificates.
    blur = cv2.GaussianBlur(gray, (0, 0), 2)
    variants.append(cv2.addWeighted(gray, 1.7, blur, -0.7, 0))

    for variant in variants:
        candidates.append(variant)
        # QR detectors are sensitive to rotation; include the common 90-degree
        # orientations without making arbitrary perspective assumptions.
        candidates.append(cv2.rotate(variant, cv2.ROTATE_90_CLOCKWISE))
        candidates.append(cv2.rotate(variant, cv2.ROTATE_90_COUNTERCLOCKWISE))
        candidates.append(cv2.rotate(variant, cv2.ROTATE_180))

    # If the certificate is large, scan overlapping tiles as well. This catches
    # QR codes that are too small relative to the full page for the detector.
    h, w = gray.shape[:2]
    if min(h, w) >= 1200:
        tile_h, tile_w = max(900, h // 2), max(900, w // 2)
        step_y = max(450, tile_h // 2)
        step_x = max(450, tile_w // 2)
        for y in range(0, max(1, h - tile_h + 1), step_y):
            for x in range(0, max(1, w - tile_w + 1), step_x):
                tile = gray[y:min(y + tile_h, h), x:min(x + tile_w, w)]
                if tile.size:
                    candidates.append(tile)
                    candidates.append(cv2.threshold(
                        tile, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
                    )[1])

    # Preserve order but avoid running the detector on identical-sized duplicate
    # arrays unnecessarily.
    unique_candidates = []
    signatures = set()
    for candidate in candidates:
        if candidate is None or candidate.size == 0:
            continue
        signature = (candidate.shape, int(candidate.mean()), int(candidate.std()))
        if signature in signatures:
            continue
        signatures.add(signature)
        unique_candidates.append(candidate)

    # First run normal decoding. If OpenCV can locate the QR but cannot decode
    # it at the full-certificate scale, immediately rectify the detected
    # quadrilateral and try a high-resolution crop with a quiet zone.
    for candidate in unique_candidates:
        try:
            ok, points = detector.detect(candidate)
            if ok and points is not None:
                pts = np.asarray(points, dtype=np.float32).reshape(-1, 2)
                if len(pts) >= 4:
                    pts = pts[:4]
                    # Order the four corners as top-left, top-right,
                    # bottom-right, bottom-left.
                    s = pts.sum(axis=1)
                    d = np.diff(pts, axis=1).reshape(-1)
                    ordered = np.array([
                        pts[np.argmin(s)],
                        pts[np.argmin(d)],
                        pts[np.argmax(s)],
                        pts[np.argmax(d)],
                    ], dtype=np.float32)

                    width_a = np.linalg.norm(ordered[2] - ordered[3])
                    width_b = np.linalg.norm(ordered[1] - ordered[0])
                    height_a = np.linalg.norm(ordered[1] - ordered[2])
                    height_b = np.linalg.norm(ordered[0] - ordered[3])
                    side = max(int(max(width_a, width_b)), int(max(height_a, height_b)), 100)
                    side = min(max(side * 4, 400), 2400)

                    target = np.array([
                        [0, 0],
                        [side - 1, 0],
                        [side - 1, side - 1],
                        [0, side - 1],
                    ], dtype=np.float32)
                    matrix = cv2.getPerspectiveTransform(ordered, target)
                    rectified = cv2.warpPerspective(
                        candidate, matrix, (side, side),
                        flags=cv2.INTER_CUBIC,
                        borderMode=cv2.BORDER_CONSTANT,
                        borderValue=255,
                    )

                    # A QR needs a quiet white border. Add one explicitly and
                    # try several resampling/threshold variants. This handles
                    # compressed certificate images where direct decoding fails.
                    for interpolation in (cv2.INTER_NEAREST, cv2.INTER_LINEAR, cv2.INTER_CUBIC, cv2.INTER_LANCZOS4):
                        scaled = cv2.resize(
                            rectified, None, fx=2.0, fy=2.0,
                            interpolation=interpolation,
                        )
                        border = max(20, int(side * 0.08))
                        bordered = cv2.copyMakeBorder(
                            scaled, border, border, border, border,
                            cv2.BORDER_CONSTANT, value=255,
                        )
                        gray_qr = cv2.cvtColor(bordered, cv2.COLOR_BGR2GRAY) if len(bordered.shape) == 3 else bordered
                        qr_variants = [
                            bordered,
                            gray_qr,
                            cv2.threshold(
                                gray_qr, 0, 255,
                                cv2.THRESH_BINARY + cv2.THRESH_OTSU,
                            )[1],
                            cv2.adaptiveThreshold(
                                gray_qr, 255,
                                cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                cv2.THRESH_BINARY, 51, 5,
                            ),
                        ]
                        for qr_variant in qr_variants:
                            try:
                                value, _, _ = detector.detectAndDecode(qr_variant)
                                add_value(value)
                            except Exception:
                                pass
                            if found:
                                return found
        except Exception:
            pass

        try:
            multi = detector.detectAndDecodeMulti(candidate)
            if multi and len(multi) == 4:
                ok, values, _, _ = multi
                if values is not None:
                    for value in values:
                        add_value(value)
        except Exception:
            pass
        try:
            value, _, _ = detector.detectAndDecode(candidate)
            add_value(value)
        except Exception:
            pass
        if found:
            return found

    return found

def detect_verification_sources(path: Path, content_type: str, text: str) -> list[dict]:
    detector = cv2.QRCodeDetector()
    sources = []
    if content_type == "application/pdf":
        document = fitz.open(str(path))
        try:
            for index, page in enumerate(document):
                pix = page.get_pixmap(matrix=fitz.Matrix(4, 4), alpha=False)
                image = cv2.imdecode(
                    __import__("numpy").frombuffer(pix.tobytes("png"), dtype="uint8"),
                    cv2.IMREAD_COLOR,
                )
                sources.extend(scan_image(image, detector, index + 1))
        finally:
            document.close()
    elif content_type.startswith("image/"):
        image = cv2.imread(str(path))
        if image is not None:
            sources.extend(scan_image(image, detector))
    for url in extract_urls(text):
        if not any(item["url"] == url for item in sources):
            sources.append({"source_type": "link", "url": url, "page": None})
    return sources


def first_match(patterns: list[str], text: str) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
        if match:
            value = re.sub(r"\s+", " ", match.group(1)).strip(" :.-\t\n").rstrip(".,;")
            if value:
                return value
    return None


def extract_issue_date(text: str) -> str | None:
    date_token = r"(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{1,2}\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s*,?\s*\d{2,4}|(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},?\s+\d{2,4})"
    label = r"(?:issue\s+date|date\s+of\s+issue|issued\s+(?:on|date)?|date\s+issued)"
    m = re.search(label + r"[^\n]{0,100}?" + r"(" + date_token + r")", text, re.I)
    if not m:
        m = re.search(label + r".{0,220}?" + r"(" + date_token + r")", text, re.I | re.S)
    if not m:
        # Many Cognitive Class certificates print only a standalone date,
        # e.g. “March 8, 2026”, without an “Issue date” label.  Use a
        # standalone date as a fallback, but never take a date from a URL.
        standalone = re.search(r"(?<![A-Za-z0-9])(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},?\s+\d{4}(?![A-Za-z0-9])", text, re.I)
        if not standalone:
            standalone = re.search(r"(?<![A-Za-z0-9])\d{1,2}[./-]\d{1,2}[./-]\d{2,4}(?![A-Za-z0-9])", text)
        return standalone.group(0).strip() if standalone else None
    value = re.sub(r"\s+", " ", m.group(1)).strip(" :.-\t\n").rstrip(".,;")
    return value or None

def extract_certificate_fields(text: str, sources: list[dict] | None = None) -> dict[str, str | None]:
    """Extract fields conservatively without assigning unrelated certificate text.

    Cognitive Class/IBM certificates commonly contain a recipient sentence, an
    ``Issued on`` date, and a validation URL.  In particular, a URL must never
    be captured as an end date and a course code must never be reported as a
    student name.
    """
    if not text or not text.strip():
        return {}

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"[ \t]+", " ", normalized)

    def clean(value: str | None) -> str | None:
        if not value:
            return None
        value = re.sub(r"\s+", " ", value).strip(" :.-\t\n")
        return value.rstrip(".,;") or None

    def match(patterns: list[str]) -> str | None:
        for pattern in patterns:
            found = re.search(pattern, normalized, flags=re.IGNORECASE | re.MULTILINE)
            if found:
                value = clean(found.group(1))
                if value:
                    return value
        return None

    # Extract URLs independently first. This prevents the validation URL from
    # being consumed by broad words such as "to" or "date".
    urls = re.findall(r"https?://[^\s<>\]\)]+", normalized, flags=re.IGNORECASE)
    verification_url = next(
        (u.rstrip(".,;\"'") for u in urls if any(host in u.lower() for host in (
            "cognitiveclass.ai", "courses.cognitiveclass.ai", "ibm.com", "skills.network"
        ))),
        None,
    )
    if not verification_url and sources:
        verification_url = next(
            (item.get("url") for item in sources
             if isinstance(item, dict) and isinstance(item.get("url"), str)
             and any(host in item["url"].lower() for host in ("cognitiveclass.ai", "skills.network", "ibm.com"))),
            None,
        )

    # Recipient-name extraction is intentionally structural. Verification pages
    # often say “This certificate has been issued to <NAME>” or put the name on
    # the line immediately after “This is to certify that”. A broad 100-character
    # regex can accidentally capture the following sentence/course title and
    # create a false mismatch. Prefer the text immediately following the marker
    # and stop at common sentence boundaries.
    def recipient_candidate(value: str | None) -> str | None:
        candidate = clean(value)
        if not candidate:
            return None
        candidate = re.sub(
            r"\s+(?:has|have|successfully|completed|for|from|on|in)\b.*$",
            "",
            candidate,
            flags=re.I,
        ).strip(" :.-,;\t\n")
        if not candidate or len(candidate) > 100:
            return None
        if re.search(r"(?:provided by|ibm|cognitive|skillsnetwork|skills network|certificate|course|acknowledges|http)", candidate, re.I):
            return None
        if re.search(r"\d", candidate) or not re.search(r"[A-Za-z]", candidate):
            return None
        return candidate

    student_name = None
    # 1) Explicit recipient markers. Keep the capture on one line when possible.
    recipient_patterns = [
        r"(?:this\s+is\s+to\s+certif(?:y|ied)\s+that|certificate\s+has\s+been\s+issued\s+to|(?:awarded|presented|issued)\s+to)\s*[:\-]?\s*([^\n\r]+)",
        r"(?:student|intern|trainee|candidate)\s*(?:full\s*)?name\s*[:\-]\s*([^\n\r]+)",
    ]
    for pattern in recipient_patterns:
        for found in re.finditer(pattern, normalized, flags=re.I | re.MULTILINE):
            candidate = recipient_candidate(found.group(1))
            if candidate:
                student_name = candidate
                break
        if student_name:
            break

    # 2) Marker on its own line followed by the recipient on the next line.
    if not student_name:
        lines = [clean(line) for line in normalized.split("\n")]
        lines = [line for line in lines if line]
        marker_re = re.compile(
            r"(?:this\s+is\s+to\s+certif(?:y|ied)\s+that|certificate\s+has\s+been\s+issued\s+to|(?:awarded|presented|issued)\s+to)\s*[:\-]?\s*$",
            re.I,
        )
        for index, line in enumerate(lines):
            if marker_re.search(line):
                for candidate_line in lines[index + 1:index + 4]:
                    candidate = recipient_candidate(candidate_line)
                    if candidate:
                        student_name = candidate
                        break
            if student_name:
                break

    # 3) Standalone recipient line used by many designed internship certificates.
    # Example: "CERTIFICATE OF INTERNSHIP" followed by the recipient name and
    # then the sentence beginning "For successfully completing...". This is
    # deliberately bounded so arbitrary all-caps text elsewhere on a certificate
    # is not mistaken for the recipient.
    if not student_name:
        standalone_patterns = [
            r"certificate\s+of\s+internship\s*\n+\s*([A-Z][A-Z .,'-]{1,100})\s*\n+\s*For\s+successfully\s+completing",
            r"certificate\s*\n+\s*of\s+internship\s*\n+\s*([A-Z][A-Z .,'-]{1,100})\s*\n+\s*For\s+successfully\s+completing",
            r"certificate\s+of\s+internship[^\n]*\n+\s*([A-Z][A-Z .,'-]{1,100})\s*\n+",
        ]
        for pattern in standalone_patterns:
            found = re.search(pattern, normalized, flags=re.IGNORECASE | re.MULTILINE)
            if found:
                candidate = found.group(1).strip()
                # Require the captured line to look like a person name rather
                # than a heading such as "YOUR SKILL SUCCESS JOURNEY".
                if len(candidate.split()) <= 8 and not re.search(
                    r"\b(?:certificate|internship|unified|mentor|skill|success|journey|verify)\b",
                    candidate, flags=re.I
                ):
                    student_name = recipient_candidate(candidate)
                    if student_name:
                        break

        # OCR commonly separates the heading as two lines: "CERTIFICATE" /
        # "OF INTERNSHIP". In that layout, inspect only the next few lines and
        # require the following line to start the certificate description.
        if not student_name:
            lines_for_name = [clean(line) for line in normalized.split("\n")]
            lines_for_name = [line for line in lines_for_name if line]
            for index, line in enumerate(lines_for_name):
                if not re.fullmatch(r"of\s+internship", line or "", flags=re.I):
                    continue
                if index == 0 or not re.search(r"certificate", lines_for_name[index - 1], flags=re.I):
                    continue
                for candidate_line in lines_for_name[index + 1:index + 4]:
                    if not re.fullmatch(r"[A-Z][A-Z .,'-]{1,100}", candidate_line or ""):
                        continue
                    if re.search(r"\b(?:certificate|internship|unified|mentor|skill|success|journey|verify)\b", candidate_line, flags=re.I):
                        continue
                    student_name = recipient_candidate(candidate_line)
                    if student_name:
                        break
                if student_name:
                    break

    # 4) Explicit labelled-name patterns as a fallback.
    if not student_name:
        student_name = recipient_candidate(match([
            r"(?:student|intern|trainee|candidate)\s*(?:full\s*)?name\s*[:\-]\s*([A-Za-z][A-Za-z .,'-]{0,100})",
            r"(?:awarded|presented|issued)\s+to\s*[:\-]?\s*([A-Za-z][A-Za-z .,'-]{0,100})",
        ]))

    # A legitimate recipient can be a single word, for example “Daniels”.
    # Do not require two or more words.
    if student_name:
        student_name = recipient_candidate(student_name)
    certificate_number = match([
        r"certificate\s+id\s+number\s*[:#\-]?\s*([A-Za-z0-9-]{8,})",
        r"(?:certificate|certification|credential|verification)\s*(?:no|number|id|code)\s*[:#\-]\s*([A-Za-z0-9./_-]{8,})",
        r"(?:credential|certificate)\s*#\s*([A-Za-z0-9./_-]{8,})",
    ])
    if not certificate_number and verification_url:
        tail = verification_url.rstrip('/').rsplit('/', 1)[-1]
        if re.fullmatch(r"[A-Za-z0-9-]{8,}", tail):
            certificate_number = tail
    # Browser pages may hide the URL in JavaScript. The source URL is still
    # authoritative for the certificate identifier.
    if not certificate_number and sources:
        for item in sources:
            source_url = item.get("url") if isinstance(item, dict) else None
            if isinstance(source_url, str):
                tail = source_url.rstrip('/').rsplit('/', 1)[-1]
                if re.fullmatch(r"[A-Za-z0-9-]{8,}", tail):
                    certificate_number = tail
                    break

    return {
        "student_name": student_name,
        "company_name": match([
            r"(?:company|organization|organisation|employer|host company|host organization)\s*(?:name)?\s*[:\-]\s*([^\n]+)",
        ]),
        "internship_role": match([
            r"(?:internship|job|training)\s*(?:role|title|position)\s*[:\-]\s*([^\n]+)",
            r"(?:role|position|designation|job title)\s*[:\-]\s*([^\n]+)",
        ]),
        "start_date": match([
            r"(?:start|starting|commencement|joining)\s*(?:date)?\s*[:\-]\s*([^\n]+)",
            r"(?:internship|training)\s+(?:started|commenced)\s+(?:on|from)\s*[:\-]?\s*([^\n]+)",
        ]),
        "end_date": match([
            r"(?:end|ending|completion|termination)\s*(?:date)?\s*[:\-]\s*([^\n]+)",
            r"(?:internship|training)\s+(?:ended|completed)\s+(?:on|by)\s*[:\-]?\s*([^\n]+)",
        ]),
        "duration": match([
            r"(?:duration|period|tenure)\s*[:\-]\s*([^\n]+)",
            r"for a period of\s+([0-9]+\s+(?:days?|weeks?|months?|years?))",
        ]),
        "issue_date": extract_issue_date(normalized),
        "certificate_number": certificate_number,
        "verification_url": verification_url,
    }


def locate_file(certificate_id: str) -> Path:
    matches = list(UPLOAD_DIR.glob(f"{certificate_id}_*"))
    if not matches:
        raise HTTPException(status_code=404, detail="Certificate not found")
    return matches[0]


def extract_page_title(html: str) -> str | None:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    title = re.sub(r"<[^>]+>", " ", match.group(1))
    title = re.sub(r"\s+", " ", title).strip()
    return title[:300] or None

def verify_with_browser(url: str) -> dict:
    worker = Path(__file__).with_name("browser_worker.py")
    try:
        completed = subprocess.run(
            [sys.executable, str(worker), url],
            capture_output=True,
            text=True,
            timeout=50,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "browser worker failed").strip()[-1000:]
            return {"status": "verification_unavailable", "message": f"Browser verification error: {detail}", "http_status": None, "final_url": url, "page_title": None, "reachable": False}
        return json.loads(completed.stdout)
    except subprocess.TimeoutExpired:
        return {"status": "verification_unavailable", "message": "Browser verification timed out.", "http_status": None, "final_url": url, "page_title": None, "reachable": False}
    except Exception as exc:
        return {"status": "verification_unavailable", "message": f"Browser verification error: {exc}", "http_status": None, "final_url": url, "page_title": None, "reachable": False}


def classify_verification(response: httpx.Response) -> tuple[str, str]:
    if not response.is_success:
        return "verification_failed", f"Verification page returned HTTP {response.status_code}."

    body = response.text.lower()
    # Known certificate-record pages, including IBM Skills Network / Cognitive Class.
    if (
        ("ibmskillsnetwork" in body or "cognitive class acknowledges" in body)
        and ("certificate" in body or "accomplishment" in body)
    ):
        return "verified", "The certificate record was found on the issuing platform."

    positive_markers = (
        "certificate is valid",
        "certificate verified",
        "certificate has been verified",
        "verification successful",
        "verification completed",
        "credential verified",
        "valid certificate",
        "authentic certificate",
        "successfully verified",
    )
    negative_markers = (
        "certificate not found",
        "certificate does not exist",
        "invalid certificate",
        "verification failed",
        "invalid credential",
        "credential not found",
        "record not found",
        "no record found",
        "verification unsuccessful",
    )

    if any(marker in body for marker in negative_markers):
        return "verification_failed", "The verification page indicates that the certificate is invalid or was not found."
    if any(marker in body for marker in positive_markers):
        return "verified", "The verification page contains a positive certificate-verification result."
    return "verification_unavailable", "The page was reachable, but no clear verification result was detected. The site may require JavaScript or manual review."


@app.get("/health")
def health():
    return {"status": "ok", "service": "internverify-api", "version": "0.9.0"}


@app.post("/api/certificates/upload")
async def upload_certificate(file: UploadFile = File(...)):
    original_name = file.filename or "unknown"
    certificate_id = str(uuid4())
    stored_name = f"{certificate_id}_{Path(original_name).name}"
    destination = UPLOAD_DIR / stored_name
    contents = await file.read()
    file_hash = hashlib.sha256(contents).hexdigest()
    duplicate = find_duplicate(file_hash)
    destination.write_bytes(contents)
    content_type = (file.content_type or "").lower()
    # Some browsers send screenshots/scanned images as application/octet-stream.
    # Fall back to the file extension so images receive the same OCR treatment.
    if not content_type or content_type == "application/octet-stream":
        suffix = Path(original_name).suffix.lower()
        content_type = {
            ".pdf": "application/pdf",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".bmp": "image/bmp",
            ".tif": "image/tiff",
            ".tiff": "image/tiff",
        }.get(suffix, content_type or "application/octet-stream")
    text = ""
    extraction_note = ""

    if content_type == "application/pdf":
        try:
            text = extract_pdf_text(destination)
            extraction_note = (
                "PDF text extracted successfully."
                if text
                else "No selectable PDF text found. QR scanning was still attempted."
            )
        except Exception as exc:
            extraction_note = f"Text extraction failed: {type(exc).__name__}. QR scanning was still attempted."
    elif content_type.startswith("image/"):
        try:
            text = extract_image_text(destination)
            extraction_note = (
                "Image OCR extracted successfully."
                if text
                else "No readable text detected in the image. QR scanning was still attempted."
            )
        except Exception as exc:
            extraction_note = f"Image OCR failed: {type(exc).__name__}. QR scanning was still attempted."

    sources = detect_verification_sources(destination, content_type, text)
    fields = extract_certificate_fields(text, sources) if text or sources else {}
    save_history((certificate_id, original_name, fields.get("student_name"), fields.get("issue_date"), "needs_review", datetime.now(timezone.utc).isoformat(), sources[0]["url"] if sources else None, "Duplicate certificate detected." if duplicate else "Uploaded; verification pending.", file_hash))
    return {
        "certificate_id": certificate_id,
        "filename": original_name,
        "stored_filename": stored_name,
        "content_type": content_type,
        "size_bytes": len(contents),
        "status": "verification_source_found" if sources else "no_verification_source",
        "next_step": "verify_certificate" if sources else "manual_source_check",
        "extraction_note": extraction_note,
        "verification_sources": sources,
        "text_preview": text[:2000],
        "extracted_fields": fields,
        "duplicate": duplicate is not None,
        "duplicate_of": duplicate,
    }


@app.get("/api/certificates/{certificate_id}/file")
def certificate_file(certificate_id: str):
    path = locate_file(certificate_id)
    media_type = "application/pdf" if path.suffix.lower() == ".pdf" else None
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Content-Disposition": f'inline; filename="{path.name}"'},
    )


def _unavailable(value) -> bool:
    """Return True when OCR/browser extraction did not provide comparable data."""
    if value is None:
        return True
    text = str(value).casefold().strip()
    if not text:
        return True
    compact = re.sub(r"[^a-z0-9 ]+", " ", text)
    compact = re.sub(r"\s+", " ", compact).strip()
    markers = (
        "not enough data to compare",
        "not enough information to compare",
        "insufficient data to compare",
        "not available",
        "data not available",
        "value not available",
        "unavailable",
        "not detected",
        "not found",
        "missing",
        "unknown",
        "n/a",
        "na",
        "none",
        "null",
    )
    return any(marker in compact for marker in markers)


def _present(value) -> bool:
    return not _unavailable(value)


def _normalise_date(value: str) -> str | None:
    text = str(value).casefold().strip()
    text = re.sub(r"[,.-]", "/", text)
    text = re.sub(r"\s+", " ", text)
    months = {
        "january": 1, "jan": 1, "february": 2, "feb": 2,
        "march": 3, "mar": 3, "april": 4, "apr": 4,
        "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
        "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
        "october": 10, "oct": 10, "november": 11, "nov": 11,
        "december": 12, "dec": 12,
    }
    m = re.search(r"(\d{1,2})\s*/\s*([a-z]+)\s*/\s*(\d{2,4})", text)
    if m and m.group(2) in months:
        day, month, year = int(m.group(1)), months[m.group(2)], int(m.group(3))
    else:
        m = re.search(r"([a-z]+)\s+(\d{1,2})\s*/\s*(\d{2,4})", text)
        if m and m.group(1) in months:
            month, day, year = months[m.group(1)], int(m.group(2)), int(m.group(3))
        else:
            m = re.search(r"(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{2,4})", text)
            if not m:
                return None
            day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if year < 100:
        year += 2000 if year < 70 else 1900
    return f"{year:04d}-{month:02d}-{day:02d}"


def _normalise(value, field: str | None = None) -> str:
    text = str(value).casefold().strip()
    if field in {"issue_date", "start_date", "end_date"}:
        parsed = _normalise_date(text)
        if parsed:
            return parsed
    text = re.sub(
        r"\b(student name|issue date|certificate number|company name|internship role|start date|end date|duration)\b\s*[:#-]?",
        "",
        text,
    )
    return re.sub(r"[^a-z0-9]", "", text)


def compare_certificate_fields(uploaded: dict, browser: dict) -> dict:
    # Issue date is included in verification because an edited issue date must
    # be detected as a real field mismatch against the verification record.
    labels = {
        "student_name": "Student name",
        "certificate_number": "Certificate number",
        "company_name": "Company name",
        "internship_role": "Internship role",
        "start_date": "Start date",
        "end_date": "End date",
        "duration": "Duration",
        # The issued date is a mapped verification field. If it differs from
        # the verification record, the certificate must be Not Verified.
        "issue_date": "Issue date",
    }
    comparisons = []
    for key, label in labels.items():
        left, right = uploaded.get(key), browser.get(key)
        # Unavailable values are ignored only when the field is unavailable on
        # both sides. If one side has a real value and the other side does not,
        # retain the row so the final result becomes Needs Review.
        left_unavailable = _unavailable(left)
        right_unavailable = _unavailable(right)
        if left_unavailable and right_unavailable:
            continue
        if left_unavailable != right_unavailable:
            comparisons.append({
                "field": key, "label": label, "uploaded": left,
                "verification_page": right, "matched": None,
                "missing_on_one_side": True,
            })
            continue
        matched = _normalise(left, key) == _normalise(right, key)
        comparisons.append({
            "field": key, "label": label, "uploaded": left,
            "verification_page": right, "matched": matched,
        })
    mismatches = [item for item in comparisons if item["matched"] is False]
    one_sided = [item for item in comparisons if item.get("missing_on_one_side")]
    return {
        "items": comparisons,
        "matched": sum(item["matched"] is True for item in comparisons),
        "compared": len(comparisons),
        "mismatches": len(mismatches),
        "missing": len(one_sided),
        "has_mismatch": bool(mismatches),
        "has_missing": bool(one_sided),
    }


def classify_match(uploaded: dict, browser: dict, browser_status: str) -> tuple[str, str]:
    report = compare_certificate_fields(uploaded, browser)
    if report["has_mismatch"]:
        return "not_verified", "Mismatch details"
    if report["has_missing"]:
        return "needs_review", "A mapped field is available on only one side."
    if report["compared"] > 0 and report["matched"] == report["compared"]:
        return "verified", "All available mapped fields matched exactly."
    return "ignore", "No comparable mapped data was found."

@app.post("/api/certificates/{certificate_id}/verify")
async def verify_certificate(certificate_id: str):
    path = locate_file(certificate_id)
    content_type = "application/pdf" if path.suffix.lower() == ".pdf" else "image/" + path.suffix.lower().lstrip(".")
    uploaded_text = extract_pdf_text(path) if content_type == "application/pdf" else extract_image_text(path)
    raw_sources = detect_verification_sources(path, content_type, uploaded_text)
    # OCR can create broken duplicate URLs from the printed certificate link.
    # Keep only complete HTTP(S) URLs, deduplicate them, and prefer QR sources.
    uploaded_sources = []
    seen_urls = set()
    for source in sorted(raw_sources, key=lambda item: 0 if item.get("source_type") == "qr" else 1):
        url = str(source.get("url") or "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.path:
            continue
        normalized_url = url.rstrip("/")
        if normalized_url in seen_urls:
            continue
        seen_urls.add(normalized_url)
        uploaded_sources.append({**source, "url": normalized_url})
    uploaded_fields = extract_certificate_fields(uploaded_text, uploaded_sources)

    if not uploaded_sources:
        return {"status": "no_verification_source", "message": "No QR code or verification link was found in the uploaded certificate.", "verification_results": [], "browser_text": "", "match_report": {}}

    # Evaluate only the primary verification source. When a QR source exists,
    # OCR-created duplicate URLs are ignored because they can contain truncated
    # or noisy text and must never change the result.
    primary_sources = [s for s in uploaded_sources if s.get("source_type") == "qr"]
    if not primary_sources:
        primary_sources = uploaded_sources[:1]

    results = []
    for source in primary_sources:
        browser_result = verify_with_browser(source["url"])
        browser_text = browser_result.get("text") or ""
        browser_fields = extract_certificate_fields(browser_text, [{"url": source["url"], "source_type": "link"}]) if browser_text else {}
        match_report = compare_certificate_fields(uploaded_fields, browser_fields)
        result = {**source, **browser_result, "browser_text": browser_text, "browser_fields": browser_fields, "match_report": match_report}
        results.append(result)

    classifications = []
    for item in results:
        status, reason = classify_match(uploaded_fields, item.get("browser_fields", {}), item.get("status", ""))
        classifications.append((status, reason))

    # Field comparison is authoritative. A matching URL/QR identifier alone
    # must not override a real name mismatch or a one-sided missing field.
    actionable = [(status, reason) for status, reason in classifications if status != "ignore"]
    if any(status == "not_verified" for status, _ in actionable):
        overall_status, reason = "not_verified", next(reason for status, reason in actionable if status == "not_verified")
        message = "The uploaded certificate could not be accepted because the verification data does not match."
    elif any(status == "needs_review" for status, _ in actionable):
        overall_status, reason = "needs_review", next(reason for status, reason in actionable if status == "needs_review")
        message = "Faculty review is required because some mapped fields are missing or unavailable on one side."
    elif any(status == "verified" for status, _ in actionable):
        overall_status, reason = "verified", "All available mapped fields matched exactly."
        message = "All available mapped fields matched exactly. The certificate is verified."
    else:
        overall_status, reason = "ignore", "No comparable mapped data was found."
        message = "No comparable mapped data was found; the source was ignored."

    first = results[0] if results else {}
    browser_text = "\n\n".join(item.get("browser_text", "") for item in results if item.get("browser_text"))
    browser_fields = next((item.get("browser_fields", {}) for item in results if item.get("browser_fields")), {})
    file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    stored_name = path.name.split("_", 1)[-1]
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""UPDATE verification_history SET student_name=?, issue_date=?, status=?, verified_at=?, verification_url=?, message=?, reason=?, uploaded_fields_json=?, browser_fields_json=?, browser_text=?, match_report_json=? WHERE certificate_id=?""", (browser_fields.get("student_name") or uploaded_fields.get("student_name"), browser_fields.get("issue_date") or uploaded_fields.get("issue_date"), overall_status, datetime.now(timezone.utc).isoformat(), first.get("url"), message, reason, json.dumps(uploaded_fields), json.dumps(browser_fields), browser_text, json.dumps(first.get("match_report", {})), certificate_id))
        db.commit()
    return {
        "status": overall_status,
        "message": message,
        "verification_results": results,
        "browser_text": browser_text,
        "browser_fields": browser_fields,
        "uploaded_fields": uploaded_fields,
        "match_report": first.get("match_report", {}),
    }


@app.get("/api/history")
def history():
    return {"items": get_history()}

@app.get("/api/history/{certificate_id}/duplicates")
def history_duplicates(certificate_id: str):
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        current = db.execute("SELECT file_hash FROM verification_history WHERE certificate_id = ?", (certificate_id,)).fetchone()
        if not current or not current["file_hash"]:
            return {"items": []}
        rows = db.execute("SELECT * FROM verification_history WHERE file_hash = ? AND certificate_id != ? ORDER BY id DESC", (current["file_hash"], certificate_id)).fetchall()
        return {"items": [dict(row) for row in rows]}


@app.patch("/api/history/{certificate_id}/review")
def review_history(certificate_id: str, payload: dict):
    notes = str(payload.get("review_notes") or "").strip()
    reviewed = 1 if payload.get("reviewed", True) else 0
    if reviewed and not notes:
        raise HTTPException(status_code=400, detail="Review remarks are mandatory before marking a certificate as reviewed.")
    with sqlite3.connect(DB_PATH) as db:
        if reviewed:
            cursor = db.execute(
                "UPDATE verification_history SET review_notes = ?, reviewed = 1, status = 'verified', reason = ? WHERE certificate_id = ?",
                (notes, f"Faculty manually reviewed and approved: {notes}", certificate_id),
            )
        else:
            cursor = db.execute(
                "UPDATE verification_history SET review_notes = ?, reviewed = 0 WHERE certificate_id = ?",
                (notes, certificate_id),
            )
        db.commit()
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="History record not found")
    return {"ok": True, "certificate_id": certificate_id, "review_notes": notes, "reviewed": bool(reviewed), "status": "verified" if reviewed else None}
"""Render a certificate verification URL and return clean, useful page text."""
import json
import sys
import io
import re

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None
    np = None
try:
    from PIL import Image, ImageEnhance, ImageFilter
except ImportError:
    Image = None
    ImageEnhance = None
    ImageFilter = None
try:
    import pytesseract
except ImportError:
    pytesseract = None
try:
    import zxingcpp
except ImportError:
    zxingcpp = None

url = sys.argv[1]

try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 1000},
        )
        response = page.goto(url, wait_until="domcontentloaded", timeout=30000)

        # Verification sites frequently render the certificate record with JS
        # after the initial document has loaded. Wait for network activity to
        # settle, then give the UI a bounded opportunity to populate its fields.
        try:
            page.wait_for_load_state("networkidle", timeout=12000)
        except PlaywrightTimeoutError:
            pass
        try:
            page.wait_for_function(
                """() => {
                    const t = (document.body?.innerText || '').toLowerCase();
                    return /completed by|student name|recipient|domain|internship role|start date|end date|duration|certificate details|verification result|verified certificate|successfully completed/.test(t);
                }""",
                timeout=7000,
            )
        except PlaywrightTimeoutError:
            pass
        # Some certificate portals render their record after a delayed fetch.
        page.wait_for_timeout(800)

        title = page.title()
        final_url = page.url
        status = response.status if response else None
        text_parts = []
        seen_blocks = set()
        seen_lines = set()

        def add_text(value):
            # Deduplicate at line level because certificate portals commonly
            # repeat the same record in <main>, <table>, and <body>.
            lines = []
            for line in (value or "").splitlines():
                line = " ".join(line.split()).strip()
                if not line:
                    continue
                key = line.casefold()
                if key not in seen_lines:
                    seen_lines.add(key)
                    lines.append(line)
            block = "\n".join(lines)
            if block and block not in seen_blocks:
                seen_blocks.add(block)
                text_parts.append(block)

        # First extract semantic label/value pairs. Many verification pages
        # render labels and values in separate DOM nodes, so raw innerText can
        # lose the association needed by extract_certificate_fields().
        try:
            pairs = page.evaluate("""() => {
                const visible = el => {
                    const s = getComputedStyle(el);
                    const r = el.getBoundingClientRect();
                    return s && s.display !== 'none' && s.visibility !== 'hidden' &&
                           Number(s.opacity || 1) !== 0 && r.width > 0 && r.height > 0;
                };
                const clean = s => (s || '').replace(/\s+/g, ' ').trim();
                const out = [];
                const add = (label, value) => {
                    label = clean(label).replace(/[:：]$/, '');
                    value = clean(value);
                    if (!label || !value || label.length > 80 || value.length > 500) return;
                    if (label.toLowerCase() === value.toLowerCase()) return;
                    out.push(label + ': ' + value);
                };
                // Definition lists preserve an explicit label/value relation.
                document.querySelectorAll('dt').forEach(label => {
                    if (!visible(label)) return;
                    const value = label.nextElementSibling;
                    if (value && value.matches('dd') && visible(value)) add(label.innerText, value.innerText);
                });
                // Table headers and cells: pair each row's first cell with its value.
                document.querySelectorAll('tr').forEach(row => {
                    if (!visible(row)) return;
                    const cells = [...row.querySelectorAll(':scope > th, :scope > td')].filter(visible);
                    if (cells.length >= 2) add(cells[0].innerText, cells.slice(1).map(c => c.innerText).join(' '));
                });
                // Common label/value layouts used by credential providers.
                document.querySelectorAll('label, [class*="label" i], [data-testid*="label" i]').forEach(label => {
                    if (!visible(label)) return;
                    const parent = label.parentElement;
                    if (!parent || !visible(parent)) return;
                    let value = label.nextElementSibling;
                    if (!value) value = parent.querySelector('dd, output, input, [class*="value" i], [data-testid*="value" i]');
                    if (value && value !== label && visible(value)) {
                        add(label.innerText || label.getAttribute('aria-label'), value.value || value.innerText || value.textContent);
                    }
                });
                return [...new Set(out)];
            }""")
            add_text("\n".join(pairs || []))
        except Exception:
            pass

        # Prefer focused certificate-record containers before page-wide text.
        # This keeps navigation and footer content from winning field matches.
        for selector in ("main", "[role=main]", "article", "[class*='certificate' i]", "[class*='credential' i]", "table", "dl"):
            try:
                for element in page.locator(selector).all():
                    if element.is_visible():
                        add_text(element.inner_text(timeout=1500))
            except Exception:
                continue
        try:
            add_text(page.locator("body").inner_text(timeout=10000))
        except Exception:
            pass

        # Include visible text from embedded frames when a verification record
        # is hosted inside an iframe. Avoid duplicating the top-level body.
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                add_text(frame.locator("body").inner_text(timeout=1500))
            except Exception:
                continue

        # Metadata can contain a certificate/recipient title not repeated in
        # the visible body. Keep only meaningful values, not arbitrary scripts.
        try:
            metadata = page.locator(
                'meta[name="description"], meta[property="og:title"], '
                'meta[property="og:description"]'
            ).evaluate_all(
                "els => els.map(e => e.getAttribute('content') || '').filter(Boolean)"
            )
            add_text("\n".join(metadata))
        except Exception:
            pass


        # Image-only verification pages need OCR and QR decoding. Inspect
        # visible certificate-sized images first, then use a full-page screenshot
        # fallback for canvas/background/embedded preview renderers.
        image_text_parts = []
        image_qr_parts = []
        if Image is not None:
            try:
                image_items = page.locator("img").evaluate_all("""els => els.map((el, i) => {
                    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
                    return {i, w: Math.round(r.width), h: Math.round(r.height),
                            nw: el.naturalWidth || 0, nh: el.naturalHeight || 0,
                            visible: !!s && s.display !== 'none' && s.visibility !== 'hidden' &&
                                     Number(s.opacity || 1) > 0 && r.width > 100 && r.height > 70};
                }).filter(x => x.visible && x.nw > 100 && x.nh > 70)
                  .sort((a,b) => (b.nw*b.nh)-(a.nw*a.nh)).slice(0, 8)""")
                for item in image_items:
                    try:
                        raw = page.locator("img").nth(item["i"]).screenshot(timeout=2500, animations="disabled")
                        pil = Image.open(io.BytesIO(raw)).convert("RGB")
                        if cv2 is not None and np is not None:
                            frame = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
                            candidates = [frame]
                            if max(frame.shape[:2]) < 1800:
                                candidates.append(cv2.resize(frame, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC))
                            if zxingcpp is not None:
                                for candidate in candidates:
                                    try:
                                        for barcode in zxingcpp.read_barcodes(candidate):
                                            value = (getattr(barcode, "text", "") or "").strip()
                                            if value and value not in image_qr_parts:
                                                image_qr_parts.append(value)
                                    except Exception:
                                        pass
                            try:
                                value, _, _ = cv2.QRCodeDetector().detectAndDecode(frame)
                                if value and value not in image_qr_parts:
                                    image_qr_parts.append(value)
                            except Exception:
                                pass
                        if pytesseract is not None:
                            if max(pil.size) > 2400:
                                pil.thumbnail((2400, 2400))
                            gray = pil.convert("L")
                            if max(gray.size) < 1800:
                                gray = gray.resize((gray.width*2, gray.height*2))
                            if ImageEnhance is not None:
                                gray = ImageEnhance.Contrast(gray).enhance(1.8)
                            for config in ("--oem 1 --psm 6", "--oem 1 --psm 11"):
                                try:
                                    value = pytesseract.image_to_string(gray, config=config).strip()
                                    if value and value not in image_text_parts:
                                        image_text_parts.append(value[:14000])
                                except Exception:
                                    pass
                    except Exception:
                        continue
            except Exception:
                pass

        dom_text_for_check = "\n".join(text_parts)
        has_credential_fields = bool(re.search(
            r"student name|recipient|completed by|certificate number|certificate id|"
            r"start date|end date|issue date|issued date|duration|issued to|congratulates|"
            r"successfully completing|successfully completed",
            dom_text_for_check, flags=re.I
        ))
        if Image is not None and pytesseract is not None and (not has_credential_fields or not image_text_parts):
            try:
                raw = page.screenshot(full_page=True, timeout=4000, animations="disabled", caret="hide")
                pil = Image.open(io.BytesIO(raw)).convert("L")
                if max(pil.size) > 2600:
                    pil.thumbnail((2600, 2600))
                if max(pil.size) < 1800:
                    pil = pil.resize((pil.width*2, pil.height*2))
                if ImageEnhance is not None:
                    pil = ImageEnhance.Contrast(pil).enhance(1.8)
                if ImageFilter is not None:
                    pil = pil.filter(ImageFilter.SHARPEN)
                for config in ("--oem 1 --psm 11", "--oem 1 --psm 6"):
                    try:
                        value = pytesseract.image_to_string(pil, config=config).strip()
                        if value and value not in image_text_parts:
                            image_text_parts.append(value[:16000])
                    except Exception:
                        pass
            except Exception:
                pass

        if image_qr_parts:
            add_text("\n".join("QR decoded from verification-page image: " + value for value in image_qr_parts))
        if image_text_parts:
            add_text("\n".join("Text extracted from verification-page image:\n" + value for value in image_text_parts))

        text = "\n\n".join(text_parts)
        browser.close()

    low = text.casefold()
    positive_markers = [
        "certificate verified", "verification successful", "successfully verified",
        "certificate is valid", "credential verified", "issued to",
        "certificate details", "verification completed", "valid certificate",
        "authentic certificate", "completed by", "start date", "end date",
    ]
    negative_markers = [
        "certificate not found", "invalid certificate", "verification failed",
        "does not exist", "not a valid certificate", "credential not found",
        "record not found",
    ]
    if any(marker in low for marker in negative_markers):
        result = "failed"
        msg = "Certificate verification failed."
    elif any(marker in low for marker in positive_markers):
        result = "verified"
        msg = "Verification page content was retrieved using browser rendering."
    else:
        result = "verification_unavailable"
        msg = "The page loaded, but no certificate fields or clear verification result were detected."

    print(json.dumps({
        "status": result,
        "message": msg,
        "http_status": status,
        "final_url": final_url,
        "page_title": title,
        "reachable": True,
        "text": text,
    }))
except Exception as exc:
    print(json.dumps({
        "status": "verification_unavailable",
        "message": f"Browser verification error: {exc}",
        "http_status": None,
        "final_url": url,
        "page_title": None,
        "reachable": False,
        "text": "",
    }))

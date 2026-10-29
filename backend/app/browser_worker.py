"""Render a certificate verification URL and return clean, useful page text."""
import json
import sys

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

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
                    return /completed by|student name|recipient|domain|internship role|start date|end date|duration|certificate details|verification result/.test(t);
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
                const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
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

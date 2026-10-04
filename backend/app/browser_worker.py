import json, sys, ipaddress, socket
from urllib.parse import urlparse
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

url=sys.argv[1]

def public_host(value):
    p=urlparse((value or "").strip())
    if p.scheme not in {"http","https"} or not p.hostname: return False
    host=p.hostname.rstrip(".").lower()
    if host in {"localhost","localhost.localdomain","metadata.google.internal","metadata"}: return False
    try:
        addrs={x[4][0] for x in socket.getaddrinfo(host,p.port or (443 if p.scheme=="https" else 80),type=socket.SOCK_STREAM)}
    except OSError: return False
    return bool(addrs) and all(not (ipaddress.ip_address(a).is_private or ipaddress.ip_address(a).is_loopback or ipaddress.ip_address(a).is_link_local or ipaddress.ip_address(a).is_multicast or ipaddress.ip_address(a).is_reserved or ipaddress.ip_address(a).is_unspecified) for a in addrs)

if not public_host(url):
    print(json.dumps({"status":"verification_unavailable","message":"The verification URL is not allowed because it is not a public HTTP(S) address.","http_status":None,"final_url":url,"page_title":None,"reachable":False,"text":""}))
    raise SystemExit(0)

try:
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        context=browser.new_context(user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",viewport={"width":1440,"height":1000})
        page=context.new_page()
        origin=urlparse(url).netloc.lower()
        json_responses=[]

        def capture(response):
            try:
                rp=urlparse(response.url)
                ct=(response.headers.get("content-type") or "").lower()
                if rp.netloc.lower()==origin and ("application/json" in ct or "+json" in ct):
                    body=response.text()
                    if body: json_responses.append(body[:1200000])
            except Exception: pass

        page.on("response",capture)

        def guard(route):
            target=route.request.url
            if target.startswith(("http://","https://")) and not public_host(target):
                route.abort(); return
            route.continue_()

        page.route("**/*",guard)
        response=page.goto(url,wait_until="domcontentloaded",timeout=30000)
        if not public_host(page.url): raise RuntimeError("The verification page redirected to an unsafe network address.")
        try: page.wait_for_selector("body",timeout=15000)
        except PlaywrightTimeoutError: pass
        try:
            page.wait_for_function("""() => { const t=(document.body?.innerText||'').trim(); return t.length>=80 || document.querySelector('main,article,[data-certificate],[class*="certificate"]'); }""",timeout=15000)
        except PlaywrightTimeoutError: pass
        try: page.wait_for_load_state("networkidle",timeout=8000)
        except PlaywrightTimeoutError: pass

        body=page.locator("body").inner_text(timeout=10000)
        try:
            focused=page.locator("main,article,[role='main'],[data-certificate],[class*='certificate'],[id*='certificate']").all_inner_texts()
        except Exception: focused=[]
        try:
            structured=page.evaluate("""() => {
              const o=[];
              for(const n of document.querySelectorAll('script[type="application/ld+json"]')) if(n.textContent) o.push('JSON-LD: '+n.textContent);
              for(const n of document.querySelectorAll('meta[name],meta[property]')) {
                const k=n.getAttribute('name')||n.getAttribute('property'),v=n.getAttribute('content');
                if(k&&v&&/name|title|description|date|issued|author|creator|certificate|credential|course|url/i.test(k)) o.push('META '+k+': '+v);
              }
              return o.slice(0,300);
            }""") or []
        except Exception: structured=[]

        parts=[body]
        parts += [x for x in focused if x and x.strip() and x.strip() not in body]
        parts += structured
        parts += ["NETWORK JSON: "+x for x in json_responses[-20:]]
        text="\n\n".join(x for x in parts if x)
        title=page.title(); final_url=page.url; status=response.status if response else None
        browser.close()

    low=text.lower()
    negative=("certificate not found","invalid certificate","verification failed","does not exist","not a valid certificate","credential not found","record not found","no certificate found","verification unsuccessful")
    positive=("certificate verified","verification successful","successfully verified","certificate is valid","credential verified","issued to","certificate details","verification completed","valid certificate","authentic certificate","certificate of internship","certificate id","credential id","issued on","date of issue","this certificate")
    if any(x in low for x in negative): result,msg="failed","The verification page indicates that the certificate is invalid or was not found."
    elif any(x in low for x in positive): result,msg="verified","Certificate verification data was found using browser rendering."
    else: result,msg="verification_unavailable","The page loaded, but no clear certificate data or verification result was detected."
    print(json.dumps({"status":result,"message":msg,"http_status":status,"final_url":final_url,"page_title":title,"reachable":True,"text":text}))
except Exception as e:
    print(json.dumps({"status":"verification_unavailable","message":f"Browser verification error: {e}","http_status":None,"final_url":url,"page_title":None,"reachable":False,"text":""}))

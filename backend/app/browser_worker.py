import json, sys
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

url=sys.argv[1]
try:
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36", viewport={"width":1440,"height":1000})
        response=page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try: page.wait_for_load_state("networkidle", timeout=10000)
        except PlaywrightTimeoutError: pass
        text=page.locator("body").inner_text(timeout=10000)
        title=page.title(); final_url=page.url; status=response.status if response else None
        browser.close()
    low=text.lower()
    pos=["certificate verified","verification successful","successfully verified","certificate is valid","credential verified","issued to","certificate details","verification completed","valid certificate","authentic certificate"]
    neg=["certificate not found","invalid certificate","verification failed","does not exist","not a valid certificate","credential not found","record not found"]
    if any(x in low for x in pos): result="verified"; msg="Certificate verified using browser rendering."
    elif any(x in low for x in neg): result="failed"; msg="Certificate verification failed."
    else: result="verification_unavailable"; msg="The page loaded, but no clear verification result was detected."
    print(json.dumps({"status":result,"message":msg,"http_status":status,"final_url":final_url,"page_title":title,"reachable":True,"text":text}))
except Exception as e:
    print(json.dumps({"status":"verification_unavailable","message":f"Browser verification error: {e}","http_status":None,"final_url":url,"page_title":None,"reachable":False,"text":""}))

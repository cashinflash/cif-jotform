import os
#!/usr/bin/env python3
"""
Cash in Flash - JotForm Auto-Processor v10
Polls JotForm, underwrites via Claude, saves to Firebase.
"""

import json, os, re, ssl, base64, time, http.client
from datetime import datetime

JOTFORM_API_KEY   = os.environ.get("JOTFORM_API_KEY", "9920086b812181a67a8d135ef649c11b")
JOTFORM_FORM_ID   = os.environ.get("JOTFORM_FORM_ID", "252566222647157")
JOTFORM_FORM_ID_2 = os.environ.get("JOTFORM_FORM_ID_2", "252556565318060")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
FIREBASE_HOST     = "cashinflash-a1dce-default-rtdb.firebaseio.com"
POLL_INTERVAL     = 10

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jotform_state.json")
TODAY = __import__("datetime").date.today().isoformat()

def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f: return json.load(f)
    except: pass
    return {"processed_ids": []}

def save_state(s):
    try:
        with open(STATE_FILE, "w") as f: json.dump(s, f)
    except: pass

def get_submissions(form_id):
    ctx = ssl.create_default_context()
    conn = http.client.HTTPSConnection("api.jotform.com", timeout=30, context=ctx)
    path = "/form/{}/submissions?apiKey={}&limit=20&orderby=created_at".format(form_id, JOTFORM_API_KEY)
    conn.request("GET", path, headers={"User-Agent": "CIF/1.0"})
    resp = conn.getresponse()
    data = json.loads(resp.read().decode())
    conn.close()
    return data

def download_pdf(url):
    try:
        if "jotform.com" in url:
            sep = "&" if "?" in url else "?"
            url = url + sep + "apiKey=" + JOTFORM_API_KEY
        url_no_scheme = url.replace("https://", "").replace("http://", "")
        host = url_no_scheme.split("/")[0]
        path = "/" + "/".join(url_no_scheme.split("/")[1:])
        ctx = ssl.create_default_context()
        conn = http.client.HTTPSConnection(host, timeout=60, context=ctx)
        conn.request("GET", path, headers={"User-Agent": "CIF/1.0"})
        resp = conn.getresponse()
        if resp.status in (301, 302):
            loc = resp.getheader("Location")
            conn.close()
            return download_pdf(loc) if loc else None
        data = resp.read()
        conn.close()
        return base64.b64encode(data).decode() if len(data) > 500 else None
    except Exception as e:
        print("[ERROR] PDF download:", e)
        return None

def find_pdf(answers):
    pdf_url = None
    info_lines = []
    for key, ans in answers.items():
        label = ans.get("text", "")
        value = ans.get("answer", "")
        if not value: continue
        vs = str(value)
        if not pdf_url:
            if isinstance(value, str) and value.startswith("http") and ".pdf" in value.lower():
                pdf_url = value
            elif "<a href" in vs and ".pdf" in vs.lower():
                m = re.search(r'href=["\']([^"\']+\.pdf[^"\']*)["\']', vs, re.I)
                if m: pdf_url = m.group(1)
            elif isinstance(value, list):
                for item in value:
                    m = re.search(r'href=["\']([^"\']+\.pdf[^"\']*)["\']', str(item), re.I)
                    if m: pdf_url = m.group(1); break
                    m2 = re.search(r'https?://\S+\.pdf\S*', str(item), re.I)
                    if m2: pdf_url = m2.group(0); break
        if label and isinstance(value, str) and "<a" not in value and len(value) < 300:
            info_lines.append("{}: {}".format(label, value))
    return pdf_url, "\n".join(info_lines)

INSTRUCTIONS = "\n".join([
    "You are a California DFPI-compliant payday loan underwriting analyst for Cash in Flash.",
    "",
    "OUTPUT THIS BLOCK FIRST - no text before it:",
    "DECISION_BLOCK_START",
    "APPLICANT_NAME: [Full name from document]",
    "DECISION: [APPROVED or DECLINED]",
    "APPROVED_AMOUNT: [dollar amount or N/A]",
    "DECLINE_REASON: [1-2 sentences if declined, or N/A]",
    "SCORE: [0-100]",
    "DECISION_BLOCK_END",
    "",
    "Then output the COMPLETE HTML report.",
    "Use h1 once, h2 per section, p per line, ul/li for lists, hr between sections, b for bold.",
    "",
    "<h1>CASH IN FLASH - DFPI UNDERWRITING REPORT</h1>",
    "<h2>Applicant Summary</h2><hr/>",
    "<h2>1 Statement Verification</h2><hr/>",
    "<h2>2 Income Analysis</h2><hr/>",
    "<h2>3 Expense and Cash-Flow Analysis</h2><hr/>",
    "<h2>4 DTI and Affordability</h2><hr/>",
    "<h2>5 Risk Flags and Compliance</h2><hr/>",
    "<h2>6 Final Decision</h2>",
    "",
    "LOAN LIMITS: $100 min, $255 max.",
    "VERIFIED INCOME: payroll, govt benefits, pension, consistent gig only.",
    "NOT income: P2P, internal transfers, refunds, loan proceeds, crypto, ATM, gambling.",
    "",
    "ACTIVE PROFILE: Standard",
    "FCF TIERS: T1=$100(FCF>=140) T2=$150(FCF>=210) T3=$200(FCF>=275) T4=$255(FCF>=350)",
    "NSF: 0-1=none | 2-3=drop 1 tier | 4=cap $100 | 5+=decline",
    "Fintech apps: 0-4=none | 5-6=drop 1 tier | 7-8=cap $100 | 9-10=decline | 11+=absolute decline",
    "Neg days: 0-6=none | 7-9=cap $100 | 10+=decline | avg below $0=decline",
    "Speculative: 0-24%=none | 25-29%=drop 1 tier | 30%+=cap $100",
    "AUTO-DECLINE: no income | closed account | fraud | FCF below $140 | 5+ NSFs | 9+ fintech apps | 10+ negative days | avg balance below $0",
    "NOT auto-decline: recent job loss | bankruptcy | statement older than 30 days",
    "",
    "Section 6 MUST show step-by-step: FCF amount, base tier, each adjustment, final tier, final decision.",
])

def call_claude(pdf_b64, applicant_info):
    text = INSTRUCTIONS
    if applicant_info:
        text = text + "\n\nAPPLICANT INFO:\n" + applicant_info

    payload = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 8000,
        "system": "You are an internal underwriting analyst for Cash in Flash. Complete all analyses fully.",
        "messages": [{"role": "user", "content": [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64}},
            {"type": "text", "text": text}
        ]}]
    }, ensure_ascii=True).encode("utf-8")

    ctx = ssl.create_default_context()
    conn = http.client.HTTPSConnection("api.anthropic.com", timeout=300, context=ctx)
    conn.request("POST", "/v1/messages", body=payload, headers={
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "Content-Length": str(len(payload))
    })
    resp = conn.getresponse()
    result = json.loads(resp.read().decode("utf-8"))
    conn.close()
    if "error" in result:
        raise Exception(str(result["error"]))
    return result["content"][0]["text"]

def parse_block(text):
    m = re.search(r"DECISION_BLOCK_START([\s\S]*?)DECISION_BLOCK_END", text)
    if not m:
        return {"name":"Unknown","decision":"PENDING","amount":"","reason":"","score":50}
    b = m.group(1)
    def g(pat): r=re.search(pat,b); return r.group(1).strip() if r else ""
    score_m = re.search(r"SCORE:\s*(\d+)", b)
    amt = g(r"APPROVED_AMOUNT:\s*(.+)")
    reason = g(r"DECLINE_REASON:\s*(.+)")
    return {
        "name":     g(r"APPLICANT_NAME:\s*(.+)") or "Unknown",
        "decision": (g(r"DECISION:\s*(.+)") or "PENDING").upper(),
        "amount":   "" if amt in ("N/A","") else amt.replace("$","").strip(),
        "reason":   "" if reason == "N/A" else reason,
        "score":    min(100,max(0,int(score_m.group(1)))) if score_m else 50
    }

def firebase_save(record):
    payload = json.dumps(record, ensure_ascii=True).encode("utf-8")
    ctx = ssl.create_default_context()
    conn = http.client.HTTPSConnection(FIREBASE_HOST, timeout=15, context=ctx)
    conn.request("POST", "/reports.json", body=payload, headers={
        "Content-Type": "application/json",
        "Content-Length": str(len(payload))
    })
    resp = conn.getresponse()
    result = json.loads(resp.read().decode("utf-8"))
    conn.close()
    return result.get("name")

def process(sub, source_tag="jotform"):
    sid = sub.get("id","?")
    print("[INFO] Submission {}...".format(sid))
    pdf_url, info = find_pdf(sub.get("answers", {}))
    if not pdf_url:
        print("[SKIP] No PDF found")
        return False
    print("[INFO] PDF: {}...".format(pdf_url[:60]))
    print("[INFO] Downloading...")
    b64 = download_pdf(pdf_url)
    if not b64:
        print("[ERROR] Download failed")
        return False
    print("[INFO] Analyzing with Claude...")
    try:
        raw = call_claude(b64, info)
    except Exception as e:
        print("[ERROR] Claude:", e)
        return False
    d = parse_block(raw)
    report = re.sub(r"DECISION_BLOCK_START[\s\S]*?DECISION_BLOCK_END\n?", "", raw).strip()
    now = datetime.now()
    record = {
        "id": int(time.time()*1000),
        "jotformSubmissionId": sid,
        "date": now.strftime("%b %d, %Y"),
        "time": now.strftime("%I:%M %p"),
        "createdAt": int(time.time()*1000),
        "source": source_tag,
        "status": "Pending",
        "name": d["name"],
        "amount": ("$"+d["amount"]) if d["amount"] else "N/A",
        "claudeDecision": d["decision"],
        "reason": d["reason"],
        "score": d["score"],
        "filename": "JotForm-{}.pdf".format(sid),
        "report": report,
        "notes": "",
        "profile": "Standard"
    }
    try:
        key = firebase_save(record)
        if key:
            print("[SUCCESS] {} -> {} -> Firebase {}".format(d["name"], d["decision"], key))
            return True
        print("[ERROR] Firebase returned no key")
        return False
    except Exception as e:
        print("[ERROR] Firebase:", e)
        return False

def main():
    print()
    print("  Cash in Flash - JotForm Auto-Processor v10")
    print("  Forms: {} (Loan App) + {} (Doc Upload)".format(JOTFORM_FORM_ID, JOTFORM_FORM_ID_2))
    print("  Interval: every {} sec".format(POLL_INTERVAL))
    print("  Keep this window open. Ctrl+C to stop.")
    print()
    state = load_state()

    # On first run (empty state), mark ALL existing submissions as seen so we skip old ones
    if not state.get("processed_ids"):
        print("[INIT] First run — scanning existing submissions to skip old ones...")
        done = set()
        for form_id in [JOTFORM_FORM_ID, JOTFORM_FORM_ID_2]:
            result = get_submissions(form_id)
            if result and result.get("responseCode") == 200:
                for sub in result.get("content", []):
                    done.add(sub.get("id"))
        state["processed_ids"] = list(done)
        save_state(state)
        print("[INIT] Marked {} existing submissions as seen — only NEW submissions will be processed going forward.".format(len(done)))
        print()

    while True:
        ts = datetime.now().strftime("%H:%M:%S")
        print("[{}] Checking JotForm...".format(ts))
        try:
            done = set(state.get("processed_ids",[]))
            total_new = 0

            for form_id, tag in [(JOTFORM_FORM_ID, "loan-app"), (JOTFORM_FORM_ID_2, "doc-upload")]:
                result = get_submissions(form_id)
                if result and result.get("responseCode") == 200:
                    subs = result.get("content", [])
                    for sub in reversed(subs):
                        sid = sub.get("id")
                        if sid in done: continue
                        ok = process(sub, source_tag=tag)
                        done.add(sid)
                        if ok: total_new += 1
                        print()
                else:
                    print("[ERROR] JotForm API for form {}: {}".format(form_id, result))

            state["processed_ids"] = list(done)[-500:]
            save_state(state)
            print("[{}] {} new submission(s) processed".format(ts, total_new) if total_new else "[{}] No new submissions".format(ts))
        except KeyboardInterrupt:
            print("\n  Stopped.")
            break
        except Exception as e:
            print("[ERROR]", e)
        print()
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()

import os
import json
import time
import threading
import requests
import re
from flask import Flask
from groq import Groq
import google.generativeai as genai
from dotenv import load_dotenv

# ---------------------------------------------------------
# 1. SETUP & CREDENTIALS
# ---------------------------------------------------------
load_dotenv()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GITHUB_USERNAME = os.getenv("GITHUB_USERNAME", "Ramanand-Shirbhate")

groq_client = Groq(api_key=GROQ_API_KEY)
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel('gemini-1.5-flash')

MY_SKILLS = "TypeScript, Node.js, React, and basic Python."
http = requests.Session()

# Global State Management
LAST_UPDATE_ID = None
PREVIOUS_BOUNTY_IDS = []  
BOUNTY_CACHE = {}         
CURRENT_MENU_ID = None
CURRENT_MENU_TEXT = ""

# ---------------------------------------------------------
# HELPER: SANITIZER & CHAT SWEEPER
# ---------------------------------------------------------
def escape_html(text):
    if not text: return "N/A"
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def clean_memory_cache():
    global BOUNTY_CACHE
    if len(BOUNTY_CACHE) > 100:
        keys = list(BOUNTY_CACHE.keys())
        for k in keys[:-20]: del BOUNTY_CACHE[k]

def sweep_chat(start_msg_id):
    print("🧹 Sweeping chat history...")
    # Clean the last 60 messages
    for i in range(start_msg_id, start_msg_id - 60, -1):
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteMessage"
        try: http.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "message_id": i}, timeout=1)
        except: pass

# ---------------------------------------------------------
# 2. GITHUB DATA FETCHERS
# ---------------------------------------------------------
def fetch_potential_bounties(limit=5):
    url = "https://api.github.com/search/issues"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    query = "is:issue is:open label:bounty no:assignee"
    params = {"q": query, "sort": "created", "order": "desc", "per_page": limit}
    
    try:
        response = http.get(url, headers=headers, params=params, timeout=10)
        if response.status_code != 200: return []
        data = response.json()
    except: return []
    
    bad_labels = ["reserved", "interview", "internal", "locked", "wip", "gitcoin", "drips"]
    good_bounties = []
    for issue in data.get("items", []):
        labels = [label["name"].lower() for label in issue["labels"]]
        if any(bad in l for l in labels for bad in bad_labels): continue 
        bounty = {
            "id": str(issue["id"]), 
            "title": issue["title"],
            "url": issue["html_url"],
            "api_comments_url": issue["comments_url"], 
            "body": str(issue["body"])[:2000]
        }
        good_bounties.append(bounty)
        BOUNTY_CACHE[bounty["id"]] = bounty 
    clean_memory_cache()
    return good_bounties

def fetch_single_issue(owner, repo, issue_num):
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_num}"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    try:
        res = http.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            issue = res.json()
            bounty = {"id": str(issue["id"]), "title": issue["title"], "url": issue["html_url"], "api_comments_url": issue["comments_url"], "body": str(issue["body"])[:2000]}
            BOUNTY_CACHE[bounty["id"]] = bounty
            return bounty
    except: pass
    return None

def fetch_issue_comments(comments_url):
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    try:
        response = http.get(comments_url, headers=headers, timeout=10)
        if response.status_code == 200:
            comments_data = response.json()
            if not comments_data: return "No comments yet."
            return "\n".join([f"{c['user']['login']}: {c['body']}" for c in comments_data])[:1500]
    except: pass
    return ""

# ---------------------------------------------------------
# 3. AI ENGINES
# ---------------------------------------------------------
def evaluate_bounty(title, body, comments_text):
    system_prompt = f"""
    Evaluate bounty for developer: {MY_SKILLS}.
    1. Must be USD ($). If RTC, crypto, or no price, set is_winnable: false.
    2. Set is_winnable: false if already claimed.
    Return JSON: {{"score": <1-10>, "is_winnable": <bool>, "reward": "<$>", "reason": "<str>"}}
    """
    try:
        completion = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": f"TITLE: {title}\n\nBODY:\n{body}\n\nCOMMENTS:\n{comments_text}"}],
            temperature=0.0, 
            response_format={"type": "json_object"}
        )
        return json.loads(completion.choices[0].message.content)
    except: return {"score": 0, "is_winnable": False, "reward": "Error", "reason": "AI Error"}

# ---------------------------------------------------------
# 4. TELEGRAM CORE
# ---------------------------------------------------------
def send_telegram_msg(text, silent=False):
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if silent: payload["disable_notification"] = True
    try:
        res = http.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json=payload, timeout=10).json()
        if res.get("ok"): return res["result"]["message_id"]
    except: pass
    return None

def edit_telegram_menu(status_text, show_buttons=True, keyboard=None):
    global CURRENT_MENU_ID, CURRENT_MENU_TEXT
    if not CURRENT_MENU_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
    full_text = CURRENT_MENU_TEXT + f"\n\n{status_text}"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "message_id": CURRENT_MENU_ID, "text": full_text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if keyboard: payload["reply_markup"] = {"inline_keyboard": keyboard}
    elif not show_buttons: payload["reply_markup"] = {"inline_keyboard": []}
    try: http.post(url, json=payload, timeout=10)
    except: pass

def send_new_menu(bounties_list, comparison_text, is_manual=False):
    global CURRENT_MENU_ID, CURRENT_MENU_TEXT
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    header = "🎯 <b>On-Demand Audit:</b>" if is_manual else "🚨 <b>Found Bounties!</b>"
    message = f"{header} <i>{escape_html(comparison_text)}</i>\n\n" + "➖"*15 + "\n\n"
    inline_keyboard = []
    for idx, b in enumerate(bounties_list, 1):
        message += f"<b>[{idx}] {escape_html(b['title'])}</b>\nScore: {b['score']}/10 | 💰 {escape_html(b['reward'])}\n<a href='{b['url']}'>🔗 View on GitHub</a>\n\n"
        inline_keyboard.append([{"text": f"✅ Claim Option {idx}", "callback_data": f"CLAIM_{b['id']}"}])
    inline_keyboard.append([{"text": "☢️ Deep Scan", "callback_data": "DEEP_SCAN"}, {"text": "⏭️ Skip", "callback_data": "SKIP"}])
    
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML", "reply_markup": {"inline_keyboard": inline_keyboard}, "disable_web_page_preview": True}
    try:
        res = http.post(url, json=payload, timeout=10).json()
        if res.get("ok"):
            CURRENT_MENU_ID, CURRENT_MENU_TEXT = res["result"]["message_id"], message
            return True
    except: pass
    return False

def poll_telegram_for_buttons(timeout_seconds):
    global LAST_UPDATE_ID
    start_time = time.time()
    while time.time() - start_time < timeout_seconds:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?timeout=3"
        if LAST_UPDATE_ID: url += f"&offset={LAST_UPDATE_ID + 1}"
        try:
            res = http.get(url, timeout=10).json()
            for update in res.get("result", []):
                LAST_UPDATE_ID = update["update_id"]
                if "callback_query" in update:
                    data = update["callback_query"]["data"]
                    http.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery?callback_query_id={update['callback_query']['id']}")
                    if data.startswith("CLAIM_"): return data.split("_")[1] 
                    return data
                elif "message" in update and "text" in update["message"]:
                    txt = update["message"]["text"].strip()
                    url_match = re.search(r"github\.com/([\w.-]+)/([\w.-]+)/issues/(\d+)", txt)
                    if url_match: return {"type": "LINK", "owner": url_match.group(1), "repo": url_match.group(2), "num": url_match.group(3)}
                    if "/START" in txt.upper():
                        send_telegram_msg("⚡ <b>System Resetting...</b> Deleting history and clearing cache.", silent=False)
                        threading.Thread(target=sweep_chat, args=(update["message"]["message_id"],)).start()
                        return "RESET"
                    if "/SCAN" in txt.upper(): return "SCAN"
        except: pass
        time.sleep(1)
    return "TIMEOUT"

# ---------------------------------------------------------
# 5. THE BOT LOOP
# ---------------------------------------------------------
def execute_claim_protocol(bounty_id):
    b = BOUNTY_CACHE.get(bounty_id)
    if not b: return
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    if GITHUB_USERNAME.lower() in fetch_issue_comments(b["api_comments_url"]).lower():
        edit_telegram_menu(f"⚠️ <i>Already claimed by {GITHUB_USERNAME}!</i>", show_buttons=False); return
    body = f"/attempt\n\nI'm interested in this bounty. I have experience in {MY_SKILLS}."
    if http.post(b["api_comments_url"], headers=headers, json={"body": body}).status_code == 201:
        edit_telegram_menu("✅ <b>Claimed!</b> Forking repo...", show_buttons=False)
        p = b["url"].split('/')
        http.post(f"https://api.github.com/repos/{p[3]}/{p[4]}/forks", headers=headers)
        edit_telegram_menu("🎯 <b>Success.</b> Environment ready.", show_buttons=False)

def run_bounty_hunter():
    global PREVIOUS_BOUNTY_IDS, CURRENT_MENU_ID
    print("🤖 V4.5 Agent Online. Always-On Polling active.")
    flush_telegram_updates()
    force_scan, deep_scan, manual_link = False, False, None

    while True:
        if manual_link:
            issue = fetch_single_issue(manual_link["owner"], manual_link["repo"], manual_link["num"])
            bounties = [issue] if issue else []
            manual_link = None
        else:
            bounties = fetch_potential_bounties(limit=10 if deep_scan else 5)
        
        display_bounties = []
        current_ids = []
        for b in bounties:
            v = evaluate_bounty(b["title"], b["body"], fetch_issue_comments(b["api_comments_url"]))
            if deep_scan or (not PREVIOUS_BOUNTY_IDS and b) or (v["score"] >= 7 and v["is_winnable"]):
                b.update(v); display_bounties.append(b); current_ids.append(b["id"])

        if display_bounties:
            is_dup = set(current_ids) == set(PREVIOUS_BOUNTY_IDS)
            if is_dup and not (force_scan or deep_scan):
                edit_telegram_menu(f"<i>Last silent refresh: {time.strftime('%H:%M')}</i>", keyboard=[[{"text": "🔍 Scan Now", "callback_data": "SCAN"}]])
            else:
                PREVIOUS_BOUNTY_IDS = current_ids
                send_new_menu(display_bounties, "Scan Finished", is_manual=(len(display_bounties)==1 and not deep_scan))
        
        force_scan, deep_scan = False, False
        # Phase 1: Wait for 5 minutes after a scan
        res = poll_telegram_for_buttons(timeout_seconds=300)
        
        if isinstance(res, dict) and res.get("type") == "LINK": manual_link = res; continue
        if res == "RESET": PREVIOUS_BOUNTY_IDS, CURRENT_MENU_ID = [], None; force_scan = True; continue
        elif res == "TIMEOUT": edit_telegram_menu("<i>🛑 Status: Auto-skipped.</i>", show_buttons=False)
        elif res == "SKIP": 
            edit_telegram_menu("<i>⏭️ Status: Skipped. Entering 15m sleep (Interruptible).</i>", keyboard=[[{"text": "🔍 Scan Now", "callback_data": "SCAN"}]])
            res = poll_telegram_for_buttons(timeout_seconds=900) # Wait 15 mins but stay responsive
            if res == "SCAN": force_scan = True
            elif res == "RESET": PREVIOUS_BOUNTY_IDS, CURRENT_MENU_ID = [], None; force_scan = True
            continue
        elif res == "SCAN": force_scan = True; continue
        elif res == "DEEP_SCAN": deep_scan = True; continue
        else: execute_claim_protocol(res)

        # Phase 2: Normal Idle (10 mins)
        edit_telegram_menu(f"<i>💤 Next scan at {time.strftime('%H:%M', time.localtime(time.time()+600))}</i>", keyboard=[[{"text": "🔍 Scan Now", "callback_data": "SCAN"}]])
        idle = poll_telegram_for_buttons(timeout_seconds=600)
        if isinstance(idle, dict) and idle.get("type") == "LINK": manual_link = idle
        elif idle == "SCAN": force_scan = True
        elif idle == "DEEP_SCAN": deep_scan = True
        elif idle == "RESET": PREVIOUS_BOUNTY_IDS, CURRENT_MENU_ID = [], None; force_scan = True

# ---------------------------------------------------------
# 6. WEB SERVER
# ---------------------------------------------------------
app = Flask(__name__)
@app.route('/')
def home(): return "🤖 Agent V4.5 Online"

if __name__ == "__main__":
    threading.Thread(target=run_bounty_hunter, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))

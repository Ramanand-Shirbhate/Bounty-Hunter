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

def sweep_chat(start_msg_id):
    print("🧹 Sweeping chat history...")
    for i in range(start_msg_id, start_msg_id - 50, -1):
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteMessage"
        http.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "message_id": i})

# ---------------------------------------------------------
# 2. GITHUB DATA FETCHERS
# ---------------------------------------------------------
def fetch_potential_bounties(limit=5):
    url = "https://api.github.com/search/issues"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    query = "is:issue is:open label:bounty no:assignee"
    params = {"q": query, "sort": "created", "order": "desc", "per_page": limit}
    
    response = http.get(url, headers=headers, params=params)
    if response.status_code != 200: return []
    data = response.json()
    
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
    return good_bounties

def fetch_single_issue(owner, repo, issue_num):
    """Fetches a specific issue for the On-Demand link scanner."""
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_num}"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    res = http.get(url, headers=headers)
    if res.status_code == 200:
        issue = res.json()
        bounty = {
            "id": str(issue["id"]), 
            "title": issue["title"], 
            "url": issue["html_url"], 
            "api_comments_url": issue["comments_url"], 
            "body": str(issue["body"])[:2000]
        }
        BOUNTY_CACHE[bounty["id"]] = bounty
        return bounty
    return None

def fetch_issue_comments(comments_url):
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    response = http.get(comments_url, headers=headers)
    if response.status_code == 200:
        comments_data = response.json()
        if not comments_data: return "No comments yet."
        return "\n".join([f"{c['user']['login']}: {c['body']}" for c in comments_data])[:1500]
    return ""

def fork_repository(html_url):
    try:
        parts = html_url.split('/')
        owner, repo = parts[3], parts[4]
        url = f"https://api.github.com/repos/{owner}/{repo}/forks"
        headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        response = http.post(url, headers=headers)
        if response.status_code in [202, 201]: return True, repo
        return False, "API blocked fork."
    except Exception as e: return False, str(e)

# ---------------------------------------------------------
# 3. AI ENGINES
# ---------------------------------------------------------
def evaluate_bounty(title, body, comments_text):
    system_prompt = f"""
    You are an expert senior developer evaluating open-source bounties. My tech stack is: {MY_SKILLS}.
    Reward MUST be in USD ($). If reward is RTC or tokens, set is_winnable to false.
    Return ONLY JSON: {{"score": <int>, "is_winnable": <bool>, "reward": "<$>", "reason": "<str>"}}
    """
    try:
        completion = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": f"TITLE: {title}\n\nBODY:\n{body}\n\nCOMMENTS:\n{comments_text}"}],
            temperature=0.0, 
            response_format={"type": "json_object"}
        )
        return json.loads(completion.choices[0].message.content)
    except: return {"score": 0, "is_winnable": False, "reward": "Error", "reason": "API Fail"}

# ---------------------------------------------------------
# 4. TELEGRAM MORPHING ENGINE
# ---------------------------------------------------------
def update_existing_menu(status_text, show_buttons=True, keyboard=None):
    global CURRENT_MENU_ID, CURRENT_MENU_TEXT
    if not CURRENT_MENU_ID: return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
    full_text = CURRENT_MENU_TEXT + f"\n\n{status_text}"
    
    payload = {
        "chat_id": TELEGRAM_CHAT_ID, 
        "message_id": CURRENT_MENU_ID, 
        "text": full_text, 
        "parse_mode": "HTML", 
        "disable_web_page_preview": True
    }
    if keyboard: payload["reply_markup"] = {"inline_keyboard": keyboard}
    elif not show_buttons: payload["reply_markup"] = {"inline_keyboard": []}
    
    http.post(url, json=payload)

def send_new_menu(bounties_list, comparison_text, show_deep=True, is_manual=False):
    global CURRENT_MENU_ID, CURRENT_MENU_TEXT
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    header = "🎯 <b>On-Demand Audit:</b>" if is_manual else "🚨 <b>Found Bounties!</b>"
    message = f"{header}\n\n🤖 <b>Verdict:</b> <i>{escape_html(comparison_text)}</i>\n\n" + "➖"*15 + "\n\n"
    inline_keyboard = []
    
    for idx, b in enumerate(bounties_list, 1):
        message += f"<b>[{idx}] {escape_html(b['title'])}</b>\nScore: {b['score']}/10 | 💰 {escape_html(b['reward'])}\n"
        if is_manual or show_deep: message += f"<i>Reason: {escape_html(b.get('reason', 'N/A'))}</i>\n"
        message += f"<a href='{b['url']}'>🔗 View</a>\n\n"
        inline_keyboard.append([{"text": f"✅ Claim Option {idx}", "callback_data": f"CLAIM_{b['id']}"}])
    
    if show_deep: inline_keyboard.append([{"text": "☢️ Deep Scan", "callback_data": "DEEP_SCAN"}])
    inline_keyboard.append([{"text": "⏭️ Skip All", "callback_data": "SKIP"}])
    
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML", "reply_markup": {"inline_keyboard": inline_keyboard}, "disable_web_page_preview": True}
    
    res = http.post(url, json=payload).json()
    if res.get("ok"):
        CURRENT_MENU_ID = res["result"]["message_id"]
        CURRENT_MENU_TEXT = message
        return True
    return False

def poll_telegram_for_buttons(timeout_seconds):
    global LAST_UPDATE_ID
    start_time = time.time()
    while time.time() - start_time < timeout_seconds:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?timeout=3"
        if LAST_UPDATE_ID: url += f"&offset={LAST_UPDATE_ID + 1}"
        try:
            res = http.get(url).json()
            for update in res.get("result", []):
                LAST_UPDATE_ID = update["update_id"]
                if "callback_query" in update:
                    data = update["callback_query"]["data"]
                    http.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery?callback_query_id={update['callback_query']['id']}")
                    if data.startswith("CLAIM_"): return data.split("_")[1] 
                    return data
                elif "message" in update and "text" in update["message"]:
                    txt = update["message"]["text"].strip()
                    
                    # LINK DETECTOR REGEX
                    github_match = re.search(r"github\.com/([\w.-]+)/([\w.-]+)/issues/(\d+)", txt)
                    if github_match:
                        return {"type": "LINK", "owner": github_match.group(1), "repo": github_match.group(2), "num": github_match.group(3)}
                    
                    txt_upper = txt.upper()
                    if "/START" in txt_upper:
                        threading.Thread(target=sweep_chat, args=(update["message"]["message_id"],)).start()
                        return "RESET"
                    if "/SCAN" in txt_upper: return "SCAN"
        except: pass
        time.sleep(2)
    return "TIMEOUT"

# ---------------------------------------------------------
# 5. EXECUTION & LOOP
# ---------------------------------------------------------
def execute_claim_protocol(bounty_id):
    bounty = BOUNTY_CACHE.get(bounty_id)
    if not bounty: return
    
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    comm = fetch_issue_comments(bounty["api_comments_url"])
    if GITHUB_USERNAME.lower() in comm.lower():
        update_existing_menu(f"⚠️ <i>Already claimed by {GITHUB_USERNAME}!</i>", show_buttons=False)
        return
    
    claim_text = f"/attempt\n\nI'm interested in this bounty. I have experience in {MY_SKILLS} and will start immediately."
    res = http.post(bounty["api_comments_url"], headers=headers, json={"body": claim_text})
    
    if res.status_code == 201:
        update_existing_menu(f"✅ <b>Claimed!</b> Attempting fork...", show_buttons=False)
        parts = bounty["url"].split('/')
        http.post(f"https://api.github.com/repos/{parts[3]}/{parts[4]}/forks", headers=headers)
        update_existing_menu(f"🎯 <b>Protocol Complete.</b> Check your GitHub forks.", show_buttons=False)

def run_bounty_hunter():
    global PREVIOUS_BOUNTY_IDS, CURRENT_MENU_ID
    flush_telegram_updates()
    force_scan = False
    deep_scan = False
    manual_link = None

    while True:
        is_manual_scan = False
        
        if manual_link:
            b = fetch_single_issue(manual_link["owner"], manual_link["repo"], manual_link["num"])
            bounties = [b] if b else []
            manual_link = None
            is_manual_scan = True
        else:
            bounties = fetch_potential_bounties(limit=10 if deep_scan else 5)

        display_bounties = []
        current_ids = []

        for b in bounties:
            v = evaluate_bounty(b["title"], b["body"], fetch_issue_comments(b["api_comments_url"]))
            # Force show if deep scan OR it's a manual link audit OR it passes normal filters
            if deep_scan or is_manual_scan or (v["score"] >= 7 and v["is_winnable"]):
                b.update(v)
                display_bounties.append(b)
                current_ids.append(b["id"])

        if display_bounties:
            is_dup = set(current_ids) == set(PREVIOUS_BOUNTY_IDS)
            
            if is_dup and not (force_scan or deep_scan or is_manual_scan):
                update_existing_menu(f"<i>Last silent refresh: {time.strftime('%H:%M')} (No new bounties)</i>", 
                                     keyboard=[[{"text": "🔍 Scan Now", "callback_data": "SCAN"}]])
            else:
                if not (deep_scan or is_manual_scan):
                    PREVIOUS_BOUNTY_IDS = current_ids
                    
                comp_text = "Analysis complete." if is_manual_scan else ("Deep Scan Results" if deep_scan else "New Bounties Found")
                send_new_menu(display_bounties, comp_text, show_deep=not deep_scan, is_manual=is_manual_scan)
        else:
            if is_manual_scan:
                update_existing_menu("<i>❌ Error: Could not fetch data from the provided GitHub link.</i>", show_buttons=True, keyboard=[[{"text": "🔍 Scan Now", "callback_data": "SCAN"}]])
        
        force_scan = False
        deep_scan = False

        # Decision Phase
        res = poll_telegram_for_buttons(timeout_seconds=300)
        
        # Check if a link was pasted during the decision phase
        if isinstance(res, dict) and res.get("type") == "LINK":
            manual_link = res
            continue
            
        if res == "TIMEOUT":
            update_existing_menu("<i>🛑 Status: Auto-skipped.</i>", show_buttons=False)
        elif res == "SKIP":
            update_existing_menu("<i>⏭️ Status: Manual skip.</i>", show_buttons=False)
            time.sleep(900); continue
        elif res == "SCAN":
            force_scan = True; continue
        elif res == "DEEP_SCAN":
            deep_scan = True; continue
        elif res == "RESET":
            PREVIOUS_BOUNTY_IDS = []; CURRENT_MENU_ID = None; force_scan = True; continue
        else:
            execute_claim_protocol(res)

        # Idle Phase
        update_existing_menu(f"<i>💤 Sleeping... Next scan at {time.strftime('%H:%M', time.localtime(time.time()+600))}</i>", 
                             keyboard=[[{"text": "🔍 Scan Now", "callback_data": "SCAN"}]])
        idle_res = poll_telegram_for_buttons(timeout_seconds=600)
        
        # Check if a link was pasted during the idle phase
        if isinstance(idle_res, dict) and idle_res.get("type") == "LINK":
            manual_link = idle_res
        elif idle_res == "SCAN": force_scan = True
        elif idle_res == "DEEP_SCAN": deep_scan = True
        elif idle_res == "RESET": PREVIOUS_BOUNTY_IDS = []; CURRENT_MENU_ID = None; force_scan = True

# ---------------------------------------------------------
# 7. WEB SERVER
# ---------------------------------------------------------
app = Flask(__name__)
@app.route('/')
def home(): return "🤖 Agent V4.3 Online"

if __name__ == "__main__":
    threading.Thread(target=run_bounty_hunter, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))

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

# Connection pooling with strict retries
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
    for i in range(start_msg_id, start_msg_id - 60, -1):
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteMessage"
        try: http.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "message_id": i}, timeout=5)
        except: pass

# ---------------------------------------------------------
# 2. GITHUB DATA FETCHERS (WITH ANTI-FREEZE TIMEOUTS)
# ---------------------------------------------------------
def fetch_potential_bounties(limit=5):
    url = "https://api.github.com/search/issues"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    query = "is:issue is:open label:bounty no:assignee"
    params = {"q": query, "sort": "created", "order": "desc", "per_page": limit}
    
    try:
        response = http.get(url, headers=headers, params=params, timeout=15)
        if response.status_code != 200: return []
        data = response.json()
    except Exception as e:
        print(f"⚠️ GitHub Fetch Error: {e}")
        return []
    
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
    """Fetches a specific issue for the On-Demand link scanner."""
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_num}"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    try:
        res = http.get(url, headers=headers, timeout=15)
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
        response = http.get(comments_url, headers=headers, timeout=15)
        if response.status_code == 200:
            comments_data = response.json()
            if not comments_data: return "No comments yet."
            return "\n".join([f"{c['user']['login']}: {c['body']}" for c in comments_data])[:1500]
    except: pass
    return ""

def fork_repository(html_url):
    try:
        parts = html_url.split('/')
        owner, repo = parts[3], parts[4]
        url = f"https://api.github.com/repos/{owner}/{repo}/forks"
        headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        response = http.post(url, headers=headers, timeout=15)
        if response.status_code in [202, 201]: return True, repo
        return False, "API blocked fork."
    except Exception as e: return False, str(e)

# ---------------------------------------------------------
# 3. AI ENGINES
# ---------------------------------------------------------
def evaluate_bounty(title, body, comments_text):
    system_prompt = f"""
    You evaluate bounties for a developer with: {MY_SKILLS}.
    1. Check for USD/Fiat ($). If RTC, crypto tokens, or no price, set is_winnable: false.
    2. Check if claimed. If so, set is_winnable: false.
    Return JSON: {{"score": <int 1-10>, "is_winnable": <bool>, "reward": "<$>", "reason": "<str>"}}
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

def generate_advanced_claim(title, body, comments):
    """Gemini AI: Generates a contextual, intelligent claim comment."""
    prompt = f"""
    I want to claim this GitHub bounty. Write a highly professional comment to post.
    Start with exactly "/attempt".
    Read the issue body, infer which files/modules need to be edited, and state that I will begin auditing/working on those specific modules. 
    Keep it concise, confident, and under 4 sentences.
    
    Title: {title}
    Body: {body}
    Existing Comments: {comments}
    """
    try:
        response = gemini_model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        print(f"Gemini Error: {e}")
        return "/attempt\n\nHi there! I have strong experience with this stack and would love to take this on. I will begin setting up my environment and auditing the required modules immediately."

# ---------------------------------------------------------
# 4. TELEGRAM MORPHING & COMMANDS
# ---------------------------------------------------------
def flush_telegram_updates():
    global LAST_UPDATE_ID
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        print("🚽 Flushing old Telegram messages to clear cache...")
        res = http.get(url, timeout=10).json()
        if res.get("result"): LAST_UPDATE_ID = res["result"][-1]["update_id"]
    except Exception as e:
        print(f"⚠️ Flush Error: {e}")

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
        res = http.post(url, json=payload, timeout=15).json()
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
                    http.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery?callback_query_id={update['callback_query']['id']}", timeout=5)
                    if data.startswith("CLAIM_"): return data.split("_")[1] 
                    return data
                elif "message" in update and "text" in update["message"]:
                    txt = update["message"]["text"].strip()
                    url_match = re.search(r"github\.com/([\w.-]+)/([\w.-]+)/issues/(\d+)", txt)
                    if url_match: return {"type": "LINK", "owner": url_match.group(1), "repo": url_match.group(2), "num": url_match.group(3)}
                    if "/START" in txt.upper():
                        threading.Thread(target=sweep_chat, args=(update["message"]["message_id"],)).start()
                        return "RESET"
                    if "/SCAN" in txt.upper(): return "SCAN"
        except Exception as e: pass
        time.sleep(2)
    return "TIMEOUT"

# ---------------------------------------------------------
# 5. EXECUTION & BOT LOOP
# ---------------------------------------------------------
def execute_claim_protocol(bounty_id):
    bounty = BOUNTY_CACHE.get(bounty_id)
    if not bounty: return
    
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    comments_text = fetch_issue_comments(bounty["api_comments_url"])
    
    # Pre-Flight Safety Check
    if GITHUB_USERNAME.lower() in comments_text.lower():
        edit_telegram_menu(f"⚠️ <i>Already claimed by {GITHUB_USERNAME}!</i>", show_buttons=False)
        return
        
    edit_telegram_menu("<i>🧠 Gemini is drafting the claim...</i>", show_buttons=False)
    
    # Restored Gemini Comment Drafter
    smart_comment = generate_advanced_claim(bounty["title"], bounty["body"], comments_text)
    
    try:
        response = http.post(bounty["api_comments_url"], headers=headers, json={"body": smart_comment}, timeout=15)
        if response.status_code == 201:
            edit_telegram_menu("✅ <b>Claimed!</b> Forking repo...", show_buttons=False)
            fork_success, repo_name = fork_repository(bounty["url"])
            if fork_success:
                edit_telegram_menu(f"🎯 <b>CLAIM COMPLETE</b>\n1. Issue Claimed.\n2. Repo Forked ({repo_name}).\nEnvironment ready.", show_buttons=False)
            else:
                edit_telegram_menu(f"⚠️ Claimed successfully, but Auto-Fork failed. Please fork manually.", show_buttons=False)
        else:
            edit_telegram_menu(f"❌ Error posting to GitHub: {response.text}", show_buttons=False)
    except Exception as e:
        edit_telegram_menu(f"❌ API Timeout during claim execution.", show_buttons=False)

def run_bounty_hunter():
    global PREVIOUS_BOUNTY_IDS, CURRENT_MENU_ID
    flush_telegram_updates()
    force_scan, deep_scan, manual_link = False, False, None

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
            if deep_scan or is_manual_scan or (v["score"] >= 7 and v["is_winnable"]):
                b.update(v); display_bounties.append(b); current_ids.append(b["id"])

        if display_bounties:
            is_dup = set(current_ids) == set(PREVIOUS_BOUNTY_IDS)
            if is_dup and not (force_scan or deep_scan or is_manual_scan):
                edit_telegram_menu(f"<i>Last silent refresh: {time.strftime('%H:%M')} (No new bounties)</i>", keyboard=[[{"text": "🔍 Scan Now", "callback_data": "SCAN"}]])
            else:
                if not (deep_scan or is_manual_scan): PREVIOUS_BOUNTY_IDS = current_ids
                comp_text = "Analysis complete." if is_manual_scan else ("Deep Scan Results" if deep_scan else "New Bounties Found")
                send_new_menu(display_bounties, comp_text, is_manual=is_manual_scan)
        else:
            if is_manual_scan: edit_telegram_menu("<i>❌ Error: Could not fetch data from link.</i>", show_buttons=True, keyboard=[[{"text": "🔍 Scan Now", "callback_data": "SCAN"}]])
        
        force_scan, deep_scan = False, False
        res = poll_telegram_for_buttons(timeout_seconds=300)
        
        if isinstance(res, dict) and res.get("type") == "LINK": manual_link = res; continue
        if res == "TIMEOUT": edit_telegram_menu("<i>🛑 Status: Auto-skipped.</i>", show_buttons=False)
        elif res == "SKIP": edit_telegram_menu("<i>⏭️ Status: Manual skip.</i>", show_buttons=False); time.sleep(900); continue
        elif res == "SCAN": force_scan = True; continue
        elif res == "DEEP_SCAN": deep_scan = True; continue
        elif res == "RESET": PREVIOUS_BOUNTY_IDS, CURRENT_MENU_ID = [], None; force_scan = True; continue
        else: execute_claim_protocol(res)

        edit_telegram_menu(f"<i>💤 Sleeping... Next scan at {time.strftime('%H:%M', time.localtime(time.time()+600))}</i>", keyboard=[[{"text": "🔍 Scan Now", "callback_data": "SCAN"}]])
        idle = poll_telegram_for_buttons(timeout_seconds=600)
        if isinstance(idle, dict) and idle.get("type") == "LINK": manual_link = idle
        elif idle == "SCAN": force_scan = True
        elif idle == "DEEP_SCAN": deep_scan = True
        elif idle == "RESET": PREVIOUS_BOUNTY_IDS, CURRENT_MENU_ID = [], None; force_scan = True

# ---------------------------------------------------------
# 7. WEB SERVER
# ---------------------------------------------------------
app = Flask(__name__)
@app.route('/')
def home(): return "🤖 Agent V4.5 Online - Full Features Restored"

if __name__ == "__main__":
    threading.Thread(target=run_bounty_hunter, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))

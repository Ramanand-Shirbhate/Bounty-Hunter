import os
import json
import time
import threading
import requests
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

groq_client = Groq(api_key=GROQ_API_KEY)
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel('gemini-1.5-flash')

MY_SKILLS = "TypeScript, Node.js, React, and basic Python."

# Global State Management
LAST_UPDATE_ID = None
PREVIOUS_BOUNTY_IDS = []  
BOUNTY_CACHE = {}         

# ---------------------------------------------------------
# HELPER: HTML SANITIZER
# ---------------------------------------------------------
def escape_html(text):
    if not text: return "N/A"
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

# ---------------------------------------------------------
# 2. GITHUB DATA FETCHERS
# ---------------------------------------------------------
def fetch_potential_bounties(limit=5):
    url = "https://api.github.com/search/issues"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    query = "is:issue is:open label:bounty no:assignee"
    params = {"q": query, "sort": "created", "order": "desc", "per_page": limit}
    
    print(f"\n🔍 Scouting GitHub for {limit} fresh bounties...")
    response = requests.get(url, headers=headers, params=params)
    if response.status_code != 200:
        return []
        
    data = response.json()
    bad_labels = ["reserved", "interview", "internal", "locked", "wip", "gitcoin", "drips"]
    good_bounties = []
    
    for issue in data.get("items", []):
        labels = [label["name"].lower() for label in issue["labels"]]
        if any(bad in l for l in labels for bad in bad_labels):
            continue 
            
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

def fetch_issue_comments(comments_url):
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    response = requests.get(comments_url, headers=headers)
    if response.status_code == 200:
        comments_data = response.json()
        if not comments_data: return "No comments yet."
        chat_log = "\n".join([f"{c['user']['login']}: {c['body']}" for c in comments_data])
        return chat_log[:1500] 
    return "Could not fetch comments."

def fork_repository(html_url):
    try:
        parts = html_url.split('/')
        owner, repo = parts[3], parts[4]
        url = f"https://api.github.com/repos/{owner}/{repo}/forks"
        headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        response = requests.post(url, headers=headers)
        if response.status_code in [202, 201]: return True, repo
        return False, "API blocked fork."
    except Exception as e: return False, str(e)

# ---------------------------------------------------------
# 3. AI ENGINES
# ---------------------------------------------------------
def evaluate_bounty(title, body, comments_text):
    system_prompt = f"""
    You are an expert senior developer evaluating open-source bounties. My tech stack is: {MY_SKILLS}.
    1. Read the issue and comments. If claimed, set is_winnable to false.
    2. STRICT FIAT MONEY FILTER: The reward MUST be in real USD (e.g., $100, $50). 
       If the reward mentions 'RTC', altcoins, tokens, or lacks a literal '$' sign, YOU MUST SET is_winnable TO false.
    
    Return ONLY a valid JSON object:
    {{"score": <int 1-10>, "is_winnable": <bool>, "reward": "<Extract the $ amount>", "reason": "<Short sentence>"}}
    """
    try:
        completion = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": f"TITLE: {title}\n\nBODY:\n{body}\n\nCOMMENTS:\n{comments_text}"}],
            temperature=0.0, 
            response_format={"type": "json_object"}
        )
        return json.loads(completion.choices[0].message.content)
    except:
        return {"score": 0, "is_winnable": False, "reward": "Unknown", "reason": "API Error"}

def generate_advanced_claim(title, body, comments):
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
        return "/attempt\n\nHi there! I have strong experience with this stack and would love to take this on. I will begin setting up my environment and auditing the required modules immediately."

# ---------------------------------------------------------
# 4. TELEGRAM COMMUNICATION 
# ---------------------------------------------------------
def flush_telegram_updates():
    global LAST_UPDATE_ID
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        res = requests.get(url).json()
        if res.get("result"): LAST_UPDATE_ID = res["result"][-1]["update_id"]
    except: pass

def send_telegram_menu(bounties_list, comparison_text, show_deep_scan=True):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    safe_comp_text = escape_html(comparison_text)
    message = f"🚨 <b>Found Bounties!</b>\n\n🤖 <b>Analyst Verdict:</b>\n<i>{safe_comp_text}</i>\n\n" + "➖"*15 + "\n\n"
    
    inline_keyboard = []
    for idx, b in enumerate(bounties_list, 1):
        safe_title = escape_html(b['title'])
        safe_reward = escape_html(b.get('reward', 'Unknown'))
        safe_reason = escape_html(b.get('reason', 'N/A'))
        
        message += f"<b>[{idx}] {safe_title}</b>\nScore: {b['score']}/10 | 💰 {safe_reward}\n"
        message += f"<i>Reason: {safe_reason}</i>\n" 
        message += f"<a href='{b['url']}'>🔗 View on GitHub</a>\n\n"
        
        inline_keyboard.append([{"text": f"✅ Claim Option {idx}", "callback_data": f"CLAIM_{b['id']}"}])
        
    if show_deep_scan:
        inline_keyboard.append([{"text": "☢️ Deep Scan (Show All)", "callback_data": "DEEP_SCAN"}])
        
    inline_keyboard.append([{"text": "⏭️ Skip All", "callback_data": "SKIP"}])
    
    payload = {
        "chat_id": TELEGRAM_CHAT_ID, 
        "text": message, 
        "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": inline_keyboard},
        "disable_web_page_preview": True # <-- CRITICAL FIX: Removes the massive GitHub box to fix Telegram UI glitch
    }
    
    response = requests.post(url, json=payload)
    if response.status_code != 200:
        send_telegram_msg("❌ Failed to display menu due to unsupported characters.", silent=False)
        return False 
    return True

def send_telegram_idle_menu(sleep_time_mins):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID, 
        "text": f"<i>💤 Sleeping for {sleep_time_mins} minutes...</i>", 
        "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": [[{"text": "🔍 Scan GitHub Now", "callback_data": "SCAN"}]]},
        "disable_notification": True 
    }
    requests.post(url, json=payload)

def send_telegram_msg(text, silent=False):
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if silent: payload["disable_notification"] = True
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json=payload)

def poll_telegram_for_buttons(timeout_seconds):
    global LAST_UPDATE_ID
    start_time = time.time()
    
    while time.time() - start_time < timeout_seconds:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?timeout=3"
        if LAST_UPDATE_ID: url += f"&offset={LAST_UPDATE_ID + 1}"
            
        try:
            res = requests.get(url).json()
            for update in res.get("result", []):
                LAST_UPDATE_ID = update["update_id"]
                
                if "callback_query" in update:
                    cb_id = update["callback_query"]["id"]
                    data = update["callback_query"]["data"]
                    requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery?callback_query_id={cb_id}")
                    
                    if data.startswith("CLAIM_"): return data.split("_")[1] 
                    elif data in ["SKIP", "SCAN", "DEEP_SCAN"]: return data
                    
                elif "message" in update and "text" in update["message"]:
                    text = update["message"]["text"].strip().upper()
                    if text == "/START": return "RESET"
                    elif text == "/SCAN": return "SCAN"
        except Exception: pass
        time.sleep(2)
    return "TIMEOUT"

# ---------------------------------------------------------
# 5. AUTO-COMMENTER 
# ---------------------------------------------------------
def execute_claim_protocol(bounty_id):
    if bounty_id not in BOUNTY_CACHE:
        send_telegram_msg("❌ Error: Bounty expired from cache. Cannot claim.", silent=False)
        return

    bounty = BOUNTY_CACHE[bounty_id]
    send_telegram_msg("<i>⏳ Claim sequence initiated...</i>", silent=True)
    send_telegram_msg("<i>🧠 Gemini is reading the repo and drafting the claim...</i>", silent=True)
    
    comments_text = fetch_issue_comments(bounty["api_comments_url"])
    smart_comment = generate_advanced_claim(bounty["title"], bounty["body"], comments_text)
    
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    response = requests.post(bounty["api_comments_url"], headers=headers, json={"body": smart_comment})
    
    if response.status_code == 201:
        send_telegram_msg("<i>✅ `/attempt` posted successfully!\n\n🤖 Cloning repository to your account...</i>", silent=True)
        fork_success, repo_name = fork_repository(bounty["url"])
        if fork_success:
            send_telegram_msg(f"🎯 <b>CLAIM COMPLETE</b>\n\n1. Issue Claimed.\n2. Repo Forked ({repo_name}).\n\nYou are clear to pull and branch.", silent=False)
        else:
            send_telegram_msg(f"⚠️ Claimed successfully, but Auto-Fork failed. Please fork manually.", silent=False)
    else:
        send_telegram_msg(f"❌ Error posting to GitHub: {response.text}", silent=False)

# ---------------------------------------------------------
# 6. THE BOT LOOP
# ---------------------------------------------------------
def run_bounty_hunter():
    global PREVIOUS_BOUNTY_IDS, BOUNTY_CACHE
    print("🤖 V3.7 Agent Online. Link Previews Disabled for UI Stability.")
    flush_telegram_updates() 
    
    force_scan = False
    deep_scan = False
    
    while True:
        fetch_limit = 10 if deep_scan else 5
        bounties = fetch_potential_bounties(limit=fetch_limit)
        
        display_bounties = []
        current_ids = []
        
        if bounties:
            for b in bounties:
                comments_text = fetch_issue_comments(b["api_comments_url"])
                verdict = evaluate_bounty(b["title"], b["body"], comments_text)
                
                b["score"] = verdict.get("score", 0)
                b["reward"] = verdict.get("reward", "Unknown")
                b["reason"] = verdict.get("reason", "N/A")
                
                if deep_scan or (b["score"] >= 7 and verdict.get("is_winnable")):
                    display_bounties.append(b)
                    current_ids.append(b["id"])
                    
        if display_bounties:
            if not force_scan and not deep_scan and set(current_ids) == set(PREVIOUS_BOUNTY_IDS):
                print("💤 Duplicate bounties found.")
                bounties_found = False 
                if force_scan: 
                    send_telegram_msg("<i>📉 Scan complete: No NEW bounties found since last check.</i>", silent=True)
            else:
                if not deep_scan: PREVIOUS_BOUNTY_IDS = current_ids
                comp_text = "☢️ Deep Scan Active: Showing all unfiltered results." if deep_scan else "Only high-match bounties shown."
                menu_sent = send_telegram_menu(display_bounties, comparison_text=comp_text, show_deep_scan=not deep_scan) 
                bounties_found = menu_sent
        else:
            bounties_found = False
            if not deep_scan: PREVIOUS_BOUNTY_IDS = []
            if force_scan or deep_scan:
                send_telegram_msg("<i>📉 Scan complete: No bounties passed the USD & Score filters right now.</i>", silent=True)
                
        force_scan = False
        deep_scan = False
        
        # --- DECISION PHASE ---
        if bounties_found:
            user_choice = poll_telegram_for_buttons(timeout_seconds=300)
            
            if user_choice == "TIMEOUT":
                send_telegram_msg("<i>⏭️ 5 mins passed. Auto-skipping.</i>", silent=True)
                sleep_mins = 10
            elif user_choice == "SKIP":
                send_telegram_msg("<i>⏭️ Manual Skip. Entering deep sleep.</i>", silent=True)
                sleep_mins = 15
            elif user_choice == "SCAN":
                send_telegram_msg("<i>🔍 Scanning GitHub and evaluating bounties... Please wait.</i>", silent=True)
                force_scan = True
                continue
            elif user_choice == "DEEP_SCAN":
                send_telegram_msg("<i>☢️ Deep Scan initialized! Evaluating 10 issues...</i>", silent=True)
                deep_scan = True
                continue
            elif user_choice == "RESET":
                PREVIOUS_BOUNTY_IDS = []
                BOUNTY_CACHE.clear()
                send_telegram_msg("<i>🔄 System Reset Triggered via /start. Cache cleared! Scanning now...</i>", silent=True)
                force_scan = True
                continue
            else:
                execute_claim_protocol(user_choice)
                time.sleep(5) 
                sleep_mins = 10 
        else:
            sleep_mins = 10

        # --- ACTIVE IDLE PHASE ---
        send_telegram_idle_menu(sleep_mins)
        idle_choice = poll_telegram_for_buttons(timeout_seconds=(sleep_mins * 60))
        
        if idle_choice == "SCAN":
            send_telegram_msg("<i>🔍 Scanning GitHub and evaluating bounties... Please wait.</i>", silent=True)
            force_scan = True
        elif idle_choice == "DEEP_SCAN":
            send_telegram_msg("<i>☢️ Deep Scan initialized! Evaluating 10 issues...</i>", silent=True)
            deep_scan = True
        elif idle_choice == "RESET":
            PREVIOUS_BOUNTY_IDS = []
            BOUNTY_CACHE.clear()
            send_telegram_msg("<i>🔄 System Reset Triggered via /start. Cache cleared! Scanning now...</i>", silent=True)
            force_scan = True

# ---------------------------------------------------------
# 7. WEB SERVER
# ---------------------------------------------------------
app = Flask(__name__)

@app.route('/')
def home(): return "🤖 Algora Bounty Hunter is ALIVE and running!"

if __name__ == "__main__":
    threading.Thread(target=run_bounty_hunter, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))

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
PREVIOUS_BOUNTY_IDS = []  # Used to check for duplicates (Silent Msgs)
BOUNTY_CACHE = {}         # Stores bounties so old Telegram buttons still work

# ---------------------------------------------------------
# 2. GITHUB DATA FETCHERS & ACTIONS
# ---------------------------------------------------------
def fetch_potential_bounties():
    url = "https://api.github.com/search/issues"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    query = "is:issue is:open label:bounty no:assignee"
    params = {"q": query, "sort": "created", "order": "desc", "per_page": 5}
    
    print("\n🔍 Scouting GitHub for fresh bounties...")
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
            "id": str(issue["id"]), # Unique ID for the cache
            "title": issue["title"],
            "url": issue["html_url"],
            "api_comments_url": issue["comments_url"], 
            "body": str(issue["body"])[:2000]
        }
        good_bounties.append(bounty)
        BOUNTY_CACHE[bounty["id"]] = bounty # Save to memory for old buttons
        
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
    """Phase 2 Setup: Automatically forks the repo to your account."""
    try:
        parts = html_url.split('/')
        owner, repo = parts[3], parts[4]
        url = f"https://api.github.com/repos/{owner}/{repo}/forks"
        headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        
        response = requests.post(url, headers=headers)
        if response.status_code in [202, 201]:
            return True, repo
        return False, "API blocked fork."
    except Exception as e:
        return False, str(e)

# ---------------------------------------------------------
# 3. AI ENGINES (GROQ & GEMINI)
# ---------------------------------------------------------
def evaluate_bounty(title, body, comments_text):
    """Groq Evaluator: Now strictly enforces FIAT money rules."""
    system_prompt = f"""
    You are an expert senior developer evaluating open-source bounties. My tech stack is: {MY_SKILLS}.
    1. Read the issue and comments. If claimed, set is_winnable to false.
    2. STRICT FIAT MONEY FILTER: The reward MUST be in real USD (e.g., $100, $50). 
       If the reward mentions 'RTC', altcoins, tokens, or lacks a literal '$' sign, YOU MUST SET is_winnable TO false. Do not calculate exchange rates.
    
    Return ONLY a valid JSON object:
    {{"score": <int 1-10>, "is_winnable": <bool>, "reward": "<Extract the $ amount>", "reason": "<Short sentence>"}}
    """
    try:
        completion = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": f"TITLE: {title}\n\nBODY:\n{body}\n\nCOMMENTS:\n{comments_text}"}],
            temperature=0.0, # 0.0 prevents AI hallucinations
            response_format={"type": "json_object"}
        )
        return json.loads(completion.choices[0].message.content)
    except:
        return {"score": 0, "is_winnable": False, "reward": "Unknown", "reason": "API Error"}

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
# 4. TELEGRAM COMMUNICATION (SILENT NOTIFICATIONS)
# ---------------------------------------------------------
def flush_telegram_updates():
    global LAST_UPDATE_ID
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        res = requests.get(url).json()
        if res.get("result"): LAST_UPDATE_ID = res["result"][-1]["update_id"]
    except: pass

def send_telegram_menu(bounties_list):
    """Sends the interactive menu to your phone."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    message = "🚨 <b>Found Top Bounties!</b>\n\n"
    
    inline_keyboard = []
    for idx, b in enumerate(bounties_list, 1):
        message += f"<b>[{idx}] {b['title']}</b>\nScore: {b['score']}/10 | 💰 {b.get('reward', 'Unknown')}\n<a href='{b['url']}'>🔗 View on GitHub</a>\n\n"
        # Store the GitHub ID in the button so we can recall it forever
        inline_keyboard.append([{"text": f"✅ Claim Option {idx}", "callback_data": f"CLAIM_{b['id']}"}])
        
    inline_keyboard.append([{"text": "⏭️ Skip All", "callback_data": "SKIP"}])
    
    payload = {
        "chat_id": TELEGRAM_CHAT_ID, 
        "text": message, 
        "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": inline_keyboard}
    }
    requests.post(url, json=payload)

def send_telegram_msg(text, silent=False):
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_notification": silent}
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
                    data = update["callback_query"]["data"]
                    if data.startswith("CLAIM_"):
                        return data.split("_")[1] # Returns the issue ID
                    elif data == "SKIP":
                        return "SKIP"
        except: pass
        time.sleep(2)
    return "TIMEOUT"

# ---------------------------------------------------------
# 5. THE AUTO-COMMENTER (WITH GEMINI)
# ---------------------------------------------------------
def execute_claim_protocol(bounty_id):
    """Handles Advanced Commenting and Auto-Forking."""
    if bounty_id not in BOUNTY_CACHE:
        send_telegram_msg("❌ Error: Bounty expired from cache. Cannot claim.")
        return

    bounty = BOUNTY_CACHE[bounty_id]
    send_telegram_msg("🧠 Gemini is reading the repo and drafting the claim...", silent=True)
    
    # 1. Draft the intelligent comment
    comments_text = fetch_issue_comments(bounty["api_comments_url"])
    smart_comment = generate_advanced_claim(bounty["title"], bounty["body"], comments_text)
    
    # 2. Post to GitHub
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    response = requests.post(bounty["api_comments_url"], headers=headers, json={"body": smart_comment})
    
    if response.status_code == 201:
        # 3. Auto-Fork the Repository
        send_telegram_msg(f"✅ `/attempt` posted successfully!\n\n🤖 Cloning repository to your account...", silent=True)
        fork_success, repo_name = fork_repository(bounty["url"])
        
        if fork_success:
            send_telegram_msg(f"🎯 <b>CLAIM COMPLETE</b>\n\n1. Issue Claimed.\n2. Repo Forked ({repo_name}).\n\nYou are clear to pull and branch.", silent=False)
        else:
            send_telegram_msg(f"⚠️ Claimed successfully, but Auto-Fork failed. Please fork manually.", silent=False)
    else:
        send_telegram_msg(f"❌ Error posting to GitHub: {response.text}")

# ---------------------------------------------------------
# 6. THE BOT LOOP
# ---------------------------------------------------------
def run_bounty_hunter():
    global PREVIOUS_BOUNTY_IDS
    print("🤖 V3.1 Agent Online. Anti-Spam active.")
    flush_telegram_updates() 
    
    while True:
        bounties = fetch_potential_bounties()
        high_score_bounties = []
        current_ids = []
        
        if bounties:
            for b in bounties:
                comments_text = fetch_issue_comments(b["api_comments_url"])
                verdict = evaluate_bounty(b["title"], b["body"], comments_text)
                
                # High standards + Money Filter check
                if verdict.get("score", 0) >= 7 and verdict.get("is_winnable"):
                    b["score"] = verdict["score"]
                    b["reward"] = verdict.get("reward", "Unknown")
                    high_score_bounties.append(b)
                    current_ids.append(b["id"])
                    
        if high_score_bounties:
            # 🛑 THE ANTI-SPAM FIX: Check for duplicates BEFORE doing anything
            if set(current_ids) == set(PREVIOUS_BOUNTY_IDS):
                print("💤 Bounties are identical to the last scan. Sleeping silently to prevent Telegram spam.")
                time.sleep(600) # Sleep 10 mins and check again
                continue # Skips the rest of the loop!
                
            # If we made it here, these are brand new bounties!
            PREVIOUS_BOUNTY_IDS = current_ids
            
            send_telegram_menu(high_score_bounties)
            
            # Wait 5 minutes (300 seconds) for interaction
            print("⏳ Waiting 5 mins for Telegram interaction...")
            user_choice = poll_telegram_for_buttons(timeout_seconds=300)
            
            if user_choice == "TIMEOUT":
                send_telegram_msg("⏭️ 5 mins passed. Auto-skipping.", silent=True)
                print("⏳ Sleeping for 10 minutes before rescan...")
                time.sleep(600) # Rescan after 10 mins
                
            elif user_choice == "SKIP":
                send_telegram_msg("⏭️ Manual Skip. Entering deep sleep.", silent=True)
                print("⏳ Sleeping for 15 minutes...")
                time.sleep(900) # Deep sleep for 15 mins
                
            else:
                # user_choice is the bounty_id from the button!
                execute_claim_protocol(user_choice)
                time.sleep(10) # Brief pause after action
        else:
            print("💤 No high-match cash bounties found. Sleeping 10 mins...")
            PREVIOUS_BOUNTY_IDS = []
            time.sleep(600)

# ---------------------------------------------------------
# 7. WEB SERVER
# ---------------------------------------------------------
app = Flask(__name__)

@app.route('/')
def home():
    return "🤖 Algora Bounty Hunter is ALIVE and running!"

if __name__ == "__main__":
    hunter_thread = threading.Thread(target=run_bounty_hunter, daemon=True)
    hunter_thread.start()
    
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

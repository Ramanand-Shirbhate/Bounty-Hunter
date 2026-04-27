import os
import json
import time
import threading
import requests
from flask import Flask
from groq import Groq
from dotenv import load_dotenv

# ---------------------------------------------------------
# 1. SETUP & CREDENTIALS
# ---------------------------------------------------------
load_dotenv()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

groq_client = Groq(api_key=GROQ_API_KEY)
MY_SKILLS = "TypeScript, Node.js, React, and basic Python."

# ---------------------------------------------------------
# 2. GITHUB DATA FETCHERS
# ---------------------------------------------------------
def fetch_potential_bounties():
    url = "https://api.github.com/search/issues"
    # Updated to Bearer token
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    query = "is:issue is:open label:bounty no:assignee"
    params = {"q": query, "sort": "created", "order": "desc", "per_page": 5}
    
    print("\n🔍 Scouting GitHub for fresh bounties...")
    response = requests.get(url, headers=headers, params=params)
    if response.status_code != 200:
        print(f"❌ GitHub API Error Fetching Bounties: {response.text}")
        return []
        
    data = response.json()
    bad_labels = ["reserved", "interview", "internal", "locked", "wip", "gitcoin", "drips"]
    good_bounties = []
    
    for issue in data.get("items", []):
        labels = [label["name"].lower() for label in issue["labels"]]
        if any(bad in l for l in labels for bad in bad_labels):
            continue 
            
        good_bounties.append({
            "title": issue["title"],
            "url": issue["html_url"],
            "api_comments_url": issue["comments_url"], 
            "body": str(issue["body"])[:2000]
        })
    return good_bounties

def fetch_issue_comments(comments_url):
    # Updated to Bearer token
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    response = requests.get(comments_url, headers=headers)
    if response.status_code == 200:
        comments_data = response.json()
        if not comments_data:
            return "No comments yet."
        chat_log = "\n".join([f"{c['user']['login']}: {c['body']}" for c in comments_data])
        return chat_log[:1500] 
    return "Could not fetch comments."

# ---------------------------------------------------------
# 3. GROQ AI EVALUATORS
# ---------------------------------------------------------
def evaluate_bounty(title, body, comments_text):
    system_prompt = f"""
    You are an expert senior developer evaluating open-source bounties for me. My tech stack is: {MY_SKILLS}.
    Read the issue description AND the comment history. If comments show someone else claimed it, set is_winnable to false.
    Return ONLY a valid JSON object:
    {{"score": <int 1-10>, "is_winnable": <bool>, "reason": "<One short sentence>"}}
    """
    try:
        completion = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": f"TITLE: {title}\n\nBODY:\n{body}\n\nCOMMENTS:\n{comments_text}"}],
            temperature=0.1,
            response_format={"type": "json_object"}
        )
        return json.loads(completion.choices[0].message.content)
    except:
        return {"score": 0, "is_winnable": False, "reason": "API Error"}

def generate_bounty_comparison(high_score_bounties):
    if len(high_score_bounties) < 2:
        return "<i>Only one high-match bounty found this round. No comparison needed.</i>"
    
    bounty_context = "".join([f"Option [{i}]: {b['title']} (Score: {b['score']}/10)\nWhy: {b['reason']}\n\n" for i, b in enumerate(high_score_bounties, 1)])
    system_prompt = f"You are my technical career advisor. My skills: {MY_SKILLS}. Read these summaries and write a 2-3 sentence comparison stating which is the fastest win. No markdown asterisks."
    
    try:
        completion = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": bounty_context}],
            temperature=0.3 
        )
        return completion.choices[0].message.content
    except:
        return "Comparison failed."

# ---------------------------------------------------------
# 4. TELEGRAM COMMUNICATION
# ---------------------------------------------------------
def send_telegram_menu(bounties_list, comparison_text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    message = f"🚨 <b>Found Top Bounties!</b>\n\n🤖 <b>AI Analyst Verdict:</b>\n{comparison_text}\n\n" + "➖"*15 + "\n\n"
    for idx, b in enumerate(bounties_list, 1):
        message += f"<b>[{idx}] {b['title']}</b>\nScore: {b['score']}/10\n<a href='{b['url']}'>🔗 View on GitHub</a>\n\n"
    message += "Reply with <b>CLAIM 1</b> or <b>SKIP</b>"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"})

def wait_for_telegram_command():
    print("⏳ Waiting for your reply on Telegram...")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    last_update_id = None
    try:
        init_res = requests.get(url).json()
        if init_res.get("result"): last_update_id = init_res["result"][-1]["update_id"]
    except: pass

    while True:
        try:
            res = requests.get(url).json()
            results = res.get("result", [])
            if results:
                latest_update = results[-1]
                update_id = latest_update["update_id"]
                if last_update_id is None or update_id > last_update_id:
                    text = latest_update.get("message", {}).get("text", "").strip().upper()
                    if text.startswith("CLAIM"):
                        try: return int(text.split(" ")[1])
                        except: pass
                    elif text == "SKIP": return 0
                    last_update_id = update_id 
        except: pass
        time.sleep(3)

def send_telegram_confirmation(text):
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": TELEGRAM_CHAT_ID, "text": text})

def post_github_comment(api_comments_url):
    # Updated to Bearer token
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    comment_body = {"body": "/attempt\n\nHi there! I'd love to take this on. I have strong experience with this stack and can start working on a clean, well-tested PR immediately."}
    
    print(f"🚀 Posting official claim to GitHub...")
    response = requests.post(api_comments_url, headers=headers, json=comment_body)
    
    if response.status_code == 201:
        return True, "Success"
    else:
        # Extract the exact error message to send back to Telegram
        try:
            error_details = response.json().get('message', response.text)
        except:
            error_details = response.text
        print(f"❌ GitHub API Error: {error_details}")
        return False, error_details

# ---------------------------------------------------------
# 5. THE BOT LOOP (Runs in Background)
# ---------------------------------------------------------
def run_bounty_hunter():
    print("🤖 Agent Background Thread Online.")
    while True:
        bounties = fetch_potential_bounties()
        high_score_bounties = []
        
        if bounties:
            for b in bounties:
                comments_text = fetch_issue_comments(b["api_comments_url"])
                verdict = evaluate_bounty(b["title"], b["body"], comments_text)
                if verdict.get("score", 0) >= 7 and verdict.get("is_winnable"):
                    b["score"] = verdict["score"]
                    b["reason"] = verdict["reason"]
                    high_score_bounties.append(b)
                    
        if high_score_bounties:
            comparison_text = generate_bounty_comparison(high_score_bounties)
            send_telegram_menu(high_score_bounties, comparison_text)
            user_choice = wait_for_telegram_command()
            
            if user_choice > 0 and user_choice <= len(high_score_bounties):
                selected = high_score_bounties[user_choice - 1]
                
                # Capture both success status and the error message
                success, error_msg = post_github_comment(selected["api_comments_url"])
                
                if success:
                    send_telegram_confirmation(f"✅ Successfully posted `/attempt` on:\n{selected['title']}")
                else:
                    # Send the exact GitHub error directly to your phone
                    send_telegram_confirmation(f"❌ Error posting to GitHub.\n\nReason: {error_msg}")
            else:
                send_telegram_confirmation("⏭️ Skipped.")
        else:
            print("💤 No high-match bounties found this round.")
            
        print("⏳ Sleeping for 10 minutes...")
        time.sleep(600)

# ---------------------------------------------------------
# 6. THE WEB SERVER (Keeps Render Awake)
# ---------------------------------------------------------
app = Flask(__name__)

@app.route('/')
def home():
    return "🤖 Algora Bounty Hunter is ALIVE and running!"

if __name__ == "__main__":
    # Start the bounty hunter in a separate background thread
    hunter_thread = threading.Thread(target=run_bounty_hunter, daemon=True)
    hunter_thread.start()
    
    # Start the web server to listen for Render/Pings
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

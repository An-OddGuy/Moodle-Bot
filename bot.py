import os, re, json, requests, time
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()
BASE = os.environ["PORTAL_URL"].rstrip("/")

def login():
    s = requests.Session()
    page = s.get(f"{BASE}/login/index.php")
    token = re.search(r'name="logintoken" value="(\w+)"', page.text).group(1)
    r = s.post(f"{BASE}/login/index.php", data={
        "username": os.environ["PORTAL_USER"],
        "password": os.environ["PORTAL_PASS"],
        "logintoken": token,
    })
    sesskey = re.search(r'"sesskey":"(\w+)"', r.text).group(1)
    userid = re.search(r'"userId":(\d+)', r.text) or re.search(r'data-userid="(\d+)"', r.text)
    return s, sesskey, int(userid.group(1))

def fetch_notifications(s, sesskey, userid):
    method = "message_popup_get_popup_notifications"
    url = f"{BASE}/lib/ajax/service.php?sesskey={sesskey}&info={method}"
    body = [{"index": 0, "methodname": method,
             "args": {"useridto": userid, "limit": 20, "offset": 0}}]
    r = s.post(url, json=body)
    return r.json()[0]["data"]["notifications"]

def mark_read(s, sesskey, nid):
    method = "core_message_mark_notification_read"
    url = f"{BASE}/lib/ajax/service.php?sesskey={sesskey}&info={method}"
    body = [{"index": 0, "methodname": method,
             "args": {"notificationid": nid, "timeread": int(time.time())}}]
    return s.post(url, json=body).json()[0].get("error") is False

def send_msg(text):
    r = requests.post(
        f"https://api.telegram.org/bot{os.environ['TG_TOKEN']}/sendMessage",
        data={"chat_id": os.environ["TG_CHAT_ID"], "text": text}, timeout=30)
    return r.ok

def build_text(n):
    body = n.get("fullmessage") or BeautifulSoup(
        n.get("fullmessagehtml", ""), "html.parser").get_text("\n").strip()
    link = n.get("contexturl", "")
    return f"📢 {n['subject']}\n\n{body}\n\n{link}"[:4000]

if __name__ == "__main__":
    s, sesskey, userid = login()
    new = fetch_notifications(s, sesskey, userid)[:2]
    for n in reversed(new):  # oldest first
        send_msg(build_text(n))  # WhatsApp goes here in Step 5
        # mark_read(s, sesskey, n["id"])  # enabled in Step 7



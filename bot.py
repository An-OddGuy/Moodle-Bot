import os, re, sys, json, time, requests
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
from dateutil import parser as dtparse
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

load_dotenv(override=True)
BASE = os.environ["PORTAL_URL"].rstrip("/")
SEEN_FILE = "seen_ann.json"
MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
ANN_DATE_RE = re.compile(
    rf"(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTHS})[a-z]*\.?,?\s*(\d{{4}})?"
    rf"(?:[,\s]+(?:at\s+)?(\d{{1,2}}(?::\d{{2}})?\s?[ap]\.?m\.?))?", re.I)


# ---------- Moodle ----------
def login():
    s = requests.Session()
    page = s.get(f"{BASE}/login/index.php")
    token = re.search(r'name="logintoken" value="(\w+)"', page.text).group(1)
    r = s.post(f"{BASE}/login/index.php", data={
        "username": os.environ["PORTAL_USER"],
        "password": os.environ["PORTAL_PASS"],
        "logintoken": token,
    })
    if "login/index.php" in r.url or "loginerrormessage" in r.text:
        raise SystemExit("Login failed")
    sesskey = re.search(r'"sesskey":"(\w+)"', r.text).group(1)
    userid = re.search(r'"userId":(\d+)', r.text) or re.search(r'data-userid="(\d+)"', r.text)
    return s, sesskey, int(userid.group(1))


def call(s, sesskey, method, args):
    url = f"{BASE}/lib/ajax/service.php?sesskey={sesskey}&info={method}"
    res = s.post(url, json=[{"index": 0, "methodname": method, "args": args}]).json()[0]
    if res.get("error"):
        raise RuntimeError(f"{method}: {res['exception'].get('errorcode')}")
    return res["data"]


def fetch_notifications(s, sesskey, userid):
    return call(s, sesskey, "message_popup_get_popup_notifications",
                {"useridto": userid, "limit": 20, "offset": 0})["notifications"]


def mark_read(s, sesskey, nid):
    try:
        call(s, sesskey, "core_message_mark_notification_read",
             {"notificationid": nid, "timeread": int(time.time())})
        return True
    except Exception:
        return False


def fetch_announcements(s, sesskey, per_course=5):
    courses = call(s, sesskey, "core_course_get_enrolled_courses_by_timeline_classification",
                   {"classification": "inprogress", "limit": 0, "offset": 0,
                    "sort": "fullname"})["courses"]
    out = []
    for c in courses:
        soup = BeautifulSoup(s.get(f"{BASE}/course/view.php?id={c['id']}").text, "html.parser")
        link = next((a["href"] for a in soup.find_all(
            "a", href=re.compile(r"/mod/forum/view\.php\?id=\d+"))
            if "announcements" in a.get_text().lower()), None)
        if not link:
            continue
        fsoup = BeautifulSoup(s.get(link).text, "html.parser")
        seen = set()
        for a in fsoup.find_all("a", href=re.compile(r"/mod/forum/discuss\.php\?d=\d+")):
            title = a.get_text(strip=True)
            d = re.search(r"d=(\d+)", a["href"]).group(1)
            if not title or d in seen:
                continue
            seen.add(d)
            out.append({"id": int(d), "course": c["fullname"], "title": title, "url": a["href"]})
            if len(seen) >= per_course:
                break
    return sorted(out, key=lambda a: a["id"])

# ---------- Telegram ----------
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


# ---------- Google Calendar ----------
DATE_RE = re.compile(r"\w+day, \d{1,2} \w+ \d{4}, \d{1,2}:\d{2}\s?[AP]M")


def get_calendar():
    if os.environ.get("GOOGLE_TOKEN_JSON"):  # GitHub Actions
        creds = Credentials.from_authorized_user_info(json.loads(os.environ["GOOGLE_TOKEN_JSON"]))
    else:
        creds = Credentials.from_authorized_user_file("token.json")
    return build("calendar", "v3", credentials=creds)


def norm(t):
    return re.sub(r"[^a-z0-9]", "", t.lower())


def make_event(cal, title, start, link):
    if start < datetime.now():
        return
    tz = timezone(timedelta(hours=5, minutes=30))  # IST
    s = start.replace(tzinfo=tz)
    existing = cal.events().list(
        calendarId="primary",
        timeMin=(s - timedelta(minutes=1)).isoformat(),
        timeMax=(s + timedelta(minutes=1)).isoformat(),
        singleEvents=True,
    ).execute().get("items", [])
    for e in existing:
        old, new = norm(e.get("summary", "")), norm(title)
        if old and (old in new or new in old):
            return
        if link and e.get("description", "") == link:
            return
    ev = {
        "summary": title,
        "description": link,
        "start": {"dateTime": start.isoformat(), "timeZone": "Asia/Kolkata"},
        "end": {"dateTime": (start + timedelta(minutes=30)).isoformat(), "timeZone": "Asia/Kolkata"},
        "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 720}]},
    }
    cal.events().insert(calendarId="primary", body=ev).execute()


def add_event(cal, n):
    body = n.get("fullmessage") or BeautifulSoup(
        n.get("fullmessagehtml", ""), "html.parser").get_text("\n")
    m = DATE_RE.search(n["subject"] + "\n" + body)
    if m:
        title = re.sub(r"^Due on .*?[AP]M: ", "", n["subject"])
        make_event(cal, title, dtparse.parse(m.group(0)), n.get("contexturl", ""))




def fetch_body(s, url):
    soup = BeautifulSoup(s.get(url).text, "html.parser")
    el = soup.select_one(".post-content-container, .posting, .forumpost .content")
    return el.get_text("\n").strip() if el else ""


def load_seen():
    try:
        return set(json.load(open(SEEN_FILE)))
    except Exception:
        return None


def ann_event(cal, a, body):
    m = ANN_DATE_RE.search(a["title"] + "\n" + body)
    if not m:
        return
    day, mon, year, t = m.groups()
    t = (t or "11:59 PM").replace(".", "")
    start = dtparse.parse(f"{day} {mon} {year or datetime.now().year} {t}")
    make_event(cal, f"{a['course'][:30]}: {a['title']}", start, a["url"])


def process_announcements(s, sesskey, cal):
    seen = load_seen()
    anns = fetch_announcements(s, sesskey)
    if seen is None:  # first run: record existing posts, don't spam
        json.dump([a["id"] for a in anns], open(SEEN_FILE, "w"))
        return
    for a in anns:
        if a["id"] in seen:
            continue
        body = fetch_body(s, a["url"])
        if send_msg(f"📌 {a['course']}\n{a['title']}\n\n{body}\n\n{a['url']}"[:4000]):
            try:
                ann_event(cal, a, body)
            except Exception as e:
                print(type(e).__name__)
            seen.add(a["id"])
            json.dump(sorted(seen), open(SEEN_FILE, "w"))


# ---------- main ----------
if __name__ == "__main__":
    s, sesskey, userid = login()

    if len(sys.argv) > 1 and sys.argv[1] == "ann":  # test: python bot.py ann
        for a in fetch_announcements(s, sesskey):
            print(a["id"], a["course"], "|", a["title"])
        raise SystemExit

    cal = get_calendar()
    new = [n for n in fetch_notifications(s, sesskey, userid) if not n["read"]]
    for n in reversed(new):  # oldest first
        if send_msg(build_text(n)):
            try:
                add_event(cal, n)
            except Exception as e:
                print(type(e).__name__)  # no details: repo logs are public
            mark_read(s, sesskey, n["id"])
    process_announcements(s, sesskey, cal)
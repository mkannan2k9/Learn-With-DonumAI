import datetime
import hmac
import json
import os
import re
import sys
import tempfile
import threading
import time

import requests
from flask import Flask, request
from google import genai
from gtts import gTTS

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
TOPICS_FILE = os.path.join(DATA_DIR, "topics.json")
AUDIO_FILE = os.path.join(DATA_DIR, "episode.mp3")


def load_env(path):
    """Minimal .env reader so secrets never live in the source code."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env(os.path.join(BASE_DIR, ".env"))

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
TRIGGER_KEY = os.environ.get("TRIGGER_KEY", "")
PODBEAN_CLIENT_ID = os.environ.get("PODBEAN_CLIENT_ID", "")
PODBEAN_CLIENT_SECRET = os.environ.get("PODBEAN_CLIENT_SECRET", "")
PODBEAN_PUBLISH = os.environ.get("PODBEAN_PUBLISH", "1") == "1"
TEXT_MODEL = os.environ.get("TEXT_MODEL", "gemini-3.5-flash-lite")

app = Flask(__name__)

_file_lock = threading.Lock()
_job_lock = threading.Lock()
_client = None


# ---------- helpers ----------

def get_client():
    global _client
    if _client is None:
        if not GOOGLE_API_KEY:
            raise RuntimeError("GOOGLE_API_KEY is not set (add it to the .env file).")
        _client = genai.Client(api_key=GOOGLE_API_KEY)
    return _client


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def write_json(path, data):
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def clean_title(title, fallback):
    title = " ".join((title or "").replace('"', "").split())
    if title.lower().startswith("title:"):
        title = title[6:].strip()
    return title[:120] or fallback


def norm_topic(text):
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", (text or "").lower()).split())


def is_duplicate_topic(topic, seen):
    words = set(topic.split())
    for old in seen:
        if topic == old:
            return True
        other = set(old.split())
        if words and other and len(words & other) / len(words | other) >= 0.8:
            return True
    return False


def authorised(supplied, expected):
    if not expected or not supplied:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


@app.after_request
def security_headers(resp):
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    return resp


# ---------- Gemini (Interactions API) ----------

def ask(prompt, schema=None, retries=3):
    """One text call. Returns text, or None after all retries fail."""
    for attempt in range(retries):
        try:
            kwargs = {"model": TEXT_MODEL, "input": prompt, "store": False}
            if schema:
                kwargs["response_format"] = {
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": schema,
                }
            interaction = get_client().interactions.create(**kwargs)
            text = (interaction.output_text or "").strip()
            if text:
                return text
        except Exception as e:
            app.logger.warning("Gemini attempt %s failed: %s", attempt + 1, e)
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                time.sleep(30 * (attempt + 1))
            else:
                time.sleep(2)
    return None


def ask_json(prompt, schema, retries=3):
    raw = ask(prompt, schema, retries)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        app.logger.warning("Model returned invalid JSON")
        return None
    return data if isinstance(data, dict) else None


# ---------- podcast pipeline ----------

TOPIC_SCHEMA = {
    "type": "object",
    "properties": {"topic": {"type": "string"}, "subject": {"type": "string"}},
    "required": ["topic", "subject"],
}
META_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "description": {"type": "string"}},
    "required": ["title", "description"],
}


def pick_topic():
    history = read_json(TOPICS_FILE, [])
    seen = {norm_topic(item.get("topic")) for item in history}
    recent = [item["topic"] for item in history[-60:] if item.get("topic")]

    for attempt in range(5):
        prompt = (
            "Suggest one specific, interesting educational topic and its school subject "
            "for a short podcast for Grade 6 students. "
        )
        if recent:
            prompt += "Do not suggest any of these topics or close variants: " + "; ".join(recent) + ". "
        prompt += "Return JSON with the fields topic and subject."
        data = ask_json(prompt, TOPIC_SCHEMA)
        if not data:
            continue
        topic = (data.get("topic") or "").strip()
        subject = (data.get("subject") or "").strip()
        if not topic or not subject:
            continue
        if is_duplicate_topic(norm_topic(topic), seen):
            app.logger.info("Duplicate topic rejected: %s", topic)
            continue
        history.append({"topic": topic, "subject": subject,
                        "date": datetime.datetime.now().strftime("%Y/%m/%d")})
        with _file_lock:
            write_json(TOPICS_FILE, history)
        return topic, subject
    return None, None


def publish_podbean(title, description):
    if not (PODBEAN_PUBLISH and PODBEAN_CLIENT_ID and PODBEAN_CLIENT_SECRET):
        app.logger.info("Podbean publishing skipped (not configured).")
        return False
    token_resp = requests.post(
        "https://api.podbean.com/v1/oauth/token",
        data={"grant_type": "client_credentials",
              "client_id": PODBEAN_CLIENT_ID,
              "client_secret": PODBEAN_CLIENT_SECRET},
        timeout=30,
    )
    token_resp.raise_for_status()
    access_token = token_resp.json()["access_token"]

    auth_resp = requests.get(
        "https://api.podbean.com/v1/files/uploadAuthorize",
        params={"access_token": access_token, "filename": "episode.mp3",
                "filesize": os.path.getsize(AUDIO_FILE), "content_type": "audio/mpeg"},
        timeout=30,
    )
    auth_resp.raise_for_status()
    auth = auth_resp.json()

    with open(AUDIO_FILE, "rb") as f:
        put = requests.put(auth["presigned_url"], data=f,
                           headers={"Content-Type": "audio/mpeg"}, timeout=180)
    put.raise_for_status()

    ep = requests.post(
        "https://api.podbean.com/v1/episodes",
        data={"access_token": access_token, "title": title, "content": description,
              "status": "publish", "type": "public", "media_key": auth["file_key"]},
        timeout=60,
    )
    if not ep.ok:
        app.logger.error("Podbean episode creation failed: %s %s", ep.status_code, ep.text[:300])
        return False
    return True


def run_podcast():
    topic, subject = pick_topic()
    if not topic:
        app.logger.error("Could not find a new topic.")
        return False
    app.logger.info("Podcast topic: %s", topic)

    script = ask(
        "Write a podcast script. Topic: " + topic + " | Subject: " + subject + ". "
        "Style: storytelling, Grade 6 level, about 300 words. Plain text only, no markdown."
    )
    if not script:
        app.logger.error("Script generation failed.")
        return False

    spoken = re.sub(r"[*#_`]", "", script)
    os.makedirs(DATA_DIR, exist_ok=True)
    gTTS(text=spoken, lang="en", slow=False).save(AUDIO_FILE)

    meta = ask_json(
        "Write a podcast episode title and a one or two sentence description for the topic: "
        + topic + ". Return JSON with the fields title and description.", META_SCHEMA
    ) or {}
    title = clean_title(meta.get("title"), topic + " Episode")
    description = (meta.get("description") or "Listen now.").strip()[:500]

    try:
        return publish_podbean(title, description)
    except Exception as e:
        app.logger.error("Podbean upload failed: %s", e)
        return False


def start_job():
    if not _job_lock.acquire(blocking=False):
        return False

    def runner():
        try:
            run_podcast()
        except Exception:
            app.logger.exception("Podcast job failed")
        finally:
            _job_lock.release()

    threading.Thread(target=runner, daemon=True).start()
    return True


# ---------- routes ----------

@app.route("/")
def home():
    return "Learn with DonumAI podcast generator.", 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/trigger/hello")
@app.route("/trigger/podcast")
def trigger_podcast():
    supplied = request.headers.get("X-Trigger-Key") or request.args.get("key") or ""
    if not authorised(supplied, TRIGGER_KEY):
        return "Forbidden", 403
    if not start_job():
        return "A podcast job is already running.", 409
    return "Podcast job started.", 202


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "podcast":
        print("Podcast published:", run_podcast())
    else:
        app.run(host="127.0.0.1", port=5000)

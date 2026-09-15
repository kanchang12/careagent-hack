import os
import io
import time
import base64
import json
import threading
import requests
import logging
import traceback
from datetime import datetime
from pathlib import Path

from flask import Flask, request, jsonify, render_template, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix
from dotenv import load_dotenv
from PIL import Image
from google import genai
from google.genai import types
from twilio.rest import Client as TwilioClient

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "care-agent-v1")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

gunicorn_logger = logging.getLogger('gunicorn.error')
if gunicorn_logger.handlers:
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
else:
    app.logger.setLevel(logging.INFO)

UPLOAD_DIR = Path("static/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
TWILIO_SID     = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN   = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM    = os.getenv("TWILIO_VOICE_FROM")
FAMILY_PHONE   = os.getenv("FAMILY_PHONE_TO")

ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
twilio_client = TwilioClient(TWILIO_SID, TWILIO_TOKEN) if (TWILIO_SID and TWILIO_TOKEN) else None

alerts = {}
alerts_lock = threading.Lock()
events_feed = []
feed_lock = threading.Lock()

# ---------- GLOBAL PROCESSING LOCK ----------
LOCK = {"until": 0.0}
lock_mutex = threading.Lock()

# ---------- SPEAKER FLAG ----------
SPEAKER = {"on": False, "phrase": "Emergency detected. Please stay calm. Help is coming."}
speaker_mutex = threading.Lock()

PERSON_DETECTION_TIMEOUT = 180
CONTINUOUS_GESTURE_LIMIT = 3

app.logger.info("=" * 60)
app.logger.info("[BOOT] Care Agent starting")
app.logger.info(f"[BOOT] GEMINI_MODEL      = {GEMINI_MODEL}")
app.logger.info(f"[BOOT] ai_client         = {'OK' if ai_client else 'MISSING'}")
app.logger.info(f"[BOOT] twilio_client     = {'OK' if twilio_client else 'MISSING'}")
app.logger.info("=" * 60)


def push_feed(entry):
    with feed_lock:
        events_feed.insert(0, entry)
        del events_feed[100:]
    app.logger.info(f"[FEED] {entry.get('type')}: {entry.get('message')}")


def lock_is_on():
    with lock_mutex:
        return LOCK["until"] > time.time()

def lock_remaining():
    with lock_mutex:
        return max(0, int(LOCK["until"] - time.time()))

def lock_on(seconds):
    with lock_mutex:
        LOCK["until"] = time.time() + seconds
    app.logger.info(f"[LOCK] ON for {seconds}s")

def lock_off():
    with lock_mutex:
        LOCK["until"] = 0.0
    app.logger.info("[LOCK] OFF")


def speaker_is_on():
    with speaker_mutex:
        return SPEAKER["on"]

def speaker_on(phrase=None):
    with speaker_mutex:
        SPEAKER["on"] = True
        if phrase:
            SPEAKER["phrase"] = phrase
    app.logger.info("[SPEAKER] ON — will repeat every 20s until stopped.")

def speaker_off():
    with speaker_mutex:
        SPEAKER["on"] = False
    app.logger.info("[SPEAKER] OFF (manual stop).")


# ---------- TOOLS ----------
def tool_send_whatsapp(message):
    token = os.getenv("META_WHATSAPP_TOKEN")
    phone_id = os.getenv("META_PHONE_NUMBER_ID")
    to_number = os.getenv("FAMILY_WHATSAPP_TO")
    if not token or not phone_id:
        app.logger.warning("[WhatsApp] Missing credentials.")
        return False
    url = f"https://graph.facebook.com/v19.0/{phone_id}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": f"🚨 Care Agent Alert\n{message}\nPlease check the dashboard."}
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        app.logger.info(f"[WhatsApp] HTTP {r.status_code}")
        return r.status_code in [200, 201]
    except Exception as e:
        app.logger.error(f"[WhatsApp Exception] {e}")
        return False


def tool_send_voice_call(reason):
    app.logger.info(f"[Voice] Attempting call. Reason: {reason}")
    if not twilio_client:
        app.logger.warning("[Voice] Missing Twilio credentials.")
        return False
    if not TWILIO_FROM or not FAMILY_PHONE:
        app.logger.warning("[Voice] Missing FROM or TO.")
        return False
    try:
        twiml = f"<Response><Say voice='alice'>Emergency Alert. {reason}. Please check the dashboard immediately.</Say></Response>"
        call = twilio_client.calls.create(twiml=twiml, to=FAMILY_PHONE, from_=TWILIO_FROM)
        app.logger.info(f"[Voice] ✅ Call dispatched. SID: {call.sid}")
        return True
    except Exception as e:
        app.logger.error(f"[Voice Error] {type(e).__name__}: {e}")
        app.logger.error(traceback.format_exc())
        return False


def tool_send_to_webhook(alert_id, gesture, assessment, raw_b64):
    webhook_url = os.getenv("MAKE_WEBHOOK_URL")
    if not webhook_url:
        app.logger.warning("[Webhook] MAKE_WEBHOOK_URL not set.")
        return False
    payload = {
        "alert_id": alert_id,
        "timestamp": datetime.utcnow().isoformat(),
        "gesture": gesture,
        "assessment": assessment,
        "filename": f"{alert_id}.jpg",
        "image_base64": raw_b64
    }
    try:
        r = requests.post(webhook_url, json=payload, timeout=10)
        app.logger.info(f"[Webhook] HTTP {r.status_code}")
        return r.status_code in [200, 201, 202]
    except Exception as e:
        app.logger.error(f"[Webhook Exception] {e}")
        return False


def detect_person_in_frame(image_bytes):
    """
    Uses Gemini vision to detect if a caregiver/person is present in frame.
    Returns True if person detected, False otherwise.
    """
    if not ai_client:
        return False
    try:
        prompt = (
            "Look at this image of an elderly patient. "
            "Is there another person (caregiver, family member, visitor) visible in the frame? "
            "Answer ONLY 'YES' or 'NO'."
        )
        chat = ai_client.chats.create(
            model=GEMINI_MODEL,
            config=types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=10,
            ),
        )
        response = chat.send_message(
            [types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"), prompt]
        )
        answer = (response.text or "").strip().upper()
        detected = "YES" in answer
        app.logger.info(f"[PERSON_DETECT] {answer} → {detected}")
        return detected
    except Exception as e:
        app.logger.error(f"[PERSON_DETECT Error] {e}")
        return False


# ---------- ESCALATION WITH PERSON DETECTION ----------
def escalation_monitor(alert_id, gesture_type, spoken_phrase):
    """
    Monitor for 3 minutes.
    - If person appears in frame within 3 min: drop alert.
    - If person never appears: call after 3 min.
    - If person appears but patient gestures again 3+ times continuously: call.
    """
    start_time = time.time()
    person_appeared = False
    gesture_count = 0

    app.logger.info(f"[ESCALATION {alert_id}] started. Watching for {PERSON_DETECTION_TIMEOUT}s")

    while True:
        time.sleep(5)
        elapsed = time.time() - start_time

        with alerts_lock:
            alert = alerts.get(alert_id)
            if not alert:
                app.logger.info(f"[ESCALATION {alert_id}] alert cleared externally.")
                return

        # Timeout: if 3 min passed and no person, call and send WhatsApp
        if elapsed >= PERSON_DETECTION_TIMEOUT and not person_appeared:
            app.logger.info(f"[ESCALATION {alert_id}] 3 min timeout, no person. Calling.")
            ok = tool_send_voice_call(spoken_phrase)
            ok_wa = tool_send_whatsapp(spoken_phrase)
            push_feed({
                "time": datetime.utcnow().strftime("%H:%M:%S"),
                "type": "Twilio Voice",
                "message": f"3-min timeout call {'dispatched' if ok else 'FAILED'}."
            })
            push_feed({
                "time": datetime.utcnow().strftime("%H:%M:%S"),
                "type": "Meta WhatsApp",
                "message": f"WhatsApp alert {'sent' if ok_wa else 'FAILED'} at 3-min timeout."
            })
            return

        # If person appeared, check gesture count
        if person_appeared and gesture_count >= CONTINUOUS_GESTURE_LIMIT:
            app.logger.info(f"[ESCALATION {alert_id}] person present but {gesture_count} gestures. Calling.")
            ok = tool_send_voice_call(spoken_phrase)
            ok_wa = tool_send_whatsapp(spoken_phrase)
            push_feed({
                "time": datetime.utcnow().strftime("%H:%M:%S"),
                "type": "Twilio Voice",
                "message": f"Persistent gesture call {'dispatched' if ok else 'FAILED'}."
            })
            push_feed({
                "time": datetime.utcnow().strftime("%H:%M:%S"),
                "type": "Meta WhatsApp",
                "message": f"WhatsApp alert {'sent' if ok_wa else 'FAILED'} after persistent gestures."
            })
            return


def detect_person_and_update(alert_id, image_bytes):
    """
    Called each frame. If person detected, set person_appeared flag.
    """
    with alerts_lock:
        alert = alerts.get(alert_id)
        if not alert:
            return

        if not alert.get("person_appeared"):
            if detect_person_in_frame(image_bytes):
                alert["person_appeared"] = True
                app.logger.info(f"[{alert_id}] Person detected in frame. Escalation cancels if no more gestures.")
                push_feed({
                    "time": datetime.utcnow().strftime("%H:%M:%S"),
                    "type": "Person Detection",
                    "message": "Caregiver/visitor detected in frame."
                })


def log_gesture_for_alert(alert_id):
    """Track consecutive gestures after person appears."""
    with alerts_lock:
        alert = alerts.get(alert_id)
        if alert and alert.get("person_appeared"):
            alert["consecutive_gestures"] = alert.get("consecutive_gestures", 0) + 1
            app.logger.info(f"[{alert_id}] Gesture count: {alert['consecutive_gestures']}")


# ---------- VIEWS ----------
@app.route("/", methods=["GET", "POST"])
def patient_view():
    return render_template("patient.html")

@app.route("/uploads/<path:filename>", methods=["GET", "POST"])
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


# ---------- SPEAKER LOOP ENDPOINT ----------
@app.route("/api/v1/patient-loop", methods=["GET", "POST"])
def patient_loop():
    if speaker_is_on():
        with speaker_mutex:
            phrase = SPEAKER["phrase"]
        return jsonify({"speak": True, "phrase": phrase})
    return jsonify({"speak": False})


@app.route("/api/v1/stop-speaker", methods=["GET", "POST"])
def stop_speaker():
    speaker_off()
    lock_off()
    push_feed({
        "time": datetime.utcnow().strftime("%H:%M:%S"),
        "type": "Patient",
        "message": "STOP pressed. Speaker + lock cleared."
    })
    return jsonify({"status": "STOPPED"})


# ---------- MAIN FRAME PROCESSOR ----------
@app.route("/api/v1/process-frame", methods=["GET", "POST"])
def process_frame():
    if lock_is_on():
        remaining = lock_remaining()
        app.logger.info(f"[FRAME] BLOCKED — {remaining}s left.")
        return jsonify({
            "locked": True,
            "remaining": remaining,
            "assessment": f"Processing paused. {remaining}s left.",
            "spoken_code": "NONE",
            "alert_id": None
        }), 200

    data = request.get_json(force=True, silent=True) or {}
    snapshot_b64 = data.get("snapshot")
    gesture_input = data.get("gesture", "NONE").upper()

    app.logger.info(f"[FRAME] Received gesture={gesture_input}")

    if not snapshot_b64:
        return jsonify({"error": "snapshot missing"}), 400

    alert_id = f"ALT-{int(time.time())}"
    fname = f"{alert_id}.jpg"
    fpath = UPLOAD_DIR / fname

    try:
        raw_b64 = snapshot_b64.split(",")[1] if "," in snapshot_b64 else snapshot_b64
        image_bytes = base64.b64decode(raw_b64)
        Image.open(io.BytesIO(image_bytes)).convert("RGB").save(fpath, "JPEG", quality=80)
    except Exception as e:
        app.logger.error(f"[FRAME] Image save failed: {e}")
        return jsonify({"error": f"Image decode failed: {e}"}), 400

    # Determine if alert needed
    is_alert = gesture_input in ["WATER", "FOOD", "TOILET"]
    is_emergency = gesture_input == "FIST"

    spoken_code = "NONE"
    assessment = f"No action. Gesture: {gesture_input}"

    if gesture_input == "WATER":
        assessment = "Patient is asking for water."
        spoken_code = "He is asking for water, please attend."
    elif gesture_input == "FOOD":
        assessment = "Patient is asking for food."
        spoken_code = "He is asking for food, please attend."
    elif gesture_input == "TOILET":
        assessment = "Patient is asking for toilet assistance."
        spoken_code = "He needs toilet assistance, please attend."
    elif gesture_input == "FIST":
        assessment = "Emergency: Patient in distress."
        spoken_code = "Emergency detected. Help is coming immediately."
        is_alert = True
        is_emergency = True

    status = "ESCALATED" if is_alert else "RESOLVED"

    with alerts_lock:
        alerts[alert_id] = {
            "alert_id": alert_id,
            "created_at": datetime.utcnow().isoformat(),
            "gesture": gesture_input,
            "status": status,
            "assessment": assessment,
            "image": f"/uploads/{fname}",
            "person_appeared": False,
            "consecutive_gestures": 0
        }

    # WhatsApp only on emergency (immediate) or escalation (later)
    # For routine requests, wait for escalation logic

    # Immediate call for emergency
    if is_emergency:
        ok_call = tool_send_voice_call(assessment)
        push_feed({
            "time": datetime.utcnow().strftime("%H:%M:%S"),
            "type": "Twilio Voice",
            "message": f"Emergency call {'dispatched' if ok_call else 'FAILED'} immediately."
        })

    # Turn on speaker and lock for any alert
    if is_alert:
        lock_on(30)  # 30s lock between frames
        speaker_on(spoken_code)  # Use the specific phrase (water/food/toilet/emergency)
        app.logger.info(f"[{alert_id}] ALERT — emergency={is_emergency}. Lock ON, Speaker ON.")

    # Back up to webhook
    if gesture_input != "NONE":
        tool_send_to_webhook(alert_id, gesture_input, assessment, raw_b64)
        push_feed({"time": datetime.utcnow().strftime("%H:%M:%S"), "type": "Make.com", "message": "Backed up to G-Drive."})

    # Start escalation monitor in background (non-emergency only)
    if is_alert and not is_emergency:
        threading.Thread(
            target=escalation_monitor,
            args=(alert_id, gesture_input, spoken_code),
            daemon=True
        ).start()
        # Start person detection thread
        threading.Thread(
            target=person_detection_thread,
            args=(alert_id, image_bytes),
            daemon=True
        ).start()

    if status == "RESOLVED":
        push_feed({"time": datetime.utcnow().strftime("%H:%M:%S"), "type": "WATCHDOG", "message": "Routine check safe."})

    return jsonify({
        "alert_id": alert_id,
        "assessment": assessment,
        "spoken_code": spoken_code,
        "locked": False
    })


def person_detection_thread(alert_id, initial_image_bytes):
    """
    Periodically check frames for person presence over 3 minutes.
    """
    start_time = time.time()
    while True:
        time.sleep(10)
        elapsed = time.time() - start_time

        with alerts_lock:
            alert = alerts.get(alert_id)
            if not alert:
                return

        if elapsed >= PERSON_DETECTION_TIMEOUT:
            return

        if not alert.get("person_appeared"):
            detect_person_and_update(alert_id, initial_image_bytes)


@app.route("/api/v1/state", methods=["GET", "POST"])
def get_state():
    with alerts_lock:
        active = [a for a in alerts.values() if a["status"] != "RESOLVED"]
        recent = sorted(alerts.values(), key=lambda x: x["created_at"], reverse=True)[:10]
    with feed_lock:
        feed = events_feed[:20]
    return jsonify({"active_alerts": active, "recent_images": recent, "feed": feed})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False, threaded=True)

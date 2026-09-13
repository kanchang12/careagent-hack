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

# ---------- GLOBAL PROCESSING LOCK (independent of speaker) ----------
LOCK = {"until": 0.0}
lock_mutex = threading.Lock()

# ---------- SPEAKER FLAG (independent, manual stop only) ----------
SPEAKER = {"on": False, "phrase": "Emergency detected. Please stay calm. Help is coming."}
speaker_mutex = threading.Lock()

ESCALATION_CALL_AT  = 300
PROCESSING_LOCK_SEC = 300

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


# ---------------- TOOLS ----------------
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


def tool_get_jamendo_music():
    client_id = os.getenv("JAMENDO_CLIENT_ID")
    fallback = "https://prod-1.storage.jamendo.com/?trackid=1890757&format=mp31"
    if not client_id:
        return fallback
    try:
        url = f"https://api.jamendo.com/v3.0/tracks/?client_id={client_id}&format=json&tags=ambient,relaxing&limit=1"
        r = requests.get(url, timeout=5)
        data = r.json()
        if data.get("results"):
            return data["results"][0]["audio"]
        return fallback
    except Exception:
        return fallback


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


# ---------------- ESCALATION (for non-emergency 5 min call) ----------------
def escalation_timer(alert_id, message, is_emergency):
    start = time.time()
    call_sent = is_emergency
    app.logger.info(f"[ESCALATION {alert_id}] started. emergency={is_emergency}")

    while True:
        time.sleep(1)
        elapsed = time.time() - start

        with alerts_lock:
            alert = alerts.get(alert_id)
            if not alert:
                return

        if (not is_emergency) and (not call_sent) and elapsed >= ESCALATION_CALL_AT:
            call_sent = True
            app.logger.info(f"[ESCALATION {alert_id}] 5 min → placing call")
            ok = tool_send_voice_call(f"Unresolved request: {message}")
            push_feed({
                "time": datetime.utcnow().strftime("%H:%M:%S"),
                "type": "Twilio Voice",
                "message": f"Voice call {'dispatched' if ok else 'FAILED'} at 5 min."
            })
            return

        if elapsed >= ESCALATION_CALL_AT:
            app.logger.info(f"[ESCALATION {alert_id}] 5 min elapsed. Done.")
            return


# ---------------- VIEWS ----------------
@app.route("/", methods=["GET", "POST"])
def patient_view():
    return render_template("patient.html")

@app.route("/uploads/<path:filename>", methods=["GET", "POST"])
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


# ---------------- SPEAKER LOOP ENDPOINT ----------------
@app.route("/api/v1/patient-loop", methods=["GET", "POST"])
def patient_loop():
    # Returns ON until manually stopped via /api/v1/stop-speaker
    if speaker_is_on():
        with speaker_mutex:
            phrase = SPEAKER["phrase"]
        return jsonify({"speak": True, "phrase": phrase})
    return jsonify({"speak": False})


@app.route("/api/v1/stop-speaker", methods=["GET", "POST"])
def stop_speaker():
    speaker_off()
    lock_off()  # stop button also releases the processing lock
    push_feed({
        "time": datetime.utcnow().strftime("%H:%M:%S"),
        "type": "Patient",
        "message": "STOP pressed. Speaker + lock cleared."
    })
    return jsonify({"status": "STOPPED"})


# ---------------- MAIN FRAME PROCESSOR ----------------
@app.route("/api/v1/process-frame", methods=["GET", "POST"])
def process_frame():
    # HARD GATE — if locked, do nothing else
    if lock_is_on():
        remaining = lock_remaining()
        app.logger.info(f"[FRAME] BLOCKED — {remaining}s left.")
        return jsonify({
            "locked": True,
            "remaining": remaining,
            "assessment": f"Processing paused. {remaining}s left.",
            "spoken_code": "NONE",
            "play_music_url": None,
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

    prompt = (
        f"You are an autonomous Care Agent evaluating an elderly patient.\n"
        f"A local edge model guessed the gesture is: '{gesture_input}'. DO NOT TRUST IT BLINDLY.\n"
        f"Look closely at the patient's hand in the image. If YOU see 1 finger, 2 fingers, 3 fingers, or a closed fist, your vision overrides the edge model.\n"
        f"Based on your visual assessment:\n"
        f"- If YOU see a FIST or a fall: execute voice_call, whatsapp, and comfort_music.\n"
        f"- If YOU see 1, 2, or 3 fingers: execute whatsapp, NO voice_call, NO music.\n"
        f"- If YOU see no gestures and the patient is safe: execute nothing.\n\n"
        f"Return ONLY valid JSON matching this schema:\n"
        "{\n"
        '  "assessment": "<factual description>",\n'
        '  "tools_to_execute": {\n'
        '    "whatsapp_alert": {"execute": true|false, "message": "<short text>"},\n'
        '    "voice_call": {"execute": true|false, "reason": "<urgency reason>"},\n'
        '    "comfort_music": {"execute": true|false}\n'
        '  },\n'
        '  "spoken_code": "CONFIRM_WATER" | "CONFIRM_FOOD" | "CONFIRM_TOILET" | "EMERGENCY_DISPATCHED" | "COMFORT_WAITING" | "NONE"\n'
        "}"
    )

    plan = {
        "assessment": f"Fallback. Gesture: {gesture_input}",
        "tools_to_execute": {
            "whatsapp_alert": {"execute": (gesture_input != "NONE"), "message": f"{gesture_input} requested."},
            "voice_call": {"execute": False, "reason": ""},
            "comfort_music": {"execute": False}
        },
        "spoken_code": "NONE"
    }

    if ai_client:
        try:
            app.logger.info(f"[{alert_id}] Requesting Gemini...")
            t0 = time.time()
            chat = ai_client.chats.create(
                model=GEMINI_MODEL,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.0,
                    max_output_tokens=1200,
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                ),
            )
            response = chat.send_message(
                [types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"), prompt]
            )
            raw_text = (response.text or "").strip()
            app.logger.info(f"[{alert_id}] Gemini in {time.time()-t0:.1f}s")
            parsed = None
            try:
                parsed = json.loads(raw_text)
            except json.JSONDecodeError:
                first = raw_text.find("{")
                last = raw_text.rfind("}")
                if first != -1 and last > first:
                    try:
                        parsed = json.loads(raw_text[first:last + 1])
                    except json.JSONDecodeError:
                        pass
            if isinstance(parsed, dict) and parsed:
                plan.update(parsed)
        except Exception as e:
            app.logger.error(f"[Gemini Error] {type(e).__name__}: {e}")
            app.logger.error(traceback.format_exc())

    if gesture_input == "FIST":
        app.logger.info(f"[{alert_id}] FIST override.")
        plan["assessment"] = "Emergency fist gesture."
        plan["tools_to_execute"] = {
            "whatsapp_alert": {"execute": True, "message": "CRITICAL: Emergency fist gesture."},
            "voice_call": {"execute": True, "reason": "Emergency fist gesture detected."},
            "comfort_music": {"execute": True}
        }
        plan["spoken_code"] = "EMERGENCY_DISPATCHED"

    tools = plan.get("tools_to_execute", {})
    is_emergency = bool(tools.get("voice_call", {}).get("execute", False))
    wants_whatsapp = bool(tools.get("whatsapp_alert", {}).get("execute", False))

    status = "ESCALATED" if is_emergency else ("PENDING" if wants_whatsapp else "RESOLVED")
    any_alert = is_emergency or wants_whatsapp

    with alerts_lock:
        alerts[alert_id] = {
            "alert_id": alert_id,
            "created_at": datetime.utcnow().isoformat(),
            "gesture": gesture_input,
            "status": status,
            "assessment": plan.get("assessment", ""),
            "image": f"/uploads/{fname}"
        }

    if any_alert:
        # Turn on the global processing lock for 5 minutes
        lock_on(PROCESSING_LOCK_SEC)
        # Turn on the speaker — it stays on until patient presses STOP
        speaker_on("Emergency detected. Please stay calm. Help is coming.")
        app.logger.info(f"[{alert_id}] EVENT RAISED — emergency={is_emergency}. Lock ON, Speaker ON.")

    if is_emergency:
        reason = tools.get("voice_call", {}).get("reason", "Critical alert.")
        ok_call = tool_send_voice_call(reason)
        push_feed({
            "time": datetime.utcnow().strftime("%H:%M:%S"),
            "type": "Twilio Voice",
            "message": f"Emergency call {'dispatched' if ok_call else 'FAILED'} immediately."
        })
        msg = tools.get("whatsapp_alert", {}).get("message", "Critical alert.")
        ok_wa = tool_send_whatsapp(msg)
        push_feed({
            "time": datetime.utcnow().strftime("%H:%M:%S"),
            "type": "Meta WhatsApp",
            "message": f"Emergency WhatsApp {'sent' if ok_wa else 'FAILED'} immediately."
        })

    elif wants_whatsapp:
        msg = tools.get("whatsapp_alert", {}).get("message", "Patient needs attention.")
        ok_wa = tool_send_whatsapp(msg)
        push_feed({
            "time": datetime.utcnow().strftime("%H:%M:%S"),
            "type": "Meta WhatsApp",
            "message": f"WhatsApp {'sent' if ok_wa else 'FAILED'} immediately."
        })

    if any_alert:
        threading.Thread(
            target=escalation_timer,
            args=(alert_id, tools.get("whatsapp_alert", {}).get("message", "Patient needs attention."), is_emergency),
            daemon=True
        ).start()

    music_url = tool_get_jamendo_music() if tools.get("comfort_music", {}).get("execute", False) else None
    if music_url:
        push_feed({"time": datetime.utcnow().strftime("%H:%M:%S"), "type": "Jamendo API", "message": "Comfort Music Deployed."})

    if gesture_input != "NONE":
        tool_send_to_webhook(alert_id, gesture_input, plan.get("assessment", ""), raw_b64)
        push_feed({"time": datetime.utcnow().strftime("%H:%M:%S"), "type": "Make.com", "message": "Backed up to G-Drive."})

    if status == "RESOLVED":
        push_feed({"time": datetime.utcnow().strftime("%H:%M:%S"), "type": "WATCHDOG", "message": "Routine check safe."})

    return jsonify({
        "alert_id": alert_id,
        "assessment": plan.get("assessment"),
        "spoken_code": plan.get("spoken_code", "NONE"),
        "play_music_url": music_url,
        "locked": False
    })


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

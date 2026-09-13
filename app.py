import os
import io
import time
import base64
import json
import threading
import requests
import logging
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

# ─── Configs ───
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")   # faster vision model
TWILIO_SID     = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN   = os.getenv("TWILIO_AUTH_TOKEN")

# ─── ESCALATION MATRIX (Thing 2) ───
WHATSAPP_AFTER_SEC = 120   # 2 minutes
VOICE_AFTER_SEC    = 300   # 5 minutes

ai_client     = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
twilio_client = TwilioClient(TWILIO_SID, TWILIO_TOKEN) if (TWILIO_SID and TWILIO_TOKEN) else None

alerts       = {}
alerts_lock  = threading.Lock()
events_feed  = []
feed_lock    = threading.Lock()

# ─── Bluetooth speaker command queue (dashboard polls this) ───
speaker_queue = []
speaker_lock  = threading.Lock()


def push_feed(entry):
    with feed_lock:
        events_feed.insert(0, entry)
        del events_feed[100:]


def push_speaker_command(cmd):
    with speaker_lock:
        speaker_queue.append({**cmd, "ts": time.time()})


# ═══════════════════════════════════════════════════════════════
# TOOL 1 — META WHATSAPP
# ═══════════════════════════════════════════════════════════════
def tool_send_whatsapp(message):
    token     = os.getenv("META_WHATSAPP_TOKEN")
    phone_id  = os.getenv("META_PHONE_NUMBER_ID")
    to_number = os.getenv("FAMILY_WHATSAPP_TO")

    if not token or not phone_id:
        app.logger.warning("[WhatsApp] Missing Meta API credentials.")
        return False

    url = f"https://graph.facebook.com/v19.0/{phone_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": f"🚨 Care Agent Alert\n{message}\nPlease check the dashboard."}
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        if response.status_code in [200, 201]:
            app.logger.info("[WhatsApp] Sent successfully via Meta Graph API.")
            return True
        app.logger.error(f"[WhatsApp Error] {response.text}")
        return False
    except Exception as e:
        app.logger.error(f"[WhatsApp Exception] {e}")
        return False


# ═══════════════════════════════════════════════════════════════
# TOOL 2 — TWILIO VOICE
# ═══════════════════════════════════════════════════════════════
def tool_send_voice_call(reason):
    if not twilio_client:
        app.logger.warning("[Voice] Missing Twilio credentials.")
        return False
    try:
        twiml = f"<Response><Say voice='alice'>Emergency Alert. {reason}. Please check the dashboard immediately.</Say></Response>"
        call = twilio_client.calls.create(
            twiml=twiml,
            to=os.getenv("FAMILY_PHONE_TO"),
            from_=os.getenv("TWILIO_VOICE_FROM")
        )
        app.logger.info(f"[Voice] Twilio call dispatched. SID: {call.sid}")
        return True
    except Exception as e:
        app.logger.error(f"[Voice Error] {e}")
        return False


# ═══════════════════════════════════════════════════════════════
# TOOL 3 — JAMENDO MUSIC
# ═══════════════════════════════════════════════════════════════
def tool_get_jamendo_music():
    client_id = os.getenv("JAMENDO_CLIENT_ID")
    if not client_id:
        return "https://prod-1.storage.jamendo.com/?trackid=1890757&format=mp31"
    try:
        url = f"https://api.jamendo.com/v3.0/tracks/?client_id={client_id}&format=json&tags=ambient,relaxing&limit=1"
        response = requests.get(url, timeout=5)
        data = response.json()
        if data.get("results") and len(data["results"]) > 0:
            return data["results"][0]["audio"]
        return "https://prod-1.storage.jamendo.com/?trackid=1890757&format=mp31"
    except Exception as e:
        app.logger.error(f"[Jamendo Error] {e}")
        return "https://prod-1.storage.jamendo.com/?trackid=1890757&format=mp31"


# ═══════════════════════════════════════════════════════════════
# TOOL 4 — MAKE.COM WEBHOOK
# ═══════════════════════════════════════════════════════════════
def tool_send_to_webhook(alert_id, gesture, assessment, raw_b64):
    webhook_url = os.getenv("MAKE_WEBHOOK_URL")
    if not webhook_url:
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
        response = requests.post(webhook_url, json=payload, timeout=5)
        if response.status_code in [200, 201]:
            app.logger.info("[Webhook] Data sent to Make.com successfully.")
            return True
        app.logger.error(f"[Webhook Error] {response.text}")
        return False
    except Exception as e:
        app.logger.error(f"[Webhook Exception] {e}")
        return False


# ═══════════════════════════════════════════════════════════════
# ESCALATION MATRIX  (Thing 2)
# ─────────────────────────────────────────────────────────────
#  t = 0s     → Bluetooth speaker alert sound
#  t = 120s   → WhatsApp to family   (if not RESOLVED)
#  t = 300s   → Twilio voice call    (if not RESOLVED)
# ═══════════════════════════════════════════════════════════════
def escalation_ladder(alert_id, message, gesture):
    app.logger.info(f"[{alert_id}] Ladder started (whatsapp@{WHATSAPP_AFTER_SEC}s, voice@{VOICE_AFTER_SEC}s)")

    # STEP 1 — Bluetooth speaker alert (t=0)
    push_speaker_command({
        "action":   "play_alert",
        "alert_id": alert_id,
        "gesture":  gesture,
        "message":  message,
    })
    push_feed({
        "time":    datetime.utcnow().strftime("%H:%M:%S"),
        "type":    "Bluetooth Speaker",
        "message": f"Alert sound sent to caregiver speaker ({gesture})."
    })

    start = time.time()

    # STEP 2 — wait until 2 min → WhatsApp
    while time.time() - start < WHATSAPP_AFTER_SEC:
        time.sleep(2)
        with alerts_lock:
            alert = alerts.get(alert_id)
            if not alert or alert["status"] == "RESOLVED":
                app.logger.info(f"[{alert_id}] RESOLVED before WhatsApp — ladder stopped.")
                push_feed({
                    "time":    datetime.utcnow().strftime("%H:%M:%S"),
                    "type":    "Dashboard",
                    "message": f"Caregiver came. Ladder stopped for {alert_id}."
                })
                return

    with alerts_lock:
        alert = alerts.get(alert_id)
        if alert:
            alert["status"] = "WHATSAPP_SENT"

    tool_send_whatsapp(message)
    push_feed({
        "time":    datetime.utcnow().strftime("%H:%M:%S"),
        "type":    "Meta WhatsApp",
        "message": "2 min passed. WhatsApp sent to family."
    })

    # STEP 3 — wait until 5 min → Voice call
    while time.time() - start < VOICE_AFTER_SEC:
        time.sleep(2)
        with alerts_lock:
            alert = alerts.get(alert_id)
            if not alert or alert["status"] == "RESOLVED":
                app.logger.info(f"[{alert_id}] RESOLVED before voice call — ladder stopped.")
                push_feed({
                    "time":    datetime.utcnow().strftime("%H:%M:%S"),
                    "type":    "Dashboard",
                    "message": f"Caregiver came. Voice call cancelled for {alert_id}."
                })
                return

    with alerts_lock:
        alert = alerts.get(alert_id)
        if alert:
            alert["status"] = "VOICE_CALLED"

    tool_send_voice_call(f"Emergency gesture: {gesture}. {message}")
    push_feed({
        "time":    datetime.utcnow().strftime("%H:%M:%S"),
        "type":    "Twilio Voice",
        "message": "5 min passed. Voice call dispatched to family."
    })


# ═══════════════════════════════════════════════════════════════
# VIEWS & ROUTES
# ═══════════════════════════════════════════════════════════════
@app.route("/", methods=["GET", "POST"])
def patient_view():
    return render_template("patient.html")


@app.route("/dashboard", methods=["GET", "POST"])
def dashboard_view():
    return render_template("dashboard.html")


@app.route("/uploads/<path:filename>", methods=["GET", "POST"])
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


@app.route("/api/v1/process-frame", methods=["GET", "POST"])
def process_frame():
    data          = request.get_json(force=True, silent=True) or {}
    snapshot_b64  = data.get("snapshot")
    gesture_input = data.get("gesture", "NONE").upper()

    if not snapshot_b64:
        return jsonify({"error": "snapshot missing"}), 400

    alert_id = f"ALT-{int(time.time())}"
    fname    = f"{alert_id}.jpg"
    fpath    = UPLOAD_DIR / fname

    try:
        raw_b64     = snapshot_b64.split(",")[1] if "," in snapshot_b64 else snapshot_b64
        image_bytes = base64.b64decode(raw_b64)
        Image.open(io.BytesIO(image_bytes)).convert("RGB").save(fpath, "JPEG", quality=80)
    except Exception as e:
        app.logger.error(f"Image save failed: {e}")
        return jsonify({"error": f"Image decode failed: {e}"}), 400

    # ═══════════════════════════════════════════════════════════
    # GEMINI PROMPT — SHORT (Thing 1: faster processing)
    # ═══════════════════════════════════════════════════════════
    prompt = (
        f"Care agent. Gesture: '{gesture_input}'. Look at image.\n"
        f"FIST or fall → voice_call+whatsapp+music.\n"
        f"WATER/FOOD/TOILET → whatsapp only.\n"
        f"NONE & safe → nothing.\n"
        f"If patient looks unwell, slumped, or in distress → treat as FIST.\n"
        f"JSON only:\n"
        "{\n"
        '  "assessment": "<short>",\n'
        '  "tools_to_execute": {\n'
        '    "whatsapp_alert": {"execute": bool, "message": "<text>"},\n'
        '    "voice_call": {"execute": bool, "reason": "<text>"},\n'
        '    "comfort_music": {"execute": bool}\n'
        '  },\n'
        '  "spoken_code": "CONFIRM_WATER"|"CONFIRM_FOOD"|"CONFIRM_TOILET"|"EMERGENCY_DISPATCHED"|"NONE"\n'
        "}"
    )

    plan = {
        "assessment": f"Fallback. Gesture: {gesture_input}",
        "tools_to_execute": {
            "whatsapp_alert": {"execute": (gesture_input != "NONE"), "message": f"{gesture_input} requested."},
            "voice_call":     {"execute": (gesture_input == "FIST"), "reason": "Emergency gesture."},
            "comfort_music":  {"execute": (gesture_input == "FIST")}
        },
        "spoken_code": "EMERGENCY_DISPATCHED" if gesture_input == "FIST" else "NONE"
    }

    # ═══════════════════════════════════════════════════════════
    # GEMINI CALL — TIMED + CAPPED (Thing 1: 8-second target)
    # ═══════════════════════════════════════════════════════════
    if ai_client:
        try:
            t0 = time.time()
            app.logger.info(f"[{alert_id}] Gemini call started...")
            response = ai_client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"), prompt],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                    max_output_tokens=180,   # cap output → faster
                ),
            )
            elapsed = time.time() - t0
            app.logger.info(f"[{alert_id}] Gemini responded in {elapsed:.1f}s")
            plan = json.loads(response.text.strip())
            app.logger.info(f"[{alert_id}] Agent Plan: {plan}")
        except Exception as e:
            app.logger.error(f"[Gemini Error] {e}")

    tools        = plan.get("tools_to_execute", {})
    is_emergency = tools.get("voice_call", {}).get("execute", False)
    needs_alert  = tools.get("whatsapp_alert", {}).get("execute", False) or is_emergency
    status       = "PENDING" if needs_alert else "RESOLVED"

    with alerts_lock:
        alerts[alert_id] = {
            "alert_id":   alert_id,
            "created_at": datetime.utcnow().isoformat(),
            "gesture":    gesture_input,
            "status":     status,
            "assessment": plan.get("assessment", ""),
            "image":      f"/uploads/{fname}"
        }

    # ─── Execute Tools ───
    if needs_alert:
        push_feed({
            "time":    datetime.utcnow().strftime("%H:%M:%S"),
            "type":    "Dashboard",
            "message": "Pop-up triggered. Awaiting caregiver..."
        })
        threading.Thread(
            target=escalation_ladder,
            args=(
                alert_id,
                tools.get("whatsapp_alert", {}).get("message", "Patient needs attention."),
                gesture_input
            ),
            daemon=True
        ).start()

    music_url = tool_get_jamendo_music() if tools.get("comfort_music", {}).get("execute", False) else None
    if music_url:
        push_feed({
            "time":    datetime.utcnow().strftime("%H:%M:%S"),
            "type":    "Jamendo API",
            "message": "Comfort Music Deployed."
        })

    if gesture_input != "NONE":
        tool_send_to_webhook(alert_id, gesture_input, plan.get("assessment", ""), raw_b64)
        push_feed({
            "time":    datetime.utcnow().strftime("%H:%M:%S"),
            "type":    "Make.com",
            "message": "Backed up to G-Drive."
        })

    if status == "RESOLVED":
        push_feed({
            "time":    datetime.utcnow().strftime("%H:%M:%S"),
            "type":    "WATCHDOG",
            "message": "Routine check safe."
        })

    return jsonify({
        "alert_id":       alert_id,
        "assessment":     plan.get("assessment"),
        "spoken_code":    plan.get("spoken_code", "NONE"),
        "play_music_url": music_url
    })


@app.route("/api/v1/acknowledge", methods=["GET", "POST"])
def acknowledge_alert():
    data = request.get_json(force=True, silent=True) or {}
    with alerts_lock:
        alert = alerts.get(data.get("alert_id"))
        if alert:
            alert["status"] = "RESOLVED"
            app.logger.info(f"Alert {data.get('alert_id')} marked RESOLVED.")
    return jsonify({"status": "SUCCESS"})


@app.route("/api/v1/speaker", methods=["GET", "POST"])
def speaker_commands():
    with speaker_lock:
        cmds = list(speaker_queue)
        speaker_queue.clear()
    return jsonify({"commands": cmds})


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

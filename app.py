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

# ---------------- CONFIG ----------------
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

# ---------------- TIMING ----------------
ESCALATION_WINDOW_SEC  = 120     # speaker repeats for 2 min
ESCALATION_WHATSAPP_AT = 120     # WhatsApp at 2 min
ESCALATION_CALL_AT     = 300     # Twilio call at 5 min
PROCESSING_LOCK_SEC    = 300     # STOP processing for 5 min after an event

# ---------------- BOOT DIAGNOSTICS ----------------
app.logger.info("=" * 60)
app.logger.info("[BOOT] Care Agent starting")
app.logger.info(f"[BOOT] GEMINI_MODEL        = {GEMINI_MODEL}")
app.logger.info(f"[BOOT] GEMINI_API_KEY set  = {bool(GEMINI_API_KEY)}")
app.logger.info(f"[BOOT] ai_client           = {'OK' if ai_client else 'MISSING'}")
app.logger.info(f"[BOOT] twilio_client       = {'OK' if twilio_client else 'MISSING'}")
app.logger.info(f"[BOOT] TWILIO_VOICE_FROM   = {TWILIO_FROM}")
app.logger.info(f"[BOOT] FAMILY_PHONE_TO     = {FAMILY_PHONE}")
app.logger.info(f"[BOOT] META token set      = {bool(os.getenv('META_WHATSAPP_TOKEN'))}")
app.logger.info(f"[BOOT] META phone_id set   = {bool(os.getenv('META_PHONE_NUMBER_ID'))}")
app.logger.info(f"[BOOT] JAMENDO_CLIENT_ID   = {bool(os.getenv('JAMENDO_CLIENT_ID'))}")
app.logger.info(f"[BOOT] MAKE_WEBHOOK_URL    = {'set' if os.getenv('MAKE_WEBHOOK_URL') else 'MISSING'}")
app.logger.info("=" * 60)


def push_feed(entry):
    with feed_lock:
        events_feed.insert(0, entry)
        del events_feed[100:]
    app.logger.info(f"[FEED] {entry.get('type')}: {entry.get('message')}")


# ---------------- TOOL 1: META WHATSAPP ----------------
def tool_send_whatsapp(message):
    app.logger.info("[WhatsApp] Attempting to send...")
    token = os.getenv("META_WHATSAPP_TOKEN")
    phone_id = os.getenv("META_PHONE_NUMBER_ID")
    to_number = os.getenv("FAMILY_WHATSAPP_TO")

    if not token or not phone_id:
        app.logger.warning("[WhatsApp] Missing Meta API credentials.")
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
        app.logger.info(f"[WhatsApp] HTTP {r.status_code} :: {r.text[:200]}")
        return r.status_code in [200, 201]
    except Exception as e:
        app.logger.error(f"[WhatsApp Exception] {e}")
        return False


# ---------------- TOOL 2: TWILIO VOICE ----------------
def tool_send_voice_call(reason):
    app.logger.info(f"[Voice] Attempting Twilio call. Reason: {reason}")
    app.logger.info(f"[Voice] twilio_client={'OK' if twilio_client else 'MISSING'}")
    app.logger.info(f"[Voice] FROM={TWILIO_FROM} TO={FAMILY_PHONE}")

    if not twilio_client:
        app.logger.warning("[Voice] Missing Twilio credentials.")
        return False
    if not TWILIO_FROM or not FAMILY_PHONE:
        app.logger.warning("[Voice] Missing TWILIO_VOICE_FROM or FAMILY_PHONE_TO.")
        return False

    try:
        twiml = f"<Response><Say voice='alice'>Emergency Alert. Assessment: {reason}. Please check the dashboard immediately.</Say></Response>"
        call = twilio_client.calls.create(
            twiml=twiml,
            to=FAMILY_PHONE,
            from_=TWILIO_FROM
        )
        app.logger.info(f"[Voice] ✅ Twilio call dispatched. SID: {call.sid}")
        return True
    except Exception as e:
        app.logger.error(f"[Voice Error] {type(e).__name__}: {e}")
        app.logger.error(traceback.format_exc())
        return False


# ---------------- TOOL 3: JAMENDO MUSIC ----------------
def tool_get_jamendo_music():
    client_id = os.getenv("JAMENDO_CLIENT_ID")
    fallback = "https://prod-1.storage.jamendo.com/?trackid=1890757&format=mp31"
    if not client_id:
        app.logger.warning("[Jamendo] No Client ID. Using fallback.")
        return fallback
    try:
        url = f"https://api.jamendo.com/v3.0/tracks/?client_id={client_id}&format=json&tags=ambient,relaxing&limit=1"
        r = requests.get(url, timeout=5)
        data = r.json()
        if data.get("results"):
            app.logger.info("[Jamendo] Live track retrieved.")
            return data["results"][0]["audio"]
        return fallback
    except Exception as e:
        app.logger.error(f"[Jamendo Error] {e}")
        return fallback


# ---------------- TOOL 4: MAKE.COM WEBHOOK ----------------
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
        app.logger.info(f"[Webhook] HTTP {r.status_code} :: {r.text[:200]}")
        return r.status_code in [200, 201, 202]
    except Exception as e:
        app.logger.error(f"[Webhook Exception] {e}")
        return False


# ---------------- ESCALATION TIMER ----------------
def escalation_timer(alert_id, message):
    start = time.time()
    whatsapp_sent = False
    call_sent = False

    app.logger.info(f"[ESCALATION {alert_id}] Timer started. Window={ESCALATION_WINDOW_SEC}s, WhatsApp@{ESCALATION_WHATSAPP_AT}s, Call@{ESCALATION_CALL_AT}s")
    push_feed({
        "time": datetime.utcnow().strftime("%H:%M:%S"),
        "type": "Bluetooth Speaker",
        "message": "Repeat loop started (20s interval, 2 min window)."
    })

    while True:
        time.sleep(1)
        elapsed = time.time() - start

        with alerts_lock:
            alert = alerts.get(alert_id)
            if not alert:
                app.logger.info(f"[ESCALATION {alert_id}] Alert gone. Exiting.")
                return
            resolved = alert["status"] == "RESOLVED"

        if resolved:
            with alerts_lock:
                if alert_id in alerts:
                    alerts[alert_id]["processing_locked"] = False
                    alerts[alert_id]["lock_until_ts"] = 0
            app.logger.info(f"[ESCALATION {alert_id}] Resolved by caregiver. Stopping.")
            push_feed({
                "time": datetime.utcnow().strftime("%H:%M:%S"),
                "type": "Caregiver",
                "message": "Acknowledged. Escalation cancelled."
            })
            return

        if not whatsapp_sent and elapsed >= ESCALATION_WHATSAPP_AT:
            whatsapp_sent = True
            app.logger.info(f"[ESCALATION {alert_id}] 2 min mark → sending WhatsApp ONCE")
            ok = tool_send_whatsapp(message)
            push_feed({
                "time": datetime.utcnow().strftime("%H:%M:%S"),
                "type": "Meta WhatsApp",
                "message": f"WhatsApp {'sent' if ok else 'FAILED'} at 2 min."
            })

        if not call_sent and elapsed >= ESCALATION_CALL_AT:
            call_sent = True
            app.logger.info(f"[ESCALATION {alert_id}] 5 min mark → placing Twilio call")
            ok = tool_send_voice_call(f"Unresolved emergency: {message}")
            push_feed({
                "time": datetime.utcnow().strftime("%H:%M:%S"),
                "type": "Twilio Voice",
                "message": f"Voice call {'dispatched' if ok else 'FAILED'} at 5 min."
            })
            # Release processing lock after 5 min
            with alerts_lock:
                if alert_id in alerts:
                    alerts[alert_id]["processing_locked"] = False
                    alerts[alert_id]["lock_until_ts"] = 0
            app.logger.info(f"[ESCALATION {alert_id}] 5 min elapsed. Lock released. Exiting.")
            return


# ---------------- VIEWS ----------------
@app.route("/", methods=["GET", "POST"])
def patient_view():
    return render_template("patient.html")

@app.route("/dashboard", methods=["GET", "POST"])
def dashboard_view():
    return render_template("dashboard.html")

@app.route("/uploads/<path:filename>", methods=["GET", "POST"])
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


# ---------------- PATIENT SPEAKER LOOP ----------------
@app.route("/api/v1/patient-loop", methods=["GET", "POST"])
def patient_loop():
    now = time.time()
    with alerts_lock:
        for a in alerts.values():
            if a["status"] == "RESOLVED":
                continue
            if a.get("speak_until_ts", 0) > now:
                return jsonify({
                    "speak": True,
                    "phrase": a.get("speak_phrase", "Emergency detected. Help is on the way."),
                    "alert_id": a["alert_id"],
                    "remaining": int(a["speak_until_ts"] - now)
                })
    return jsonify({"speak": False})


# ---------------- MAIN FRAME PROCESSOR ----------------
@app.route("/api/v1/process-frame", methods=["GET", "POST"])
def process_frame():
    data = request.get_json(force=True, silent=True) or {}
    snapshot_b64 = data.get("snapshot")
    gesture_input = data.get("gesture", "NONE").upper()

    app.logger.info(f"[FRAME] Received gesture={gesture_input}")

    if not snapshot_b64:
        return jsonify({"error": "snapshot missing"}), 400

    # ---- STOP PROCESSING FOR 5 MIN AFTER AN EVENT ----
    now = time.time()
    with alerts_lock:
        for a in alerts.values():
            if a["status"] == "RESOLVED":
                continue
            lock_until = a.get("lock_until_ts", 0)
            if lock_until > now:
                remaining = int(lock_until - now)
                app.logger.info(f"[FRAME] BLOCKED — alert {a['alert_id']} active. {remaining}s left on lock.")
                return jsonify({
                    "alert_id": a["alert_id"],
                    "assessment": f"Processing paused. {remaining}s left.",
                    "spoken_code": "NONE",
                    "play_music_url": None,
                    "locked": True,
                    "remaining": remaining
                })

    alert_id = f"ALT-{int(time.time())}"
    fname = f"{alert_id}.jpg"
    fpath = UPLOAD_DIR / fname

    try:
        raw_b64 = snapshot_b64.split(",")[1] if "," in snapshot_b64 else snapshot_b64
        image_bytes = base64.b64decode(raw_b64)
        Image.open(io.BytesIO(image_bytes)).convert("RGB").save(fpath, "JPEG", quality=80)
        app.logger.info(f"[FRAME] Image saved {fname}")
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
        "assessment": f"Fallback mode. Gesture: {gesture_input}",
        "tools_to_execute": {
            "whatsapp_alert": {"execute": (gesture_input != "NONE"), "message": f"{gesture_input} requested."},
            "voice_call": {"execute": False, "reason": ""},
            "comfort_music": {"execute": False}
        },
        "spoken_code": "NONE"
    }

    if ai_client:
        try:
            app.logger.info(f"[{alert_id}] Requesting Gemini reasoning...")
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
                [types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"), prompt],
                request_options={"timeout": 20}
            )
            raw_text = (response.text or "").strip()
            app.logger.info(f"[{alert_id}] Gemini responded in {time.time()-t0:.1f}s, {len(raw_text)} chars")

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
                app.logger.info(f"[{alert_id}] Plan: {json.dumps(plan)[:300]}")
        except Exception as e:
            app.logger.error(f"[Gemini Error] {type(e).__name__}: {e}")
            app.logger.error(traceback.format_exc())

    if gesture_input == "FIST":
        app.logger.info(f"[{alert_id}] FIST override applied.")
        plan["assessment"] = "Emergency fist gesture received."
        plan["tools_to_execute"] = {
            "whatsapp_alert": {"execute": True, "message": "CRITICAL: Patient sent an emergency fist gesture."},
            "voice_call": {"execute": True, "reason": "Emergency fist gesture detected."},
            "comfort_music": {"execute": True}
        }
        plan["spoken_code"] = "EMERGENCY_DISPATCHED"

    tools = plan.get("tools_to_execute", {})
    is_emergency = tools.get("voice_call", {}).get("execute", False)
    status = "ESCALATED" if is_emergency else ("PENDING" if tools.get("whatsapp_alert", {}).get("execute", False) else "RESOLVED")

    any_alert = (
        tools.get("whatsapp_alert", {}).get("execute", False)
        or tools.get("voice_call", {}).get("execute", False)
    )

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
            alerts[alert_id]["speak_until_ts"] = time.time() + ESCALATION_WINDOW_SEC
            alerts[alert_id]["speak_phrase"] = "Emergency detected. Please stay calm. Help is coming."
            alerts[alert_id]["processing_locked"] = True
            alerts[alert_id]["lock_until_ts"] = time.time() + PROCESSING_LOCK_SEC
            app.logger.info(f"[{alert_id}] EVENT RAISED — processing locked for {PROCESSING_LOCK_SEC}s.")

    if any_alert:
        push_feed({
            "time": datetime.utcnow().strftime("%H:%M:%S"),
            "type": "Dashboard",
            "message": "Alert raised. Speaker loop + escalation timer started."
        })
        threading.Thread(
            target=escalation_timer,
            args=(alert_id, tools.get("whatsapp_alert", {}).get("message", "Patient needs attention.")),
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


# ---------------- ACK ----------------
@app.route("/api/v1/acknowledge", methods=["GET", "POST"])
def acknowledge_alert():
    data = request.get_json(force=True, silent=True) or {}
    aid = data.get("alert_id")
    with alerts_lock:
        alert = alerts.get(aid)
        if alert:
            alert["status"] = "RESOLVED"
            alert["speak_until_ts"] = 0
            alert["processing_locked"] = False
            alert["lock_until_ts"] = 0
            app.logger.info(f"[ACK] Alert {aid} RESOLVED. Locks cleared.")
        else:
            app.logger.warning(f"[ACK] Unknown alert_id {aid}")
    return jsonify({"status": "SUCCESS"})


# ---------------- STATE ----------------
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

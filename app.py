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

# Gunicorn Logging Wiring
gunicorn_logger = logging.getLogger('gunicorn.error')
if gunicorn_logger.handlers:
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
else:
    app.logger.setLevel(logging.INFO)

UPLOAD_DIR = Path("static/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Configs
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")

ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
twilio_client = TwilioClient(TWILIO_SID, TWILIO_TOKEN) if (TWILIO_SID and TWILIO_TOKEN) else None

alerts = {}            
alerts_lock = threading.Lock()
events_feed = []       
feed_lock = threading.Lock()

def push_feed(entry):
    with feed_lock:
        events_feed.insert(0, entry)
        del events_feed[100:]

# --- TOOL 1: META WHATSAPP ---
def tool_send_whatsapp(message):
    token = os.getenv("META_WHATSAPP_TOKEN")
    phone_id = os.getenv("META_PHONE_NUMBER_ID")
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
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code in [200, 201]:
            app.logger.info("[WhatsApp] Sent successfully via Meta Graph API.")
            return True
        app.logger.error(f"[WhatsApp Error] {response.text}")
        return False
    except Exception as e:
        app.logger.error(f"[WhatsApp Exception] {e}")
        return False

# --- TOOL 2: TWILIO VOICE ---
def tool_send_voice_call(reason):
    if not twilio_client: 
        app.logger.warning("[Voice] Missing Twilio credentials.")
        return False
    try:
        twiml = f"<Response><Say voice='alice'>Emergency Alert. Assessment: {reason}. Please check the dashboard immediately.</Say></Response>"
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

# --- TOOL 3: JAMENDO MUSIC API ---
def tool_get_jamendo_music():
    client_id = os.getenv("JAMENDO_CLIENT_ID")
    if not client_id:
        app.logger.warning("[Jamendo] No Client ID provided. Using fallback track.")
        return "https://prod-1.storage.jamendo.com/?trackid=1890757&format=mp31"
        
    try:
        url = f"https://api.jamendo.com/v3.0/tracks/?client_id={client_id}&format=json&tags=ambient,relaxing&limit=1"
        response = requests.get(url, timeout=5)
        data = response.json()
        
        if data.get("results") and len(data["results"]) > 0:
            app.logger.info("[Jamendo] Live track retrieved via API.")
            return data["results"][0]["audio"]
            
        return "https://prod-1.storage.jamendo.com/?trackid=1890757&format=mp31"
    except Exception as e:
        app.logger.error(f"[Jamendo Error] {e}")
        return "https://prod-1.storage.jamendo.com/?trackid=1890757&format=mp31"

# --- TOOL 4: MAKE.COM WEBHOOK (GOOGLE DRIVE) ---
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
            app.logger.info("[Webhook] Data and image sent to Make.com successfully.")
            return True
        app.logger.error(f"[Webhook Error] Make.com rejected payload: {response.text}")
        return False
    except Exception as e:
        app.logger.error(f"[Webhook Exception] {e}")
        return False

# --- ESCALATION TIMER ---
def escalation_timer(alert_id, message):
    time.sleep(30)
    with alerts_lock:
        alert = alerts.get(alert_id)
        if alert and alert["status"] != "RESOLVED":
            tool_send_whatsapp(message)
            push_feed({"time": datetime.utcnow().strftime("%H:%M:%S"), "type": "Meta WhatsApp", "message": "Pop-up ignored. Escalated to WhatsApp."})

# --- VIEWS & ROUTES ---
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
    data = request.get_json(force=True, silent=True) or {}
    snapshot_b64 = data.get("snapshot")
    gesture_input = data.get("gesture", "NONE").upper() 

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
        app.logger.error(f"Image save failed: {e}")
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
            response = ai_client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"), prompt],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.0,
                    max_output_tokens=1200 # Increased to prevent truncation 
                )
            )
            raw_text = response.text.strip()
            
            parsed = None
            try:
                parsed = json.loads(raw_text)
            except json.JSONDecodeError as e:
                app.logger.error(f"[{alert_id}] JSON decode error: {e.msg}. Raw response: {raw_text!r}")
                first = raw_text.find("{")
                last = raw_text.rfind("}")
                if first != -1 and last > first:
                    candidate = raw_text[first:last + 1]
                    try:
                        parsed = json.loads(candidate)
                    except json.JSONDecodeError as nested_error:
                        app.logger.error(f"[{alert_id}] Extracted JSON decode error: {nested_error.msg}. Candidate: {candidate!r}")
            
            if isinstance(parsed, dict) and parsed:
                plan.update(parsed)
                
        except Exception as e:
            app.logger.error(f"[Gemini Error] {e}")

    # Deterministic FIST override
    if gesture_input == "FIST":
        plan["assessment"] = "Emergency fist gesture received."
        plan["tools_to_execute"] = {
            "whatsapp_alert": {"execute": True, "message": "CRITICAL: Patient sent an emergency fist gesture. Please check immediately."},
            "voice_call": {"execute": True, "reason": "Emergency fist gesture detected."},
            "comfort_music": {"execute": True}
        }
        plan["spoken_code"] = "EMERGENCY_DISPATCHED"

    tools = plan.get("tools_to_execute", {})
    is_emergency = tools.get("voice_call", {}).get("execute", False)
    status = "ESCALATED" if is_emergency else ("PENDING" if tools.get("whatsapp_alert", {}).get("execute", False) else "RESOLVED")

    with alerts_lock:
        alerts[alert_id] = {
            "alert_id": alert_id,
            "created_at": datetime.utcnow().isoformat(),
            "gesture": gesture_input,
            "status": status,
            "assessment": plan.get("assessment", ""),
            "image": f"/uploads/{fname}"
        }
    
    # --- Execute Tools ---
    if tools.get("whatsapp_alert", {}).get("execute", False):
        push_feed({"time": datetime.utcnow().strftime("%H:%M:%S"), "type": "Dashboard", "message": "Pop-up triggered. Awaiting caregiver..."})
        threading.Thread(
            target=escalation_timer, 
            args=(alert_id, tools["whatsapp_alert"].get("message", "Patient needs attention."))
        ).start()
        
    if is_emergency:
        tool_send_voice_call(tools["voice_call"].get("reason", "Critical alert."))
        push_feed({"time": datetime.utcnow().strftime("%H:%M:%S"), "type": "Twilio Voice", "message": "Escalation Dialed."})

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

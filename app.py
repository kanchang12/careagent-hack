import os
import io
import time
import base64
import json
import threading
import requests
from datetime import datetime
from pathlib import Path

from flask import Flask, request, jsonify, render_template, send_from_directory
from dotenv import load_dotenv
from PIL import Image
from google import genai
from google.genai import types
from twilio.rest import Client as TwilioClient

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "care-agent-v1")
UPLOAD_DIR = Path("static/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

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

def tool_send_whatsapp(message):
    token = os.getenv("META_WHATSAPP_TOKEN")
    phone_id = os.getenv("META_PHONE_NUMBER_ID")
    to_number = os.getenv("FAMILY_WHATSAPP_TO")
    
    if not token or not phone_id: 
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
        return response.status_code in [200, 201]
    except Exception as e:
        print(f"[Meta WhatsApp Exception] {e}")
        return False

def tool_send_voice_call(reason):
    if not twilio_client: return False
    try:
        twiml = f"<Response><Say voice='alice'>Emergency Alert. Assessment: {reason}. Please check the dashboard immediately.</Say></Response>"
        twilio_client.calls.create(
            twiml=twiml,
            to=os.getenv("FAMILY_PHONE_TO"),
            from_=os.getenv("TWILIO_VOICE_FROM")
        )
        return True
    except Exception as e:
        print(f"[Voice Error] {e}")
        return False

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
        return jsonify({"error": f"Image decode failed: {e}"}), 400

    prompt = (
        f"You are an autonomous Care Agent evaluating an elderly patient.\n"
        f"A local edge model detected gesture: '{gesture_input}'.\n"
        f"Analyze the image for context. Determine physical safety, posture, and distress.\n"
        f"Based on severity, generate an execution plan for your tools.\n"
        f"- If gesture is FIST or you see a fall: execute voice_call, whatsapp, and comfort_music.\n"
        f"- If gesture is WATER/FOOD/TOILET: execute whatsapp, NO voice call, NO music.\n"
        f"- If gesture is NONE and scene is safe: execute nothing.\n\n"
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
            "voice_call": {"execute": (gesture_input == "FIST"), "reason": "Emergency gesture."},
            "comfort_music": {"execute": (gesture_input == "FIST")}
        },
        "spoken_code": "EMERGENCY_DISPATCHED" if gesture_input == "FIST" else "NONE"
    }

    if ai_client:
        try:
            response = ai_client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"), prompt],
                config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.2)
            )
            plan = json.loads(response.text.strip())
        except Exception as e:
            print(f"[Gemini Error] {e}")

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
    
    if tools.get("whatsapp_alert", {}).get("execute", False):
        tool_send_whatsapp(tools["whatsapp_alert"].get("message", "Patient needs attention."))
        push_feed({"time": datetime.utcnow().strftime("%H:%M:%S"), "type": "Meta WhatsApp", "message": "Alert Dispatched."})
        
    if is_emergency:
        tool_send_voice_call(tools["voice_call"].get("reason", "Critical alert."))
        push_feed({"time": datetime.utcnow().strftime("%H:%M:%S"), "type": "Twilio Voice", "message": "Escalation Dialed."})

    music_url = "https://prod-1.storage.jamendo.com/?trackid=1890757&format=mp31" if tools.get("comfort_music", {}).get("execute", False) else None
    if music_url:
        push_feed({"time": datetime.utcnow().strftime("%H:%M:%S"), "type": "Jamendo API", "message": "Comfort Music Deployed."})

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
        if alert: alert["status"] = "RESOLVED"
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

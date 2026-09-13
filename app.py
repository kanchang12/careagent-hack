import os
import io
import time
import base64
import json
import threading
from datetime import datetime
from pathlib import Path

from flask import Flask, request, jsonify, render_template, send_from_directory, Response
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

# Auth & Config
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL") # Alias for latest Flash
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")

ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
twilio_client = TwilioClient(TWILIO_SID, TWILIO_TOKEN) if (TWILIO_SID and TWILIO_TOKEN) else None

# In-Memory Database for Demo
alerts = {}            
alerts_lock = threading.Lock()
events_feed = []       
feed_lock = threading.Lock()

def push_feed(entry):
    with feed_lock:
        events_feed.insert(0, entry)
        del events_feed[100:]

# --- TOOL EXECUTORS (The Mechanical Arms) ---

def tool_send_whatsapp(message, image_path=None):
    if not twilio_client: return False
    try:
        twilio_client.messages.create(
            from_=os.getenv("TWILIO_WHATSAPP_FROM"),
            body=f"🚨 Care Agent Alert\n{message}",
            to=os.getenv("FAMILY_WHATSAPP_TO")
        )
        return True
    except Exception as e:
        print(f"[WhatsApp Error] {e}")
        return False

def tool_send_voice_call(reason):
    if not twilio_client: return False
    try:
        twiml = (
            f"<Response><Say voice='alice'>Emergency Alert. Assessment: {reason}. "
            f"Please check the dashboard immediately.</Say></Response>"
        )
        twilio_client.calls.create(
            twiml=twiml,
            to=os.getenv("FAMILY_PHONE_TO"),
            from_=os.getenv("TWILIO_VOICE_FROM")
        )
        return True
    except Exception as e:
        print(f"[Voice Error] {e}")
        return False

# --- WEB VIEWS ---

@app.route("/")
def patient_view():
    return render_template("patient.html")

@app.route("/dashboard")
def dashboard_view():
    return render_template("dashboard.html")

@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)

# --- THE AGENTIC ORCHESTRATOR ---

@app.route("/api/v1/process-frame",  methods=["POST", "GET"])
def process_frame():
    data = request.get_json(force=True, silent=True) or {}
    snapshot_b64 = data.get("snapshot")
    gesture_input = data.get("gesture", "NONE").upper() 

    if not snapshot_b64:
        return jsonify({"error": "snapshot missing"}), 400

    alert_id = f"ALT-{int(time.time())}"
    fname = f"{alert_id}.jpg"
    fpath = UPLOAD_DIR / fname

    # Decode and save image
    try:
        raw_b64 = snapshot_b64.split(",")[1] if "," in snapshot_b64 else snapshot_b64
        image_bytes = base64.b64decode(raw_b64)
        Image.open(io.BytesIO(image_bytes)).convert("RGB").save(fpath, "JPEG", quality=80)
    except Exception as e:
        return jsonify({"error": f"Image decode failed: {e}"}), 400

    # AGENT PROMPT: Forcing tool selection via JSON schema
    prompt = (
        f"You are an autonomous Care Agent evaluating an elderly patient.\n"
        f"A local edge model detected gesture: '{gesture_input}'.\n"
        f"Analyze the image for context. Are they falling? Are they safe in a chair? Are they requesting food/water?\n\n"
        f"Based on the severity, generate an execution plan for your external tools.\n"
        f"- If gesture is FIST or you see a fall: execute voice_call, whatsapp, and comfort_music.\n"
        f"- If gesture is WATER/FOOD/TOILET: execute whatsapp, but NO voice call and NO music.\n"
        f"- If gesture is NONE and scene is safe: execute nothing.\n\n"
        f"Return ONLY valid JSON matching this schema:\n"
        "{\n"
        '  "assessment": "<factual description of what you see>",\n'
        '  "tools_to_execute": {\n'
        '    "whatsapp_alert": {"execute": true|false, "message": "<short alert text>"},\n'
        '    "voice_call": {"execute": true|false, "reason": "<urgency reason>"},\n'
        '    "comfort_music": {"execute": true|false}\n'
        '  },\n'
        '  "spoken_code": "CONFIRM_WATER" | "CONFIRM_FOOD" | "CONFIRM_TOILET" | "EMERGENCY_DISPATCHED" | "COMFORT_WAITING" | "NONE"\n'
        "}"
    )

    # Default fallback plan if API fails
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
            print(f"[Gemini Error] Using fallback: {e}")

    # Determine status
    tools = plan.get("tools_to_execute", {})
    is_emergency = tools.get("voice_call", {}).get("execute", False)
    status = "ESCALATED" if is_emergency else ("PENDING" if tools.get("whatsapp_alert", {}).get("execute", False) else "RESOLVED")

    # Save state
    with alerts_lock:
        alerts[alert_id] = {
            "alert_id": alert_id,
            "created_at": datetime.utcnow().isoformat(),
            "gesture": gesture_input,
            "status": status,
            "assessment": plan.get("assessment", ""),
            "image": f"/uploads/{fname}"
        }

    # === EXECUTE THE AGENT'S PLAN ===
    
    # Tool 1: WhatsApp
    if tools.get("whatsapp_alert", {}).get("execute", False):
        tool_send_whatsapp(tools["whatsapp_alert"].get("message", "Patient needs attention."))
        push_feed({"time": datetime.utcnow().strftime("%H:%M:%S"), "type": "APP 1", "message": "WhatsApp Dispatched."})
        
    # Tool 2: Twilio Voice
    if is_emergency:
        tool_send_voice_call(tools["voice_call"].get("reason", "Critical alert."))
        push_feed({"time": datetime.utcnow().strftime("%H:%M:%S"), "type": "APP 2", "message": "Voice Escalation Dialed."})

    # Tool 3: Jamendo Music (URL returned to frontend)
    music_url = "https://prod-1.storage.jamendo.com/?trackid=1890757&format=mp31" if tools.get("comfort_music", {}).get("execute", False) else None
    if music_url:
        push_feed({"time": datetime.utcnow().strftime("%H:%M:%S"), "type": "APP 3", "message": "Comfort Music Deployed."})

    if status == "RESOLVED":
        push_feed({"time": datetime.utcnow().strftime("%H:%M:%S"), "type": "WATCHDOG", "message": "Routine check safe."})

    return jsonify({
        "alert_id": alert_id,
        "assessment": plan.get("assessment"),
        "spoken_code": plan.get("spoken_code", "NONE"),
        "play_music_url": music_url
    })

# --- DASHBOARD ENDPOINTS ---

@app.route("/api/v1/acknowledge",  methods=["POST", "GET"])
def acknowledge_alert():
    data = request.get_json(force=True, silent=True) or {}
    with alerts_lock:
        alert = alerts.get(data.get("alert_id"))
        if alert: alert["status"] = "RESOLVED"
    return jsonify({"status": "SUCCESS"})

@app.route("/api/v1/state")
def get_state():
    with alerts_lock:
        active = [a for a in alerts.values() if a["status"] != "RESOLVED"]
        recent = sorted(alerts.values(), key=lambda x: x["created_at"], reverse=True)[:10]
    with feed_lock:
        feed = events_feed[:20]
    return jsonify({"active_alerts": active, "recent_images": recent, "feed": feed})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False, threaded=True)

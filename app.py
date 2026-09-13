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

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
TWILIO_SID     = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN   = os.getenv("TWILIO_AUTH_TOKEN")

WHATSAPP_AFTER_SEC = 120
VOICE_AFTER_SEC    = 300

ai_client     = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
twilio_client = TwilioClient(TWILIO_SID, TWILIO_TOKEN) if (TWILIO_SID and TWILIO_TOKEN) else None

alerts      = {}
alerts_lock = threading.Lock()
events_feed = []
feed_lock   = threading.Lock()

speaker_queue = []
speaker_lock  = threading.Lock()


def push_feed(entry):
    with feed_lock:
        events_feed.insert(0, entry)
        del events_feed[100:]


def push_speaker_command(cmd):
    with speaker_lock:
        speaker_queue.append({**cmd, "ts": time.time()})


def tool_send_whatsapp(message):
    token     = os.getenv("META_WHATSAPP_TOKEN")
    phone_id  = os.getenv("META_PHONE_NUMBER_ID")
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
        if r.status_code in [200, 201]:
            app.logger.info("[WhatsApp] Sent ✅")
            return True
        app.logger.error(f"[WhatsApp Error] {r.text}")
        return False
    except Exception as e:
        app.logger.error(f"[WhatsApp Exception] {e}")
        return False


def tool_send_voice_call(reason):
    if not twilio_client:
        app.logger.warning("[Voice] Missing credentials.")
        return False
    try:
        twiml = f"<Response><Say voice='alice'>Emergency Alert. {reason}. Please check the dashboard immediately.</Say></Response>"
        call = twilio_client.calls.create(
            twiml=twiml,
            to=os.getenv("FAMILY_PHONE_TO"),
            from_=os.getenv("TWILIO_VOICE_FROM")
        )
        app.logger.info(f"[Voice] Call dispatched. SID: {call.sid}")
        return True
    except Exception as e:
        app.logger.error(f"[Voice Error] {e}")
        return False


def tool_get_jamendo_music():
    client_id = os.getenv("JAMENDO_CLIENT_ID")
    if not client_id:
        return "https://prod-1.storage.jamendo.com/?trackid=1890757&format=mp31"
    try:
        url = f"https://api.jamendo.com/v3.0/tracks/?client_id={client_id}&format=json&tags=ambient,relaxing&limit=1"
        r = requests.get(url, timeout=5)
        data = r.json()
        if data.get("results") and len(data["results"]) > 0:
            return data["results"][0]["audio"]
        return "https://prod-1.storage.jamendo.com/?trackid=1890757&format=mp31"
    except Exception as e:
        app.logger.error(f"[Jamendo Error] {e}")
        return "https://prod-1.storage.jamendo.com/?trackid=1890757&format=mp31"


def tool_send_to_webhook(
    alert_id,
    gesture,
    assessment,
    raw_b64,
    plan=None
):
    webhook_url = (os.getenv("MAKE_WEBHOOK_URL") or "").strip()

    if not webhook_url:
        app.logger.error("[Make] MAKE_WEBHOOK_URL is missing or empty.")
        return {
            "success": False,
            "error": "MAKE_WEBHOOK_URL is missing"
        }

    plan = plan or {}

    payload = {
        "event_type": "care_agent_alert",
        "alert_id": alert_id,
        "timestamp_utc": datetime.utcnow().isoformat() + "Z",
        "gesture": gesture,
        "assessment": assessment,
        "emergency_level": plan.get("emergency_level", "NONE"),
        "incident_type": plan.get("incident_type", "UNKNOWN"),
        "details": plan.get("details", ""),
        "observations": plan.get("observations", []),
        "confidence": plan.get("confidence", 0),
        "needs_human_check": bool(
            plan.get("needs_human_check", False)
        ),
        "image_filename": f"{alert_id}.jpg",
        "image_mime_type": "image/jpeg",
        "image_base64": raw_b64
    }

    safe_url = (
        webhook_url[:45] + "..."
        if len(webhook_url) > 45
        else webhook_url
    )

    app.logger.info(
        f"[Make] Posting alert {alert_id} to webhook: {safe_url}"
    )

    try:
        response = requests.post(
            webhook_url,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Guardian-Angel-Care-Agent/1.0"
            },
            timeout=20
        )

        app.logger.info(
            f"[Make] Response for {alert_id}: "
            f"status={response.status_code}, "
            f"body={response.text[:1000]!r}"
        )

        if 200 <= response.status_code < 300:
            app.logger.info(
                f"[Make] Webhook accepted successfully for {alert_id}."
            )
            return {
                "success": True,
                "status_code": response.status_code,
                "response": response.text[:1000]
            }

        app.logger.error(
            f"[Make] Webhook rejected for {alert_id}: "
            f"HTTP {response.status_code} | {response.text[:1000]}"
        )

        return {
            "success": False,
            "status_code": response.status_code,
            "error": response.text[:1000]
        }

    except requests.Timeout:
        app.logger.error(
            f"[Make] Timeout sending webhook for {alert_id}."
        )
        return {
            "success": False,
            "error": "Request timed out"
        }

    except requests.RequestException as e:
        app.logger.exception(
            f"[Make] Network error sending webhook for {alert_id}: {e}"
        )
        return {
            "success": False,
            "error": str(e)
        }


def escalation_ladder(alert_id, message, gesture):
    REPEAT_EVERY_SEC = 20
    MAX_WAIT_SEC = 120

    app.logger.info(
        f"[{alert_id}] Escalation started: "
        f"speaker + WhatsApp every {REPEAT_EVERY_SEC}s for {MAX_WAIT_SEC}s"
    )

    start = time.time()
    reminder_number = 0

    while True:
        elapsed = time.time() - start

        # Stop immediately if dashboard caregiver clicked Okay.
        with alerts_lock:
            alert = alerts.get(alert_id)
            if not alert or alert["status"] == "RESOLVED":
                push_feed({
                    "time": datetime.utcnow().strftime("%H:%M:%S"),
                    "type": "Dashboard",
                    "message": f"Caregiver acknowledged {alert_id}. Speaker loop stopped."
                })
                app.logger.info(f"[{alert_id}] Resolved. Escalation cancelled.")
                return

        # No acknowledgement within 2 minutes: escalate to telephone call.
        if elapsed >= MAX_WAIT_SEC:
            with alerts_lock:
                alert = alerts.get(alert_id)
                if alert and alert["status"] != "RESOLVED":
                    alert["status"] = "VOICE_CALLED"

            tool_send_voice_call(
                f"Unacknowledged patient alert. {gesture}. {message}"
            )

            push_feed({
                "time": datetime.utcnow().strftime("%H:%M:%S"),
                "type": "Twilio Voice",
                "message": (
                    f"No caregiver acknowledgment after {MAX_WAIT_SEC} seconds. "
                    "Voice call dispatched."
                )
            })

            app.logger.info(f"[{alert_id}] Two-minute timeout. Voice call dispatched.")
            return

        reminder_number += 1

        # THIS is the missing action:
        # Put a new command in the queue every 20 seconds.
        push_speaker_command({
            "action": "play_alert",
            "alert_id": alert_id,
            "gesture": gesture,
            "message": message,
            "repeat_number": reminder_number,
            "elapsed_seconds": int(elapsed),
        })

        push_feed({
            "time": datetime.utcnow().strftime("%H:%M:%S"),
            "type": "Bluetooth Speaker",
            "message": (
                f"Local speaker alert #{reminder_number} sent "
                f"for {alert_id}."
            )
        })

        # WhatsApp repeats too.
        whatsapp_message = (
            message
            if reminder_number == 1
            else f"REMINDER #{reminder_number}: {message}"
        )

        tool_send_whatsapp(whatsapp_message)

        push_feed({
            "time": datetime.utcnow().strftime("%H:%M:%S"),
            "type": "Meta WhatsApp",
            "message": (
                "Initial WhatsApp alert sent."
                if reminder_number == 1
                else f"WhatsApp reminder #{reminder_number} sent."
            )
        })

        app.logger.info(
            f"[{alert_id}] Reminder #{reminder_number}: "
            "speaker queued and WhatsApp sent."
        )

        # Sleep in short blocks instead of one 20-second sleep.
        # This makes acknowledgement cancel the loop quickly.
        wait_until = min(
            start + (reminder_number * REPEAT_EVERY_SEC),
            start + MAX_WAIT_SEC
        )

        while time.time() < wait_until:
            time.sleep(1)

            with alerts_lock:
                alert = alerts.get(alert_id)
                if not alert or alert["status"] == "RESOLVED":
                    push_feed({
                        "time": datetime.utcnow().strftime("%H:%M:%S"),
                        "type": "Dashboard",
                        "message": (
                            f"Caregiver acknowledged {alert_id}. "
                            "Speaker loop stopped."
                        )
                    })
                    app.logger.info(f"[{alert_id}] Resolved during wait.")
                    return

@app.route("/", methods=["GET", "POST"])
def patient_view():
    return render_template("patient.html")


@app.route("/dashboard", methods=["GET", "POST"])
def dashboard_view():
    return render_template("dashboard.html")


@app.route("/uploads/<path:filename>", methods=["GET", "POST"])
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


@app.route("/api/v1/process-frame", methods=["POST"])
def process_frame():
    data = request.get_json(force=True, silent=True) or {}

    snapshot_b64 = data.get("snapshot")
    gesture_input = str(data.get("gesture", "NONE")).upper().strip()

    valid_gestures = {"ONE", "TWO", "THREE", "FIST", "NONE"}
    if gesture_input not in valid_gestures:
        gesture_input = "NONE"

    if not snapshot_b64:
        return jsonify({"error": "snapshot missing"}), 400

    alert_id = f"ALT-{int(time.time() * 1000)}"
    fname = f"{alert_id}.jpg"
    fpath = UPLOAD_DIR / fname

    try:
        raw_b64 = (
            snapshot_b64.split(",", 1)[1]
            if "," in snapshot_b64
            else snapshot_b64
        )

        image_bytes = base64.b64decode(raw_b64)
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        image.save(fpath, "JPEG", quality=80)

    except Exception as e:
        app.logger.error(f"[{alert_id}] Image save failed: {e}")
        return jsonify({"error": f"Image decode failed: {e}"}), 400

    prompt = f"""
You are Guardian Angel, a safety-monitoring care agent.

You receive:
1. A camera frame from a vulnerable, non-verbal patient's room.
2. A locally detected gesture value: "{gesture_input}".

Your role is NOT to diagnose disease or claim medical certainty.
Your role is to describe visible signs, identify possible safety incidents,
and choose a safe escalation plan for a human caregiver.

PATIENT COMMUNICATION RULE — HIGHEST PRIORITY:
The patient communicates using fixed hand signals.

- ONE means: patient needs water.
- TWO means: patient needs food.
- THREE means: patient needs the toilet.
- FIST means: emergency — call for help immediately.
- NONE means: no hand signal was detected locally.

If gesture_input is ONE, TWO, THREE, or FIST, treat it as a valid patient
request unless the image is clearly corrupted, covered, or contains no person.
A calm-looking scene must never override a valid hand signal.

VISUAL SAFETY RULE:
Inspect the image independently for:
- A person on the floor, collapsed, or fallen.
- A person slumped, motionless, or possibly unresponsive.
- Obvious severe distress, pain, confusion, abnormal posture, or unsafe sitting.
- A routine activity such as sitting, using a laptop, eating, drinking,
  or holding medication.
- No patient visible, a covered camera, an unusable image, or an unclear scene.

EMERGENCY POLICY:
Set emergency_level to CRITICAL if:
- gesture_input is FIST, OR
- a person appears to have fallen or is on the floor, OR
- a person appears unconscious, unresponsive, severely distressed,
  or in immediate danger.

For CRITICAL:
- whatsapp_alert.execute must be true.
- voice_call.execute must be true.
- comfort_music.execute must be true.
- needs_human_check must be true.

Set emergency_level to URGENT if:
- the patient appears unwell, unusually slumped, distressed, or abnormal,
  but there is no clear fall or immediate danger.

For URGENT:
- whatsapp_alert.execute must be true.
- voice_call.execute must be false unless immediate danger is clearly visible.
- comfort_music.execute must be false.
- needs_human_check must be true.

Set emergency_level to NONE if:
- the patient appears safe, with no valid gesture and no visible concern.

GESTURE POLICY:
- ONE: WhatsApp true; voice false; music false; spoken_code CONFIRM_WATER.
- TWO: WhatsApp true; voice false; music false; spoken_code CONFIRM_FOOD.
- THREE: WhatsApp true; voice false; music false; spoken_code CONFIRM_TOILET.
- FIST: WhatsApp true; voice true; music true; spoken_code EMERGENCY_DISPATCHED.
- A visible critical emergency overrides NONE.

DETAIL RULES:
- assessment: maximum 15 words.
- details: maximum 35 words.
- observations: visible facts only; do not invent events.
- Do not diagnose stroke, heart attack, seizure, dehydration, injury,
  overdose, or any disease.
- Use cautious wording: "possible fall", "appears motionless",
  "possible distress", "possible unresponsiveness", or
  "needs an immediate human check".
- If uncertain between safe and potentially dangerous, choose URGENT.

Return ONLY valid JSON. Do not include Markdown, code fences, or commentary.

Use exactly this structure:
{{
  "assessment": "maximum 15 words",
  "emergency_level": "NONE",
  "incident_type": "SAFE",
  "details": "maximum 35 words",
  "observations": [],
  "confidence": 0.0,
  "needs_human_check": false,
  "tools_to_execute": {{
    "whatsapp_alert": {{
      "execute": false,
      "message": ""
    }},
    "voice_call": {{
      "execute": false,
      "reason": ""
    }},
    "comfort_music": {{
      "execute": false
    }}
  }},
  "spoken_code": "NONE"
}}

Allowed emergency_level values:
NONE, URGENT, CRITICAL

Allowed incident_type values:
SAFE, WATER_REQUEST, FOOD_REQUEST, TOILET_REQUEST, FALL,
POSSIBLE_UNRESPONSIVE, VISIBLE_DISTRESS, ABNORMAL_POSTURE,
MEDICATION_ACTIVITY, MEAL_ACTIVITY, ROUTINE_ACTIVITY,
NO_PATIENT_VISIBLE, SENSOR_UNCLEAR
"""

    gesture_defaults = {
        "ONE": {
            "assessment": "Patient requested water.",
            "emergency_level": "NONE",
            "incident_type": "WATER_REQUEST",
            "details": "One-finger patient request detected.",
            "spoken_code": "CONFIRM_WATER",
            "whatsapp": True,
            "voice": False,
            "music": False,
            "message": "Patient requested water.",
            "reason": ""
        },
        "TWO": {
            "assessment": "Patient requested food.",
            "emergency_level": "NONE",
            "incident_type": "FOOD_REQUEST",
            "details": "Two-finger patient request detected.",
            "spoken_code": "CONFIRM_FOOD",
            "whatsapp": True,
            "voice": False,
            "music": False,
            "message": "Patient requested food.",
            "reason": ""
        },
        "THREE": {
            "assessment": "Patient requested the toilet.",
            "emergency_level": "NONE",
            "incident_type": "TOILET_REQUEST",
            "details": "Three-finger patient request detected.",
            "spoken_code": "CONFIRM_TOILET",
            "whatsapp": True,
            "voice": False,
            "music": False,
            "message": "Patient needs the toilet.",
            "reason": ""
        },
        "FIST": {
            "assessment": "Emergency gesture received.",
            "emergency_level": "CRITICAL",
            "incident_type": "VISIBLE_DISTRESS",
            "details": "Emergency fist gesture detected. Immediate human check required.",
            "spoken_code": "EMERGENCY_DISPATCHED",
            "whatsapp": True,
            "voice": True,
            "music": True,
            "message": "CRITICAL: Patient sent an emergency fist gesture. Please check immediately.",
            "reason": "Emergency fist gesture detected."
        },
        "NONE": {
            "assessment": "No gesture detected.",
            "emergency_level": "NONE",
            "incident_type": "SAFE",
            "details": "Fallback used because no AI assessment was available.",
            "spoken_code": "NONE",
            "whatsapp": False,
            "voice": False,
            "music": False,
            "message": "",
            "reason": ""
        }
    }

    default = gesture_defaults[gesture_input]

    plan = {
        "assessment": default["assessment"],
        "emergency_level": default["emergency_level"],
        "incident_type": default["incident_type"],
        "details": default["details"],
        "observations": [],
        "confidence": 1.0 if gesture_input != "NONE" else 0.0,
        "needs_human_check": gesture_input == "FIST",
        "tools_to_execute": {
            "whatsapp_alert": {
                "execute": default["whatsapp"],
                "message": default["message"]
            },
            "voice_call": {
                "execute": default["voice"],
                "reason": default["reason"]
            },
            "comfort_music": {
                "execute": default["music"]
            }
        },
        "spoken_code": default["spoken_code"]
    }

    if ai_client:
        try:
            t0 = time.time()
            app.logger.info(f"[{alert_id}] Gemini call started...")

            response = ai_client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[
                    types.Part.from_bytes(
                        data=image_bytes,
                        mime_type="image/jpeg"
                    ),
                    prompt
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                    max_output_tokens=1000
                )
            )

            elapsed = time.time() - t0
            raw_text = (response.text or "").strip()

            app.logger.info(
                f"[{alert_id}] Gemini in {elapsed:.1f}s | "
                f"raw='{raw_text[:500]}'"
            )

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
                        parsed = None

            if isinstance(parsed, dict):
                plan = parsed
                app.logger.info(f"[{alert_id}] Agent Plan: {plan}")
            else:
                app.logger.error(
                    f"[{alert_id}] Gemini returned invalid JSON. "
                    "Using local fallback plan."
                )

        except Exception as e:
            app.logger.exception(f"[{alert_id}] Gemini error: {e}")

    tools = plan.get("tools_to_execute") or {}
    whatsapp_tool = tools.get("whatsapp_alert") or {}
    voice_tool = tools.get("voice_call") or {}
    music_tool = tools.get("comfort_music") or {}

    whatsapp_execute = bool(whatsapp_tool.get("execute", False))
    voice_execute = bool(voice_tool.get("execute", False))
    music_execute = bool(music_tool.get("execute", False))

    emergency_level = str(
        plan.get("emergency_level", "NONE")
    ).upper().strip()

    if emergency_level not in {"NONE", "URGENT", "CRITICAL"}:
        emergency_level = "NONE"

    # A valid fist gesture must remain an emergency even if Gemini responds badly.
    if gesture_input == "FIST":
        emergency_level = "CRITICAL"
        whatsapp_execute = True
        voice_execute = True
        music_execute = True

        if not whatsapp_tool.get("message"):
            whatsapp_tool["message"] = (
                "CRITICAL: Patient sent an emergency fist gesture. "
                "Please check immediately."
            )

        if not voice_tool.get("reason"):
            voice_tool["reason"] = "Emergency fist gesture detected."

    # A CRITICAL plan must have both escalation channels.
    if emergency_level == "CRITICAL":
        whatsapp_execute = True
        voice_execute = True
        music_execute = True

    # An urgent event must at least contact the caregiver.
    if emergency_level == "URGENT":
        whatsapp_execute = True

    # Put enforced values back into the plan.
    tools["whatsapp_alert"] = {
        "execute": whatsapp_execute,
        "message": whatsapp_tool.get(
            "message",
            "Patient needs attention."
        )
    }

    tools["voice_call"] = {
        "execute": voice_execute,
        "reason": voice_tool.get(
            "reason",
            "Potential patient safety incident detected."
        )
    }

    tools["comfort_music"] = {
        "execute": music_execute
    }

    plan["tools_to_execute"] = tools
    plan["emergency_level"] = emergency_level

    needs_alert = whatsapp_execute or voice_execute
    status = "PENDING" if needs_alert else "RESOLVED"

    alert_record = {
        "alert_id": alert_id,
        "created_at": datetime.utcnow().isoformat(),
        "gesture": gesture_input,
        "status": status,
        "assessment": str(plan.get("assessment", "")),
        "emergency_level": emergency_level,
        "incident_type": str(plan.get("incident_type", "UNKNOWN")),
        "details": str(plan.get("details", "")),
        "observations": plan.get("observations", []),
        "confidence": plan.get("confidence", 0),
        "needs_human_check": bool(
            plan.get("needs_human_check", False)
        ),
        "image": f"/uploads/{fname}"
    }

    with alerts_lock:
        alerts[alert_id] = alert_record

    if needs_alert:
        push_feed({
            "time": datetime.utcnow().strftime("%H:%M:%S"),
            "type": "Dashboard",
            "message": (
                f"{emergency_level}: {alert_record['assessment']} "
                "Awaiting caregiver acknowledgement."
            )
        })

        threading.Thread(
            target=escalation_ladder,
            args=(
                alert_id,
                tools["whatsapp_alert"]["message"],
                gesture_input
            ),
            daemon=True
        ).start()

    music_url = None

    if music_execute:
        music_url = tool_get_jamendo_music()

        push_feed({
            "time": datetime.utcnow().strftime("%H:%M:%S"),
            "type": "Jamendo API",
            "message": "Comfort music prepared for patient device."
        })

    if gesture_input != "NONE" or needs_alert:
        make_result = tool_send_to_webhook(
            alert_id=alert_id,
            gesture=gesture_input,
            assessment=alert_record["assessment"],
            raw_b64=raw_b64,
            plan=plan
        )
    
        push_feed({
            "time": datetime.utcnow().strftime("%H:%M:%S"),
            "type": "Make.com",
            "message": (
                "Webhook accepted and incident backed up."
                if make_result["success"]
                else (
                    "Webhook failed: "
                    f"{make_result.get('status_code', 'network error')} — "
                    f"{make_result.get('error', 'unknown error')[:120]}"
                )
            )
        })

    if status == "RESOLVED":
        push_feed({
            "time": datetime.utcnow().strftime("%H:%M:%S"),
            "type": "WATCHDOG",
            "message": (
                f"Routine check complete: "
                f"{alert_record['assessment']}"
            )
        })

    return jsonify({
        "alert_id": alert_id,
        "assessment": alert_record["assessment"],
        "emergency_level": alert_record["emergency_level"],
        "incident_type": alert_record["incident_type"],
        "details": alert_record["details"],
        "observations": alert_record["observations"],
        "confidence": alert_record["confidence"],
        "needs_human_check": alert_record["needs_human_check"],
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

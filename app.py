"""
Guardian Angel — Autonomous Elderly Care Agent
================================================
Multi-App AI Agent Hackathon submission.

External apps integrated:
  1. Google Gemini (multimodal vision reasoning)
  2. Meta WhatsApp Cloud API (escalation messaging)
  3. Twilio Voice (emergency phone call)
  4. Jamendo Music API (therapeutic audio)
  5. Make.com (webhook → Google Drive snapshot archive)
"""

import os
import io
import time
import base64
import json
import random
import threading
import requests
import logging
from datetime import datetime
from pathlib import Path
from collections import deque

from flask import Flask, request, jsonify, render_template, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix
from dotenv import load_dotenv
from PIL import Image
from google import genai
from google.genai import types
from twilio.rest import Client as TwilioClient

# ═══════════════════════════════════════════════════════════════════
# BOOTSTRAP
# ═══════════════════════════════════════════════════════════════════
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "guardian-angel-v1")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

gunicorn_logger = logging.getLogger("gunicorn.error")
if gunicorn_logger.handlers:
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
else:
    app.logger.setLevel(logging.INFO)

UPLOAD_DIR = Path("static/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ═══════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════
GEMINI_API_KEY   = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL     = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
TWILIO_SID       = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN     = os.getenv("TWILIO_AUTH_TOKEN")
ESCALATION_DELAY = int(os.getenv("ESCALATION_DELAY", "30"))

ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
twilio_client = TwilioClient(TWILIO_SID, TWILIO_TOKEN) if (TWILIO_SID and TWILIO_TOKEN) else None

# ═══════════════════════════════════════════════════════════════════
# SHARED STATE
# ═══════════════════════════════════════════════════════════════════
alerts: dict = {}
alerts_lock = threading.Lock()

events_feed: deque = deque(maxlen=200)
feed_lock = threading.Lock()

stats = {
    "total_frames": 0, "total_alerts": 0, "whatsapp_sent": 0,
    "voice_calls": 0, "music_plays": 0, "webhooks_sent": 0,
    "acknowledged": 0, "escalated": 0,
    "started_at": datetime.utcnow().isoformat(),
}
stats_lock = threading.Lock()


def bump_stat(key: str, by: int = 1):
    with stats_lock:
        stats[key] = stats.get(key, 0) + by


def push_feed(entry: dict):
    entry.setdefault("time", datetime.utcnow().strftime("%H:%M:%S"))
    entry.setdefault("ts", datetime.utcnow().isoformat())
    with feed_lock:
        events_feed.appendleft(entry)


# ═══════════════════════════════════════════════════════════════════
# TOOL 1 — META WHATSAPP
# ═══════════════════════════════════════════════════════════════════
def tool_send_whatsapp(message: str) -> bool:
    token     = os.getenv("META_WHATSAPP_TOKEN")
    phone_id  = os.getenv("META_PHONE_NUMBER_ID")
    to_number = os.getenv("FAMILY_WHATSAPP_TO")

    if not all([token, phone_id, to_number]):
        app.logger.warning(f"[WhatsApp] Missing creds: token={bool(token)} phone_id={bool(phone_id)} to={bool(to_number)}")
        return False

    url = f"https://graph.facebook.com/v19.0/{phone_id}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": f"🚨 Guardian Angel Alert\n{message}\nCheck dashboard immediately."},
    }

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        if r.status_code in (200, 201):
            app.logger.info("[WhatsApp] Sent ✅")
            bump_stat("whatsapp_sent")
            return True
        app.logger.error(f"[WhatsApp] HTTP {r.status_code}: {r.text[:200]}")
        return False
    except Exception as e:
        app.logger.error(f"[WhatsApp] Exception: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════
# TOOL 2 — TWILIO VOICE
# ═══════════════════════════════════════════════════════════════════
def tool_send_voice_call(reason: str) -> bool:
    if not twilio_client:
        app.logger.warning("[Voice] Twilio client not configured.")
        return False
    try:
        twiml = (
            "<Response><Say voice='alice'>"
            f"Emergency Alert from Guardian Angel. {reason}. "
            "Please check the caregiver dashboard immediately."
            "</Say></Response>"
        )
        call = twilio_client.calls.create(
            twiml=twiml,
            to=os.getenv("FAMILY_PHONE_TO"),
            from_=os.getenv("TWILIO_VOICE_FROM"),
        )
        app.logger.info(f"[Voice] Call dispatched. SID={call.sid}")
        bump_stat("voice_calls")
        return True
    except Exception as e:
        app.logger.error(f"[Voice] Exception: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════
# TOOL 3 — JAMENDO MUSIC
# ═══════════════════════════════════════════════════════════════════
def tool_get_jamendo_music() -> str:
    fallback = "https://prod-1.storage.jamendo.com/?trackid=1890757&format=mp31"
    client_id = os.getenv("JAMENDO_CLIENT_ID")
    if not client_id:
        app.logger.warning("[Jamendo] No client ID. Using fallback.")
        return fallback
    try:
        url = (
            f"https://api.jamendo.com/v3.0/tracks/?client_id={client_id}"
            f"&format=json&tags=ambient,relaxing,calm&limit=5&audioformat=mp31"
        )
        r = requests.get(url, timeout=6)
        data = r.json()
        results = data.get("results") or []
        if results:
            track = random.choice(results)
            app.logger.info(f"[Jamendo] Track: {track.get('name')} by {track.get('artist_name')}")
            bump_stat("music_plays")
            return track["audio"]
        return fallback
    except Exception as e:
        app.logger.error(f"[Jamendo] Exception: {e}")
        return fallback


# ═══════════════════════════════════════════════════════════════════
# TOOL 4 — MAKE.COM → GOOGLE DRIVE
# ═══════════════════════════════════════════════════════════════════
def tool_send_to_webhook(alert_id: str, gesture: str, assessment: str, raw_b64: str) -> bool:
    webhook_url = os.getenv("MAKE_WEBHOOK_URL")
    if not webhook_url:
        app.logger.error("[Webhook] MAKE_WEBHOOK_URL not set.")
        return False
    payload = {
        "alert_id": alert_id,
        "timestamp": datetime.utcnow().isoformat(),
        "gesture": gesture,
        "assessment": assessment,
        "filename": f"{alert_id}.jpg",
        "image_base64": raw_b64,
    }
    try:
        app.logger.info(f"[Webhook] POST → {webhook_url[:50]}...")
        r = requests.post(webhook_url, json=payload, timeout=10)
        app.logger.info(f"[Webhook] {r.status_code} {r.text[:120]}")
        if r.status_code in (200, 201, 202, 204):
            bump_stat("webhooks_sent")
            return True
        return False
    except Exception as e:
        app.logger.error(f"[Webhook] Exception: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════
# ESCALATION LADDER
# ═══════════════════════════════════════════════════════════════════
def escalation_timer(alert_id: str, message: str):
    """Cancellable escalation. If caregiver acknowledges before timer
    expires, the WhatsApp message is silently aborted."""
    app.logger.info(f"[{alert_id}] Escalation timer armed ({ESCALATION_DELAY}s)")
    time.sleep(ESCALATION_DELAY)

    with alerts_lock:
        alert = alerts.get(alert_id)
        if not alert:
            return
        if alert["status"] == "RESOLVED":
            app.logger.info(f"[{alert_id}] Acknowledged → WhatsApp aborted ✅")
            push_feed({
                "type": "Dashboard",
                "message": f"Caregiver acknowledged {alert_id}. WhatsApp aborted.",
                "severity": "success",
            })
            return
        app.logger.info(f"[{alert_id}] Not acknowledged → escalating 🚨")
        alert["status"] = "ESCALATED"
        ok = tool_send_whatsapp(message)
        push_feed({
            "type": "Meta WhatsApp",
            "message": f"{alert_id} escalated to WhatsApp." if ok else f"{alert_id} WhatsApp FAILED.",
            "severity": "danger" if ok else "warning",
        })
        if ok:
            bump_stat("escalated")


# ═══════════════════════════════════════════════════════════════════
# GESTURE → PLAN (HARD RULE ENGINE)
# ═══════════════════════════════════════════════════════════════════
def build_plan(gesture: str) -> dict:
    if gesture == "FIST":
        return {"whatsapp": True, "voice": True, "music": True,
                "spoken": "EMERGENCY_DISPATCHED", "severity": "critical",
                "label": "Emergency — Fall / Distress"}
    if gesture == "WATER":
        return {"whatsapp": True, "voice": False, "music": False,
                "spoken": "CONFIRM_WATER", "severity": "info", "label": "Water Requested"}
    if gesture == "FOOD":
        return {"whatsapp": True, "voice": False, "music": False,
                "spoken": "CONFIRM_FOOD", "severity": "info", "label": "Food Requested"}
    if gesture == "TOILET":
        return {"whatsapp": True, "voice": False, "music": False,
                "spoken": "CONFIRM_TOILET", "severity": "info", "label": "Toilet Assistance"}
    return {"whatsapp": False, "voice": False, "music": False,
            "spoken": "NONE", "severity": "safe", "label": "Routine Check"}


# ═══════════════════════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════════════════════
@app.route("/", methods=["GET", "POST"])
def patient_view():
    return render_template("patient.html")


@app.route("/dashboard", methods=["GET", "POST"])
def dashboard_view():
    return render_template("dashboard.html")


@app.route("/uploads/<path:filename>", methods=["GET", "POST"])
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


@app.route("/health", methods=["GET", "POST"])
def health():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()})


@app.route("/api/v1/debug", methods=["GET", "POST"])
def debug_env():
    return jsonify({
        "gemini": bool(GEMINI_API_KEY),
        "gemini_model": GEMINI_MODEL,
        "meta_whatsapp": {
            "token": bool(os.getenv("META_WHATSAPP_TOKEN")),
            "phone_id": bool(os.getenv("META_PHONE_NUMBER_ID")),
            "to": bool(os.getenv("FAMILY_WHATSAPP_TO")),
        },
        "twilio": {
            "sid": bool(TWILIO_SID), "token": bool(TWILIO_TOKEN),
            "from": bool(os.getenv("TWILIO_VOICE_FROM")),
            "to": bool(os.getenv("FAMILY_PHONE_TO")),
        },
        "jamendo": bool(os.getenv("JAMENDO_CLIENT_ID")),
        "make_webhook": bool(os.getenv("MAKE_WEBHOOK_URL")),
        "escalation_delay_seconds": ESCALATION_DELAY,
    })


@app.route("/api/v1/state", methods=["GET", "POST"])
def get_state():
    with alerts_lock:
        all_alerts = list(alerts.values())
    with feed_lock:
        feed = list(events_feed)[:50]
    with stats_lock:
        s = dict(stats)

    active = [a for a in all_alerts if a["status"] != "RESOLVED"]
    recent = sorted(all_alerts, key=lambda x: x["created_at"], reverse=True)[:12]

    return jsonify({
        "active_alerts": active,
        "recent_images": recent,
        "feed": feed,
        "stats": s,
        "server_time": datetime.utcnow().strftime("%H:%M:%S"),
    })


@app.route("/api/v1/acknowledge", methods=["GET", "POST"])
def acknowledge_alert():
    data = request.get_json(force=True, silent=True) or request.args.to_dict() or {}
    alert_id = data.get("alert_id")
    with alerts_lock:
        alert = alerts.get(alert_id)
        if alert:
            alert["status"] = "RESOLVED"
            alert["resolved_at"] = datetime.utcnow().isoformat()
            app.logger.info(f"[{alert_id}] Acknowledged by caregiver")
            bump_stat("acknowledged")
    push_feed({
        "type": "Dashboard",
        "message": f"Caregiver acknowledged {alert_id}.",
        "severity": "success",
    })
    return jsonify({"status": "SUCCESS", "alert_id": alert_id})


@app.route("/api/v1/process-frame", methods=["GET", "POST"])
def process_frame():
    bump_stat("total_frames")
    data = request.get_json(force=True, silent=True) or {}
    snapshot_b64 = data.get("snapshot")
    gesture = (data.get("gesture") or "NONE").upper()

    if not snapshot_b64:
        return jsonify({"error": "snapshot missing"}), 400

    alert_id = f"ALT-{int(time.time() * 1000) % 10_000_000}"
    fname = f"{alert_id}.jpg"
    fpath = UPLOAD_DIR / fname

    # Decode image
    try:
        raw_b64 = snapshot_b64.split(",", 1)[1] if "," in snapshot_b64 else snapshot_b64
        image_bytes = base64.b64decode(raw_b64)
        Image.open(io.BytesIO(image_bytes)).convert("RGB").save(fpath, "JPEG", quality=80)
    except Exception as e:
        app.logger.error(f"[{alert_id}] Image decode failed: {e}")
        return jsonify({"error": f"image decode failed: {e}"}), 400

    app.logger.info(f"[{alert_id}] gesture={gesture}")

    plan = build_plan(gesture)
    assessment = f"Gesture detected: {gesture}"

    # Gemini Vision refinement
    if ai_client:
        prompt = (
            "You are a vision assistant for an elderly-care monitoring system.\n"
            f"The patient triggered gesture: '{gesture}'. This is GROUND TRUTH.\n"
            "Look at the image and describe:\n"
            "1. What the person is doing (posture, activity, environment)\n"
            "2. Whether you see an OBVIOUS additional emergency "
            "(visible fall, person on floor, blood, unconsciousness, fire)\n\n"
            "Return ONLY valid JSON:\n"
            '{"assessment": "<one concise sentence>", "visible_emergency": true|false}'
        )
        try:
            resp = ai_client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                    prompt,
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                ),
            )
            parsed = json.loads(resp.text.strip())
            assessment = parsed.get("assessment", assessment)
            vision_emergency = bool(parsed.get("visible_emergency", False))
            if vision_emergency and not plan["voice"]:
                app.logger.info(f"[{alert_id}] 🔥 Vision emergency — upgrading")
                plan = build_plan("FIST")
                assessment = f"VISION EMERGENCY — {assessment}"
            app.logger.info(f"[{alert_id}] Gemini OK: {assessment[:80]}")
        except Exception as e:
            app.logger.error(f"[{alert_id}] Gemini error: {e}")

    if plan["voice"]:
        status = "ESCALATED"
    elif plan["whatsapp"]:
        status = "PENDING"
    else:
        status = "RESOLVED"

    with alerts_lock:
        alerts[alert_id] = {
            "alert_id": alert_id,
            "created_at": datetime.utcnow().isoformat(),
            "gesture": gesture,
            "label": plan["label"],
            "severity": plan["severity"],
            "status": status,
            "assessment": assessment,
            "image": f"/uploads/{fname}",
        }
    if gesture != "NONE":
        bump_stat("total_alerts")

    # ─── EXECUTE TOOLS ───

    if plan["whatsapp"]:
        push_feed({
            "type": "Dashboard",
            "message": f"{alert_id}: {plan['label']} — pop-up awaiting caregiver...",
            "severity": "warning",
        })
        threading.Thread(
            target=escalation_timer,
            args=(alert_id, f"Patient triggered '{gesture}'. {assessment}"),
            daemon=True,
            name=f"escalate-{alert_id}",
        ).start()

    if plan["voice"]:
        ok = tool_send_voice_call(f"Emergency gesture: {gesture}. {assessment}")
        push_feed({
            "type": "Twilio Voice",
            "message": "Voice call dispatched to family." if ok else "Voice call FAILED.",
            "severity": "danger" if ok else "warning",
        })

    music_url = None
    if plan["music"]:
        music_url = tool_get_jamendo_music()
        if music_url:
            push_feed({
                "type": "Jamendo API",
                "message": "Comfort music deployed to patient.",
                "severity": "info",
            })

    if gesture != "NONE":
        ok = tool_send_to_webhook(alert_id, gesture, assessment, raw_b64)
        push_feed({
            "type": "Make.com",
            "message": "Backed up to Google Drive." if ok else "Webhook FAILED — check MAKE_WEBHOOK_URL.",
            "severity": "success" if ok else "warning",
        })

    if status == "RESOLVED":
        push_feed({"type": "WATCHDOG", "message": f"Routine check safe ({gesture}).", "severity": "safe"})

    return jsonify({
        "alert_id": alert_id,
        "assessment": assessment,
        "spoken_code": plan["spoken"],
        "play_music_url": music_url,
        "severity": plan["severity"],
        "status": status,
    })


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    app.logger.info("=" * 60)
    app.logger.info("🛡  Guardian Angel — Care Agent starting")
    app.logger.info(f"   Gemini model: {GEMINI_MODEL}")
    app.logger.info(f"   Gemini: {'✅' if ai_client else '❌'}")
    app.logger.info(f"   Twilio: {'✅' if twilio_client else '❌'}")
    app.logger.info(f"   Escalation delay: {ESCALATION_DELAY}s")
    app.logger.info("=" * 60)
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False, threaded=True)

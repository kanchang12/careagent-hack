# Care Guardian Edge — README

---

## 1. The Problem

**One in three adults over 65 falls every year.** Half of them can't get up without help. The average time between a fall and someone finding them is **over an hour**. In care homes it's better; at home, alone, it's worse.

Existing solutions fail in three ways:

- **Wearables** (pendants, watches) get taken off, run out of battery, or sit on the bedside table when the fall happens.
- **Camera-only systems** detect falls but don't understand intent — a person kneeling to pick something up triggers the same alarm as a person who just collapsed.
- **Passive monitoring** tells family *after* something happened. It doesn't let the patient *ask* for help without speaking, without a phone, without pressing anything.

Non-verbal patients, stroke survivors, dementia patients in early stages, and post-surgery patients in bed — they can't always speak or reach a device. They can, however, raise a hand.

**Care Guardian Edge** turns a hand gesture into a full emergency response in under five seconds: a spoken confirmation on the patient side, a WhatsApp alert to family, a voice call if unresolved, and a comfort audio loop that only stops when someone physically presses STOP.

---

## 2. The Technology

| Layer | Component | Purpose |
|---|---|---|
| **Edge vision** | MediaPipe Hands (WASM, in-browser) | Detects hand landmarks and classifies 0/1/2/3 fingers. Runs entirely client-side — no video leaves the browser until a gesture is confirmed. |
| **Reasoning** | Google Gemini (vision + JSON mode) | Receives the still frame and the edge-model guess. Verifies the gesture against the image and decides which tools to invoke. |
| **Orchestration** | Flask + Gunicorn on Koyeb | Backend that receives frames, runs the agent loop, stores alerts, and dispatches tools. |
| **Messaging** | Meta WhatsApp Cloud API | Sends the family alert with the assessment text. |
| **Voice** | Twilio Programmable Voice | Places an automated call if the WhatsApp isn't acknowledged in time. |
| **Audio** | Jamendo API | Fetches a calm ambient track to play while the patient waits. |
| **Archive** | Make.com → Google Drive | Backs up the snapshot and metadata for the audit trail. |
| **Speaker** | Web Speech API (browser TTS) | Voice confirmation and the repeating emergency prompt on the patient's own device. |

**Why hybrid edge + cloud?** Running MediaPipe in the browser means video only leaves the device when a gesture is confirmed. Gemini then does what the edge can't: verify that the hand in the image actually matches the gesture, and decide urgency. This cuts false positives from "hand near face while eating" down to near zero.

---

## 3. The Process

### Gesture vocabulary (patient side)

| Gesture | Meaning | Response |
|---|---|---|
| 1 finger | Water | WhatsApp to family |
| 2 fingers | Food | WhatsApp to family |
| 3 fingers | Toilet | WhatsApp to family |
| Closed fist | **Emergency** | Immediate call + WhatsApp + speaker loop |
| No gesture (every 8 s) | Watchdog | Snapshot sent for safety check |

### Full flow

1. **Capture.** Browser grabs a 480×360 JPEG every 8 s or on gesture.
2. **Edge detection.** MediaPipe classifies the hand into FIST / 1 / 2 / 3 / NONE.
3. **Send.** Frame + gesture label go to `/api/v1/process-frame`.
4. **Gate check.** If a 5-minute processing lock is active, the frame is dropped and the loop is paused. No further processing.
5. **Reasoning.** Gemini looks at the image, compares it with the edge label, and returns a JSON plan: which tools to run, what to say, and whether to escalate.
6. **Dispatch.** Depending on the plan:
   - **Emergency** → Twilio call and WhatsApp fire immediately.
   - **Non-emergency** → WhatsApp fires immediately; Twilio call fires at 5 minutes if unresolved.
   - **No gesture, safe** → nothing.
7. **Speaker loop.** If any alert is raised, the patient device repeats a spoken emergency prompt **every 20 seconds** until the patient presses **STOP EMERGENCY SIGNAL**.
8. **Lock.** Processing is frozen for 5 minutes. New frames are ignored. This prevents alert storms from a single incident.
9. **Acknowledge or escalate.** Family can ACK on the dashboard to stop the speaker loop early. Otherwise the escalation timer fires the call.

### Escalation rules

| Alert type | Immediate | 5 min later (if unresolved) |
|---|---|---|
| Emergency (fist) | Call + WhatsApp + Speaker | — (already called) |
| Non-emergency (1/2/3) | WhatsApp + Speaker | Twilio call |
| Watchdog (no gesture) | Nothing | Nothing |

---

## 4. YouTube Link

*(Insert demo video URL here before submission)*

`https://youtu.be/________________`

---

## 5. Market

### Total addressable population

| Region | Population 65+ | Notes |
|---|---|---|
| United Kingdom | ~12.5 M | NHS-supported home-care programmes |
| United States | ~57 M | Largest single private-pay market |
| India | ~150 M | Growing middle-class elder-care spend |
| China | ~430 M | Government-backed smart-elderly programmes |
| **Total** | **~650 M** | |

Even at **0.1% penetration** that's **650,000 households** — a viable business at £5–15/month per household.

### Target segments (priority order)

1. **Adult children of elderly parents living alone** — the buyer.
2. **Home-care agencies** — need remote monitoring for their carers.
3. **Assisted-living facilities** — want non-wearable fall detection.
4. **Post-surgical recovery at home** — 4–8 week need, high willingness to pay.

### Why now

- Camera hardware is now cheap (a £20 USB camera is enough).
- Edge ML in the browser is free and private.
- LLM vision is dropping in cost every quarter.
- WhatsApp is the default family-communication channel in all four target markets.

---

## 6. Trade-offs

1. **Camera.** This demo uses a laptop webcam / spare phone camera to keep the bill of materials low. In production, a fixed CCTV-style camera with IR night vision, wide-angle lens, and PoE is the correct hardware — roughly £40–80 per unit at scale.

2. **LLM cost.** Every frame hits Gemini. At 8-second intervals that's ~450 calls per hour per household. For production:
   - Cache identical frames (skip call if the image hash is unchanged).
   - Use a smaller model (Gemini Flash-Lite) for routine checks.
   - Only call the full model when the edge gesture is non-NONE.
   - Expected run-rate: **~£2–4/month/household** at current pricing, dropping.

3. **False positives.** The edge model can misclassify hands near the face as fists. Gemini's vision check catches most of these — but not all. In production we'd add:
   - A 30-second confirmation window where the patient can cancel via a second gesture.
   - A second camera angle for corroboration.
   - Fine-tuning on elderly-patient-specific hand data.

4. **Privacy.** Video stays on the device until a gesture is confirmed. Only the confirmed snapshot is uploaded. Frames are never stored longer than needed. In production we'd add on-device face blurring for the background and a 30-day auto-delete policy.

5. **Connectivity.** The system needs internet. Offline fallback (local siren, SMS via GSM module) is planned but not in the current build.

6. **Language and accent.** Web Speech TTS is English-only in this demo. Production requires localised TTS for Hindi, Mandarin, and regional UK accents.

7. **Make.com.** The Make.com → Google Drive archive was intended as an audit trail. **It is not working reliably** and we're deprecating it. The next build uploads directly to Google Drive via a service account, or stores locally with a nightly `rclone` sync. Nothing else in the system depends on Make.com.

---

## 7. Match with the Requirements

### Agents in the system

There are **two agents**, not one.

**Agent 1 — Edge Gesture Agent (in the browser)**
- Runs MediaPipe Hands on every video frame.
- Classifies hand shape into FIST / 1 / 2 / 3 / NONE.
- Requires 5 consecutive frames of the same gesture before firing (prevents flicker).
- Does not make decisions. Only reports what it sees.

**Agent 2 — Care Reasoning Agent (in the cloud)**
- Receives the frame plus the edge agent's label.
- Runs Gemini vision to verify the label against the actual image.
- Decides which external tools to invoke, in what order, and with what urgency.
- Writes the human-readable assessment.
- Triggers the escalation timer, the speaker loop, and the processing lock.

### What each agent does (mapped to requirements)

| Requirement | Where it's handled |
|---|---|
| Non-verbal patient can request help | Edge Agent 1 + Care Agent 2 |
| Distinguish emergency from routine requests | Care Agent 2 (vision verification) |
| Immediate emergency response | Twilio + WhatsApp fired inline in `process_frame` |
| Non-emergency escalation if ignored | `escalation_timer` at 5-minute mark |
| Alert storm prevention | Global 5-minute processing lock |
| Family visibility | Dashboard at `/dashboard` with live feed and images |
| Audit trail | Make.com → Google Drive *(currently non-functional — see trade-offs)* |
| Patient reassurance | Web Speech TTS + repeating speaker loop + Jamendo comfort audio |
| Patient-side stop | **STOP EMERGENCY SIGNAL** button on the patient page |

### On Make.com

**Make.com is not working and should not be presented as a working feature.** In the current build it does nothing useful — the webhook fires, but the downstream Google Drive automation is unreliable. The next version replaces it with a direct Google Drive service-account upload or a local `rclone` sync. Mention this explicitly in any demo or submission so the audience doesn't think it's part of the functioning stack.

---

## 8. Next Stages

### Technical roadmap

| Stage | Feature | Effort |
|---|---|---|
| **v1.1** | Replace Make.com with direct Google Drive upload | 1 day |
| **v1.2** | Add cancel-gesture window before triggering emergency | 2 days |
| **v1.3** | Migrate from MediaPipe to a fine-tuned on-device model for elderly hands | 2 weeks |
| **v1.4** | Offline fallback (local siren + GSM SMS module) | 1 week |
| **v1.5** | Multi-language TTS (Hindi, Mandarin, regional UK) | 3 days |
| **v2.0** | Dedicated hardware unit (see below) | 6 weeks |

### Hardware for production

Current demo uses a laptop/spare phone camera. Production unit:

| Component | Spec | Cost (at 1k units) |
|---|---|---|
| Camera module | 1080p, wide-angle, IR night vision | £18 |
| Compute | Raspberry Pi Zero 2 W or similar | £15 |
| Microphone | MEMS array for far-field pickup | £3 |
| Speaker | 3 W, Bluetooth + wired | £4 |
| Enclosure | Wall-mount, tamper-resistant | £6 |
| Power + PoE | 802.3af | £5 |
| **Total BOM** | | **~£51** |
| Assembly + test | | £10 |
| **Unit cost** | | **~£61** |

Retail at **£149 one-off + £9/month** subscription for the cloud reasoning and alerts. Payback in 7 months per unit.

### Business model

**B2C — direct to families**
- £149 hardware + £9/month subscription.
- Target: adult children buying for parents. High emotional urgency, low price sensitivity.

**B2B — care agencies**
- £5/room/month, hardware leased.
- Agencies already pay £18–25/hour for carers; remote monitoring reduces night-shift visits.

**B2G — local councils / NHS**
- Funded via adult social care budgets.
- Fall-related hospital admissions cost the NHS ~£2B/year. Prevention is a hard ROI argument.

### Investment ask

| Use of funds | Amount |
|---|---|
| Hardware prototyping (100 units) | £8,000 |
| Cloud infrastructure (12 months) | £6,000 |
| Clinical validation study (50 households, 6 months) | £25,000 |
| Regulatory (CE marking, UKCA, data protection impact assessment) | £12,000 |
| **Total seed** | **£51,000** |

### White paper topics

1. *Non-wearable fall detection using browser-side hand landmark models* — the technical case for edge-first vision in elder care.
2. *LLM-assisted intent verification for reducing false-positive emergency alerts* — why a vision second opinion beats pure pose classification.
3. *Privacy-preserving continuous monitoring for ageing-in-place* — how to build a system families trust.
4. *Cost economics of AI-assisted home care in the UK, US, India, and China* — a four-market comparison for investors and policymakers.

---

## 9. Repo Layout

```
careagent-hack/
├── app.py                  # Flask + Gunicorn backend
├── templates/
│   ├── patient.html        # Patient terminal (camera, MediaPipe, STOP button)
│   └── dashboard.html      # Caregiver dashboard
├── static/uploads/         # Snapshot storage
├── Dockerfile              # Koyeb / container deployment
├── requirements.txt        # Python dependencies
└── .env                    # API keys (not committed)
```

### Environment variables

```
GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.0-flash
META_WHATSAPP_TOKEN=
META_PHONE_NUMBER_ID=
FAMILY_WHATSAPP_TO=
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_VOICE_FROM=
FAMILY_PHONE_TO=
JAMENDO_CLIENT_ID=
MAKE_WEBHOOK_URL=          # deprecated — see trade-offs
FLASK_SECRET=
PORT=5000
```

### Running locally

```bash
pip install -r requirements.txt
python app.py
# Open http://localhost:5000        — patient terminal
# Open http://localhost:5000/dashboard — caregiver dashboard
```

---

*Care Guardian Edge — because the difference between a fall and a fatality is how fast someone knows.*

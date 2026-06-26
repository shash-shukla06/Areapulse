# AreaPulse · AR Scanner

Camera-based AR civic issue scanner for AreaPulse. Point your phone at a pothole, broken streetlight, garbage pile, or any civic issue — AI detects it, classifies it, draws an AR bounding box, and one-tap submits to the main AreaPulse Firestore database.


---

## What it does

1. **Live camera feed** — opens phone rear camera in full screen
2. **AI scan on tap** — captures frame, sends to Groq Llama-4-Scout Vision
3. **AR overlay** — draws bounding box + label on detected issue
4. **Auto-classify** — pothole, garbage, water, streetlight, sewage, electricity, traffic, tree, noise
5. **GPS auto-capture** — reverse-geocodes to Delhi area via OSM Nominatim
6. **One-tap submit** — writes issue to shared Firestore with main app
7. **My Reports** — view all reports filed under your username
8. **Hindi support** — every detected issue has Hindi title + description

---

## Structure

```
areapulse-camera/
├── app.py                  ← Flask backend (5 endpoints)
├── requirements.txt
├── Procfile                ← Render/Heroku deploy config
├── .env.example
├── .gitignore
├── README.md
├── templates/
│   └── index.html          ← Single-page UI with 4 screens
└── static/
    ├── app.js              ← Camera, AR, AI, submission logic
    └── style.css           ← All styling
```

---

## API endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| GET  | `/`              | Render the AR scanner page |
| POST | `/api/analyze`   | Send base64 frame → Groq Vision → JSON detection |
| POST | `/api/submit`    | Save detection as issue in Firestore |
| GET  | `/api/nearby`    | Nearby issues + NGOs within 5km / 10km |
| GET  | `/api/geocode`   | Reverse-geocode lat/lng → Delhi area name |
| GET  | `/api/health`    | Health check + integration status |

---

## Run locally

```bash
git clone <this-repo>
cd areapulse-camera
pip install -r requirements.txt
cp .env.example .env
# Edit .env — add your GROQ_API_KEY and FIREBASE_KEY_JSON
python app.py
```

Opens at `http://localhost:5001`.

**Important:** Browsers only allow camera access over HTTPS or `localhost`. For phone testing, use a tunnel:

```bash
# Install ngrok then:
ngrok http 5001
# Open the HTTPS URL on your phone
```

---

## Deploy to Render

1. Push this folder to a GitHub repo
2. Render Dashboard → New Web Service → Connect repo
3. Settings:
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120`
   - **Environment:** Python 3
4. Add environment variables:
   - `GROQ_API_KEY` — your Groq key
   - `FIREBASE_KEY_JSON` — single-line Firebase service account JSON
   - `AREAPULSE_URL` — your main AreaPulse Render URL
5. Deploy. Live in ~90 seconds.

---

## Firebase setup

The AR scanner writes to the **same Firestore database** as the main AreaPulse site so reports appear in users' "My Issues" feed.

1. Firebase Console → your project → ⚙ Settings → Service Accounts → Generate new private key
2. Download the JSON file
3. Flatten to one line:
   ```bash
   python -c "import json; print(json.dumps(json.load(open('firebase-key.json'))))"
   ```
4. Paste the output as `FIREBASE_KEY_JSON` in your `.env` or Render env vars

---

## How the AI detection works

The frontend captures the current video frame to a canvas, encodes it as base64 JPEG, and POSTs to `/api/analyze`. The backend forwards it to Groq's `meta-llama/llama-4-scout-17b-16e-instruct` model with a vision prompt that asks for:

- Issue type (10 categories)
- Severity (low/medium/high)
- Confidence (70–98)
- Bilingual title + description (English + Hindi)
- AR label (max 3 words)
- Recommended Delhi authority (MCD/DJB/BSES/PWD/etc.)
- Estimated repair time
- Bounding box (normalized 0–100 coordinates)

Returns up to 4 distinct issues per frame. `primary_index` indicates which to highlight.

---

## Browser support

| Feature | Required | Note |
|---|---|---|
| `getUserMedia` (camera) | ✅ Required | All modern mobile + desktop browsers |
| Geolocation API | Recommended | Falls back to Delhi center if denied |
| HTTPS or localhost | ✅ Required | Camera blocked on plain HTTP |
| Canvas + base64 encoding | ✅ Required | All modern browsers |

Best on: **iOS Safari 14+, Chrome Android 90+, Chrome desktop 90+**

---

## Demo flow (for hackathon judges)

```
1. Open URL on phone → "Sign in" → enter any username
2. Grant camera + location permission
3. Point camera at a real or printed civic issue
4. Tap big yellow SCAN button
5. AI returns detection in ~3 seconds with AR box drawn over the issue
6. Tap "View Report →" to see auto-filled report
7. Tap "Submit" — issue is saved to Firestore
8. Open main AreaPulse site — your report appears on the map
```

---

## Troubleshooting

**"Camera permission denied"** — Browser settings → Site Settings → Camera → Allow for this site. iOS Safari also requires "Always Allow" not just "Once."

**"No issue detected"** — The AI is aggressive but can miss. Try a clearer angle, better lighting, or get closer to the issue. The model errs toward false-positives for civic context.

**"AI returned unparseable response"** — Groq occasionally returns malformed JSON. The retry button in the UI handles this. If persistent, check `/api/health` for Groq connectivity.

**"Reports don't show on main site"** — Confirm both apps share the same `FIREBASE_KEY_JSON` and project ID. Run `/api/health` on both apps to verify.

---

## Built for QuantCraft 2026 by Team Nexons

Companion project to AreaPulse main site. Both apps share the same Firestore database, NGO registry, and authority routing logic.

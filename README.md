<div align="center">

# 🌆 AreaPulse

### AI-Powered Civic Infrastructure Reporting & Resolution Platform

**Citizens · Government · NGOs — One Platform, One Live Map**

[![Live App](https://img.shields.io/badge/Live-areapulse.onrender.com-E55934?style=for-the-badge)](https://areapulse.onrender.com/)
[![AR Scanner](https://img.shields.io/badge/AR_Scanner-Live-15803D?style=for-the-badge)](https://areapulse-cam.onrender.com/)
[![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-3.0-000000?style=for-the-badge&logo=flask)](https://flask.palletsprojects.com/)
[![Groq](https://img.shields.io/badge/AI-Groq_Llama--4-F55036?style=for-the-badge)](https://groq.com/)

**Team Nexons** · Garv Chopra & Shashwat Shukla

[Problem](#-the-problem) • [Solution](#-the-solution) • [Features](#-key-features) • [Setup](#-local-setup) • [Architecture](#-architecture) • [API](#-api-reference)

</div>

---

## 📋 Table of Contents

- [What Is AreaPulse](#-what-is-areapulse)
- [The Problem](#-the-problem)
- [The Solution](#-the-solution)
- [Three Reporting Channels](#-three-reporting-channels)
- [AI Processing Pipeline](#-ai-processing-pipeline)
- [Key Features](#-key-features)
- [Tech Stack](#-tech-stack)
- [Architecture](#-architecture)
- [Local Setup](#-local-setup)
- [Environment Variables](#-environment-variables)
- [Demo Accounts](#-demo-accounts)
- [Project Structure](#-project-structure)
- [API Reference](#-api-reference)
- [Deployment](#-deployment)
- [Recognition](#-recognition)
- [Team](#-team)

---

## 🎯 What Is AreaPulse

AreaPulse is a unified AI-powered platform that connects **citizens**, **government officials**, and **NGOs** on a single live map to report, route, and resolve civic infrastructure issues across India.

The core principle: **the citizen does almost nothing. AI does the rest.**

- 📸 **5-second AR reporting** — point camera, AI auto-classifies, auto-submits
- 💬 **WhatsApp reporting** — send a photo, AI files the complete report
- 🗺️ **One-tap map submission** — tap location, snap, done
- 🤖 **AI pipeline** — classification, severity scoring, spam filtering, duplicate detection, auto-routing
- 🏛️ **Government dashboard** — AI-prioritized queue with live SLA timers and one-click status updates
- 🤝 **NGO dashboard** — real-time civic data showing exactly where resources are needed most

**Result: resolution time drops from 2–3 months to 24–48 hours.**

---

## 🔴 The Problem

India's civic complaint system is broken for **all three stakeholders simultaneously**:

### 👥 For Citizens
Government portals (gov.in) demand **12+ fields** — name, address, ward number, category, sub-category, captcha, OTP, document uploads. **80%+ of citizens abandon mid-form.** Those who finish wait 2–3 months and hear nothing. Rural users with basic phones cannot navigate these portals at all.

### 🏛️ For Government
Municipal offices receive **thousands of unsorted complaints daily** across fragmented channels — gov.in portal, helplines, paper forms, social media, RWA emails. There's **no AI prioritization, no duplicate detection, no SLA tracking, no auto-routing**. Critical issues like sewer overflows sit buried under routine complaints.

### 🤝 For NGOs
Organizations willing to deploy field resources have **zero structured data** showing where intervention is needed most. They work from guesswork while real civic problems cluster invisibly in underserved neighborhoods.

**The stakes:** sewer overflows spread disease, potholes claim lives, broken streetlights enable crime. With Indian cities adding 10M residents per year, the current system is collapsing under volume it was never designed to handle.

---

## ✅ The Solution

AreaPulse rebuilds the entire complaint cycle into a unified workflow:

```
┌──────────────────────────────────────────────────────────────────┐
│  CITIZEN reports in 5 seconds via AR / WhatsApp / Map-tap        │
│                              ↓                                    │
│  AI auto-classifies (10 categories) + severity scores + filters  │
│  spam + detects duplicates within 50m + auto-routes              │
│                              ↓                                    │
│  GOVERNMENT receives AI-prioritized queue with live SLA timers   │
│                              ↓                                    │
│  NGO sees where to deploy resources for maximum impact           │
│                              ↓                                    │
│  Citizen receives WhatsApp updates at every stage                │
└──────────────────────────────────────────────────────────────────┘
```

---

## 📱 Three Reporting Channels

### 1. AR Camera Scanner (PWA)
- User opens camera, points at pothole/garbage/broken streetlight
- **Computer vision (Groq Llama-4-Scout)** analyzes the live image
- Classifies issue type, scores severity, captures GPS automatically
- Submits in **under 5 seconds with one tap** — no form, no typing
- Deployed separately at [areapulse-cam.onrender.com](https://areapulse-cam.onrender.com/)

### 2. WhatsApp Bot (Twilio)
- Citizen sends a photo to AreaPulse's WhatsApp number
- AI reads the image, classifies, extracts GPS from EXIF metadata
- Files complete report automatically
- **No app install, no account, no form** — works on any phone with WhatsApp
- Sandbox: send `join feet-cheese` to `+14155238886`

### 3. Map-Tap Submission (Website)
- Open full-screen Delhi map, tap location of issue
- Snap photo, submit
- AI handles categorization, severity, area detection, and routing in background
- **Under 30 seconds end-to-end**

---

## 🤖 AI Processing Pipeline

Every submitted report passes through a multi-stage Groq Llama-4-Scout pipeline:

| Stage | What It Does |
|---|---|
| **Tag Classification** | Categorizes across 10 infrastructure types: pothole, water, sewage, electricity, streetlight, garbage, traffic, noise, tree, other |
| **Severity Scoring** | Rates as high / medium / low based on description + image analysis |
| **Spam Detection** | Filters fake/abusive/test reports before they reach gov queues |
| **Duplicate Detection** | Within 50m radius + 7-day window — multiple reports of same pothole **strengthen one issue** rather than creating noise |
| **Auto-Routing** | Forwards to correct department (Water Board, MCD, DISCOM, Traffic Police, etc.) |
| **Authority Lookup** | Matches issue tag to government authority with email/phone for direct complaint dispatch |
| **AI Triage** | Government officers can ask AI to summarize their queue and recommend priorities |
| **AI Draft Response** | Generates personalized WhatsApp/SMS replies to citizens with one click |
| **NGO Recommendations** | Suggests where each NGO can have the biggest impact this month |

---

## ⭐ Key Features

### For Citizens
- ✅ 5-second AR camera reporting
- ✅ WhatsApp photo-based reporting (rural-friendly)
- ✅ Anonymous reporting + Google OAuth login
- ✅ Live issue tracking with public map
- ✅ Status notifications via WhatsApp at every stage
- ✅ "My Reports" page with personal issue history + points
- ✅ Upvote to amplify nearby issues
- ✅ Community feed for neighborhood discussions
- ✅ AI-drafted formal complaint letters (with PDF export)
- ✅ Email complaints directly to government authorities

### For Government Officials
- ✅ Department-specific dashboard (Water/Power/Traffic/MCD)
- ✅ AI-prioritized issue queue sorted by SLA urgency
- ✅ Live SLA countdown timers per issue
- ✅ Indian municipal norm-based SLAs (24h sewage, 48h water, 7d potholes)
- ✅ Auto-escalation on SLA breach
- ✅ Crowd escalation at 25+ citizen upvotes
- ✅ One-click status updates: open → acknowledged → in_progress → resolved
- ✅ Full status history with timestamps
- ✅ ✨ **AI Triage** — Groq summarizes queue and recommends priorities
- ✅ ✨ **AI Draft Response** — generates citizen WhatsApp messages
- ✅ CSV export of issue data
- ✅ Search and filter (overdue, soon, in-progress, resolved)

### For NGOs
- ✅ Sage-themed "Where you're needed most" dashboard
- ✅ Live opportunity cards filtered by NGO focus area
- ✅ Stats: unresolved issues, affected citizens, NGOs already active
- ✅ One-click "Start project" commitment workflow
- ✅ Active projects with progress tracking
- ✅ ✨ **AI Recommendations** — Groq suggests highest-impact neighborhoods
- ✅ Public NGO directory listing
- ✅ Impact tracking and testimonials

### Platform-Wide
- ✅ Full-screen interactive Delhi map (Leaflet.js)
- ✅ MapTiler satellite hybrid tiles (Flightradar24-style)
- ✅ Severity-coded heatmap pins
- ✅ 141 seed issues across 36 Delhi neighborhoods (for demo)
- ✅ 16 seed NGOs with focus areas
- ✅ Postgres + Firebase + in-memory triple fallback
- ✅ 5-min read cache (Firebase quota protection)
- ✅ Rate limiting to prevent abuse

---

## 🛠️ Tech Stack

<table>
<tr>
<td><strong>Backend</strong></td>
<td>Python 3.12, Flask 3.0, Gunicorn</td>
</tr>
<tr>
<td><strong>Frontend</strong></td>
<td>Vanilla JavaScript, Leaflet.js, Jinja2 templates</td>
</tr>
<tr>
<td><strong>Database</strong></td>
<td>Neon Postgres (primary), Firebase Firestore (real-time sync), in-memory fallback</td>
</tr>
<tr>
<td><strong>Map</strong></td>
<td>MapTiler Satellite Hybrid + OpenStreetMap labels overlay</td>
</tr>
<tr>
<td><strong>AI Engine</strong></td>
<td>Groq API · Llama-4-Scout (vision + text)</td>
</tr>
<tr>
<td><strong>AR Scanner</strong></td>
<td>Separate PWA deployed on Render with real-time computer vision</td>
</tr>
<tr>
<td><strong>WhatsApp</strong></td>
<td>Twilio WhatsApp Business API</td>
</tr>
<tr>
<td><strong>Email</strong></td>
<td>Resend API (for complaint dispatch to govt authorities)</td>
</tr>
<tr>
<td><strong>Auth</strong></td>
<td>Google OAuth 2.0 + email-password + anonymous</td>
</tr>
<tr>
<td><strong>Geocoding</strong></td>
<td>Nominatim (OpenStreetMap)</td>
</tr>
<tr>
<td><strong>Deployment</strong></td>
<td>Render (continuous deployment from GitHub)</td>
</tr>
<tr>
<td><strong>Monitoring</strong></td>
<td>UptimeRobot (prevents Render free-tier cold starts)</td>
</tr>
</table>

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    CITIZEN REPORTING LAYER                       │
├─────────────────────────────────────────────────────────────────┤
│   AR Scanner PWA          WhatsApp Bot          Map-Tap Web     │
│   (cam.onrender.com)      (Twilio)              (Leaflet)       │
└────────────────┬────────────────┬────────────────┬──────────────┘
                 │                │                │
                 └────────────────┼────────────────┘
                                  ▼
        ┌─────────────────────────────────────────────────┐
        │           FLASK BACKEND (app.py)                 │
        │   /report  /whatsapp  /ai/analyze-image         │
        └─────────────────────────────────────────────────┘
                                  ▼
        ┌─────────────────────────────────────────────────┐
        │      AI PIPELINE (Groq Llama-4-Scout)            │
        │  Classify → Severity → Spam → Duplicate → Route │
        └─────────────────────────────────────────────────┘
                                  ▼
        ┌─────────────────────────────────────────────────┐
        │   DATABASE: Postgres → Firebase → In-Memory     │
        │   (database.py with triple fallback)             │
        └────────────────┬────────────────────────────────┘
                         ▼
        ┌─────────────────────────────────────────────────┐
        │              RESOLUTION LAYER                    │
        ├──────────────────────────────────────────────────┤
        │  Gov Dashboard          NGO Dashboard            │
        │  (/gov)                 (/ngo/dashboard)         │
        │  • AI-prioritized queue • Opportunity cards      │
        │  • SLA countdowns       • Where-needed-most      │
        │  • Status updates       • Commit to projects     │
        │  • AI Triage            • AI Recommendations     │
        └────────────────┬─────────────────────────────────┘
                         ▼
        ┌─────────────────────────────────────────────────┐
        │     WhatsApp Status Notifications (Twilio)       │
        │  Citizens get updates: acknowledged, resolved    │
        └─────────────────────────────────────────────────┘
```

---

## 🚀 Local Setup

### Prerequisites
- Python 3.12+
- pip
- Git
- (Optional) Postgres database (Neon recommended)
- (Optional) Firebase service account JSON

### 1. Clone the repo

```bash
git clone https://github.com/shash-shukla06/Areapulse.git
cd Areapulse
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Create `.env` file

```env
# Required for production AI features (optional for local dev)
GROQ_API_KEY=your_groq_key_here

# Optional — degrades gracefully if missing
MAPTILER_KEY=your_maptiler_key
DATABASE_URL=postgresql://user:pass@host/db
FIREBASE_KEY_JSON={"type":"service_account",...}

# Optional — WhatsApp integration
TWILIO_ACCOUNT_SID=ACxxxxx
TWILIO_AUTH_TOKEN=xxxxx
TWILIO_WHATSAPP_NUMBER=+14155238886
TWILIO_SANDBOX_CODE=feet-cheese

# Optional — Google OAuth
GOOGLE_CLIENT_ID=xxxxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=xxxxx

# Optional — Email dispatch
RESEND_API_KEY=re_xxxxx
DEMO_RECIPIENT_EMAIL=your-email@example.com

# Flask
SECRET_KEY=any-random-string
FLASK_DEBUG=1
```

### 4. Run

```bash
python app.py
```

Open http://localhost:5000 — the app runs with seeded data even without external services configured.

---

## 🔑 Environment Variables

| Variable | Required? | Purpose |
|---|---|---|
| `GROQ_API_KEY` | Yes (for AI) | AI classification, vision, draft response, triage |
| `DATABASE_URL` | No | Neon Postgres connection (falls back to in-memory) |
| `FIREBASE_KEY_JSON` | No | Firebase service account JSON (real-time sync) |
| `MAPTILER_KEY` | No | Satellite map tiles (falls back to OSM) |
| `MAPTILER_STYLE` | No | Map style (`hybrid` default) |
| `TWILIO_ACCOUNT_SID` | No | WhatsApp inbound/outbound |
| `TWILIO_AUTH_TOKEN` | No | Twilio auth |
| `TWILIO_WHATSAPP_NUMBER` | No | Bot's WhatsApp number |
| `TWILIO_SANDBOX_CODE` | No | Sandbox join code (e.g. `feet-cheese`) |
| `GOOGLE_CLIENT_ID` | No | Google OAuth |
| `GOOGLE_CLIENT_SECRET` | No | Google OAuth |
| `GOOGLE_REDIRECT_URI` | No | OAuth callback override |
| `RESEND_API_KEY` | No | Email dispatch to govt authorities |
| `DEMO_RECIPIENT_EMAIL` | No | Demo mode — routes all complaints to this address |
| `SECRET_KEY` | No | Flask session secret |
| `AREAPULSE_URL` | No | Public URL (used in WhatsApp links) |
| `WA_NOTIFY_DRY_RUN` | No | Set to `1` to simulate WhatsApp sends |

---

## 🔐 Demo Accounts

All demo accounts use **PIN: `0000`**

### Government Officers

| Username | Department | Handles |
|---|---|---|
| `gov_rmc` | Ranchi Municipal Corporation | pothole, garbage, sewage, streetlight, tree |
| `gov_water` | Water Board | water |
| `gov_electricity` | JBVNL | electricity |
| `gov_traffic` | Traffic Police | traffic, noise |

### NGO Partners

| Username | Organization | Focus | Operating Areas |
|---|---|---|---|
| `ngo_sanitation` | Delhi Sanitation Trust | sewage, garbage, tree | Lajpat Nagar, Defence Colony, GK, Saket |
| `ngo_water` | Jal Seva Foundation | water | Dwarka, Janakpuri, Rohini, Pitampura |
| `ngo_civic` | Delhi Civic Trust | pothole, streetlight, traffic, other | CP, Karol Bagh, Paharganj, Civil Lines |
| `ngo_power` | Bijli Pratikriya | electricity, streetlight | Shahdara, Laxmi Nagar, Mayur Vihar, Preet Vihar |

### Citizen Login
Any name (min 2 chars), no PIN required.

---

## 📁 Project Structure

```
Areapulse/
├── app.py                     # Main Flask app — all routes
├── database.py                # Postgres/Firebase/memory fallback + seed data
├── ai_engine.py               # Groq integration — vision, classification, drafts
├── classifier.py              # Keyword-based auto-classification fallback
├── email_sender.py            # Resend API integration
├── seed_postgres.py           # One-time DB seeding script
├── requirements.txt           # Python dependencies
├── render.yaml                # Render deployment config
├── .env                       # Local env vars (gitignored)
│
├── templates/
│   ├── index.html             # Public map (citizen home)
│   ├── login.html             # 3-tab login (citizen/gov/NGO)
│   ├── gov.html               # Government dashboard (light Civic Trust)
│   ├── ngo_dashboard.html     # NGO dashboard (sage opportunities)
│   ├── ngos.html              # Public NGO directory
│   ├── issues.html            # All issues page
│   ├── my_issues.html         # User's own reports
│   ├── community.html         # Neighborhood feed
│   ├── stats.html             # Public analytics
│   ├── complaint_print.html   # Print-to-PDF complaint letter
│   └── base.html              # Shared layout
│
└── static/                    # Optional static assets
```

---

## 📡 API Reference

### Public Routes

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/` | Public map (home) |
| `GET` | `/issues-all` | All issues view |
| `GET` | `/my-reports` | User's own issues |
| `GET` | `/community` | Community feed |
| `GET` | `/stats` | Public analytics |
| `GET` | `/ngos` | NGO directory |
| `GET` | `/login` | Login page (3 tabs) |
| `GET` | `/logout` | Sign out |

### Citizen Reporting

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/report` | Submit new issue (form-data with image) |
| `GET` | `/issues` | Fetch all issues (with SLA + auto-escalation) |
| `POST` | `/upvote/<id>` | Upvote issue (crowd-escalates at 25+) |
| `GET` | `/areas` | All Delhi areas with coordinates |

### Government Dashboard

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/gov` | Government dashboard (auth required) |
| `POST` | `/gov/update-status/<id>` | Change issue status (triggers WhatsApp ping) |
| `GET` | `/gov/ai-triage` | AI summary of queue + priorities |
| `POST` | `/gov/ai-draft-response/<id>` | AI-drafted citizen response |
| `GET` | `/gov/all` | Govt authority list (used by NGO page) |

### NGO Dashboard

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/ngo/dashboard` | NGO opportunities dashboard (auth required) |
| `POST` | `/ngo/commit` | Commit to an opportunity (area + tag) |
| `GET` | `/ngo/ai-recommendations` | AI-suggested focus areas |
| `GET` | `/ngo/all` | All NGOs (public directory data) |
| `GET` | `/ngo/nearby` | NGOs near given lat/lng |

### AI Endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/ai/analyze-image` | Vision-based issue detection from photo |
| `POST` | `/ai/ask` | Free-form Q&A about civic data |
| `GET` | `/ai/insights` | Landscape summary + AI commentary |
| `GET` | `/ai/health` | AI/email service availability check |
| `POST` | `/ai/draft-complaint/<id>` | Generate formal complaint letter |
| `POST` | `/ai/draft-dispatch/<id>` | Draft + authority lookup combined |
| `GET` | `/complaint-print/<id>` | Print-optimized complaint letter HTML |

### WhatsApp Bot

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/whatsapp` | Twilio inbound webhook |
| `POST` | `/whatsapp/status` | Delivery status callback |

### Email

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/email/send-complaint/<id>` | Dispatch complaint via Resend |

### Auth

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/login` | Citizen/gov/NGO login (auto-detects by username) |
| `GET` | `/auth/google` | Start Google OAuth flow |
| `GET` | `/auth/google/callback` | OAuth callback |

### Debug

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/health/firestore` | Firestore round-trip test |
| `GET` | `/api/debug/dup-check` | Test duplicate detection without inserting |

---

## 🌐 Deployment

AreaPulse runs on **Render** with continuous deployment from GitHub.

### Render Setup

1. Push to GitHub
2. Create a new Web Service on [Render](https://render.com/)
3. Connect to your GitHub repo
4. Build command: `pip install -r requirements.txt`
5. Start command: `gunicorn app:app --workers 2 --threads 4 --timeout 60`
6. Add all environment variables from the [Env Vars](#-environment-variables) section
7. Deploy

### Preventing Cold Starts (Free Tier)
Use [UptimeRobot](https://uptimerobot.com/) to ping both services every 5 minutes:
- `https://areapulse.onrender.com/ai/health`
- `https://areapulse-cam.onrender.com/`

---

## 📊 Database Schema

### `issues` collection
```
id, description, area, severity, tag, lat, lng,
user_name, landmark, contact, status, upvotes, image,
timestamp, verified, escalated, resolved,
status_history, escalation_reason, escalated_at, resolved_at
```

### SLA Norms (Indian Municipal Standards)

| Category | SLA |
|---|---|
| Sewage | 24 hours |
| Electricity | 24 hours |
| Traffic | 24 hours |
| Noise | 24 hours |
| Water | 48 hours |
| Streetlight | 48 hours |
| Garbage | 72 hours |
| Other | 120 hours (5 days) |
| Pothole | 168 hours (7 days) |
| Tree | 168 hours (7 days) |

---

## 🏆 Recognition

- 🥉 **QuantCraft 2026** — Top 10 Finalists
- 🎯 **Microsoft Build AI** — Selected project
- 📝 **SIH 2025** — Submitted across 5 Problem Statements (AICTE Delhi Pollution, MoJS Rural Water O&M, MoSJE Adarsh Gram, MoJS Rooftop Rainwater, Jharkhand Crowdsourced Civic)

---

## 🌍 Coverage

**36 Delhi Neighborhoods** are pre-seeded with realistic civic issue data:

Connaught Place · Karol Bagh · Rohini · Saket · Lajpat Nagar · Hauz Khas · Dwarka · Janakpuri · Chandni Chowk · Paharganj · Mehrauli · Malviya Nagar · Greater Kailash · Vasant Kunj · Pitampura · Model Town · Civil Lines · Mukherjee Nagar · Rajouri Garden · Punjabi Bagh · Mayur Vihar · Preet Vihar · Shahdara · Laxmi Nagar · Okhla · Kalkaji · Nehru Place · Lodhi Colony · Kashmere Gate · Nizamuddin · Sarojini Nagar · INA · Patel Nagar · RK Puram · Vasant Vihar · Defence Colony

---

## 🧪 Try It Now

**Live App:** [areapulse.onrender.com](https://areapulse.onrender.com/)
**AR Scanner:** [areapulse-cam.onrender.com](https://areapulse-cam.onrender.com/)

**WhatsApp Bot:**
1. Save `+1 415 523 8886` to contacts
2. Send `join feet-cheese` to it on WhatsApp
3. Send a photo of any civic issue (pothole, garbage, broken light)
4. AI does the rest

**Try the Dashboards:**
- Login as `gov_water` / `0000` → Government dashboard
- Login as `ngo_water` / `0000` → NGO dashboard

---

## 👥 Team Nexons

<table>
<tr>
<td align="center">
<strong>Garv Chopra</strong><br/>
<em>Full-stack Engineering & Product</em>
</td>
<td align="center">
<strong>Shashwat Shukla</strong><br/>
<em>AI Integration & Backend Architecture</em>
</td>
</tr>
</table>

---

## 🙏 Acknowledgments

- **Groq** for blazing-fast Llama-4-Scout inference
- **Twilio** for WhatsApp Business API access
- **Render** for free continuous deployment
- **MapTiler** for satellite map tiles
- **Neon** for serverless Postgres
- **OpenStreetMap** community for Nominatim geocoding

---

<div align="center">

### ✨ Make some difference in the society — AreaPulse 🌱

**Built with care for India's civic future**

[Report an Issue](https://areapulse.onrender.com/) · [View Demo](https://areapulse.onrender.com/) · [Star on GitHub](https://github.com/shash-shukla06/Areapulse)

</div>

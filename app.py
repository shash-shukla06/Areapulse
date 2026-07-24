import os, json, time, math
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from groq import Groq
import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)
CORS(app, origins=["https://areapulse-1.onrender.com/", "*"])

groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

db = None
try:
    key_path = "firebase_key.json"
    if os.path.exists(key_path):
        cred = credentials.Certificate(key_path)
    else:
        key_json = os.environ.get("FIREBASE_KEY_JSON") or os.environ.get("FIREBASE_CREDENTIALS_JSON")
        if key_json:
            import tempfile
            tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
            tmp.write(key_json); tmp.close()
            cred = credentials.Certificate(tmp.name)
        else:
            raise FileNotFoundError("No Firebase key found")
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    db = firestore.client()
    print("✅ Firebase connected")
except Exception as e:
    print(f"⚠️  Firebase not configured: {e}")


def _next_int_id(collection_name):
    """Mirror main app's _next_int_id so IDs and counter stay consistent."""
    counter_ref = db.collection('_counters').document(collection_name)
    snap = counter_ref.get()
    if snap.exists:
        n = snap.to_dict().get('n', 0) + 1
    else:
        n = 1
    counter_ref.set({'n': n})
    return n


def haversine(lat1, lng1, lat2, lng2):
    R = 6371
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = math.sin(d_lat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lng/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

DETECT_PROMPT = """You are a civic issue detection AI for AreaPulse — Delhi smart city platform.

CRITICAL INSTRUCTION: A citizen pointed their camera at this scene because they see a civic problem. You MUST identify it. Look very carefully — even subtle issues count: cracked roads, small piles of trash, dim/broken lights, exposed wires, leaking pipes, fallen branches, blocked drains, anything that looks like infrastructure damage or municipal neglect.

ALWAYS return at least 1 issue. Only return an empty array if the image is clearly a face, indoor selfie, or has zero outdoor/street context.

Respond with ONLY valid JSON. No markdown fences. No preamble. No "Here is..." text:
{
  "issues": [
    {
      "issue_type": "pothole|garbage|water|streetlight|sewage|electricity|traffic|tree|noise|other",
      "severity": "low|medium|high",
      "confidence": <integer 70-98>,
      "title": "<clear English title, max 8 words>",
      "title_hi": "<same title translated to Hindi (Devanagari script), max 8 words>",
      "description": "<English 2-3 sentences>",
      "description_hi": "<same description translated to Hindi (Devanagari script), 2-3 sentences>",
      "ar_label": "<max 3 English words>",
      "recommended_authority": "MCD North|MCD South|DJB|PWD|BSES Yamuna|BSES Rajdhani|Delhi Traffic Police|NDMC",
      "estimated_repair_time": "24-48 hours|3-7 days|1-2 weeks|2-4 weeks",
      "hazard_level": "low|medium|high",
      "area_estimate": "<Delhi locality>",
      "x_hint": <0-100>,
      "y_hint": <0-100>,
      "bbox": [<x1>, <y1>, <x2>, <y2>]
    }
  ],
  "primary_index": 0
}

bbox is a bounding box around the issue in normalized image coords 0-100. x1,y1 = top-left, x2,y2 = bottom-right. Be tight — only the defective region, not the whole road or scene. Example: a single pothole occupying the center-right of the road: [55, 60, 75, 80].

IMPORTANT: title_hi and description_hi must be in Devanagari script (हिंदी). Do not transliterate — actually translate. Example: "Large pothole on main road" → "मुख्य सड़क पर बड़ा गड्ढा".

Up to 4 issues if multiple distinct problems visible. Be aggressive — citizens depend on you to detect problems."""

import re

def extract_json(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    cleaned = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except Exception:
        pass
    match = re.search(r'\{.*\}', cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except Exception:
            pass
    return None

@app.route("/")
def index():
    return render_template("index.html", areapulse_url=os.environ.get("AREAPULSE_URL","https://areapulse-1.onrender.com/"))

@app.route("/api/analyze", methods=["POST"])
def analyze():
    raw = ""
    try:
        data = request.get_json(force=True)
        image_b64 = data.get("image","").strip()
        if not image_b64:
            return jsonify({"error":"No image provided"}), 400

        print(f"\n[ANALYZE] image bytes: {len(image_b64)} chars (base64)", flush=True)

        response = groq_client.chat.completions.create(
            model="llama-3.2-11b-vision-preview",
            messages=[{"role":"user","content":[
                {"type":"image_url","image_url":{"url":f"data:image/jpeg;base64,{image_b64}"}},
                {"type":"text","text":DETECT_PROMPT}
            ]}],
            max_tokens=1200, temperature=0.2,
        )
        raw = response.choices[0].message.content or ""
        print(f"[GROQ RAW]: {raw[:500]}", flush=True)

        parsed = extract_json(raw)
        if parsed is None:
            print(f"[ERROR] JSON extraction failed. Raw: {raw}", flush=True)
            return jsonify({"error": "AI returned unparseable response", "raw": raw[:300]}), 500

        if "issues" not in parsed and "issue_type" in parsed:
            parsed = {"issues":[parsed], "primary_index":0}

        print(f"[ANALYZE OK] issues={len(parsed.get('issues',[]))}", flush=True)
        return jsonify(parsed)

    except Exception as e:
        print(f"[ANALYZE ERROR] {type(e).__name__}: {e}", flush=True)
        print(f"[RAW WAS]: {raw[:300]}", flush=True)
        return jsonify({"error":f"{type(e).__name__}: {e}", "raw":raw[:300]}), 500

@app.route("/api/nearby")
def nearby():
    try:
        lat = float(request.args.get("lat",28.6139))
        lng = float(request.args.get("lng",77.2090))
        issues, ngos = [], []
        if db:
            for doc in db.collection("issues").where("status","==","open").limit(30).stream():
                d = doc.to_dict()
                if d.get("lat") and d.get("lng"):
                    dist = haversine(lat, lng, float(d["lat"]), float(d["lng"]))
                    if dist < 5:
                        issues.append({"id":doc.id,"tag":d.get("tag","other"),"severity":d.get("severity","medium"),
                            "title":d.get("title") or str(d.get("description",""))[:60],
                            "area":d.get("area","Delhi"),"distance_km":round(dist,2)})
            for doc in db.collection("ngos").limit(30).stream():
                d = doc.to_dict()
                if d.get("lat") and d.get("lng"):
                    dist = haversine(lat, lng, float(d["lat"]), float(d["lng"]))
                    if dist < 10:
                        ngos.append({"id":doc.id,"name":d.get("name","NGO"),"focus":d.get("focus",""),
                            "tag":d.get("tag","other"),"rating":d.get("rating",4.0),
                            "area":d.get("area","Delhi"),"distance_km":round(dist,2),"phone":d.get("phone","")})
        else:
            issues = [
                {"id":"1","tag":"pothole","severity":"high","title":"Large pothole on main road","area":"Rohini","distance_km":0.4},
                {"id":"2","tag":"garbage","severity":"medium","title":"Overflowing garbage bin","area":"Karol Bagh","distance_km":0.8},
                {"id":"3","tag":"streetlight","severity":"low","title":"Broken streetlight","area":"Lajpat Nagar","distance_km":1.1},
                {"id":"4","tag":"water","severity":"high","title":"Water main leak","area":"Dwarka","distance_km":1.6},
            ]
            ngos = [
                {"id":"1","name":"Delhi Green Mission","focus":"Sanitation & Waste","tag":"garbage","rating":4.5,"area":"Rohini","distance_km":0.6,"phone":"011-12345678"},
                {"id":"2","name":"Road Safety India","focus":"Road Infrastructure","tag":"pothole","rating":4.2,"area":"Dwarka","distance_km":1.3,"phone":"011-87654321"},
                {"id":"3","name":"Jal Seva Trust","focus":"Water & Sewage","tag":"water","rating":4.7,"area":"Hauz Khas","distance_km":2.1,"phone":"011-11223344"},
            ]
        issues.sort(key=lambda x:x["distance_km"])
        ngos.sort(key=lambda x:x["distance_km"])
        return jsonify({"issues":issues[:6],"ngos":ngos[:4]})
    except Exception as e:
        return jsonify({"error":str(e)}), 500


@app.route("/api/submit", methods=["POST"])
def submit():
    """
    Submit a camera-AR detection as an issue in the MAIN AreaPulse Firestore schema
    so it shows up in the user's My Issues on the main site.
    Mirrors database.insert_issue() exactly + camera-app extras.
    """
    try:
        data = request.get_json(force=True)

        # Support {"issues":[...], "primary_index":N, "user":..., ...} payload
        if "issues" in data:
            idx = data.get("primary_index", 0)
            issues_arr = data.get("issues", [])
            if idx >= len(issues_arr): idx = 0
            d = issues_arr[idx] if issues_arr else {}
            # Pull through top-level fields
            for k in ("lat","lng","user","image","area_estimate"):
                if k in data and k not in d:
                    d[k] = data[k]
            data = d

        user = (data.get("user") or "anonymous").strip() or "anonymous"
        tag = data.get("issue_type") or "other"
        severity = data.get("severity") or "medium"
        area = data.get("area_estimate") or "Delhi"
        desc = data.get("description") or "Civic issue detected via AR scan"
        title = data.get("title") or ""
        title_hi = data.get("title_hi") or ""
        desc_hi = data.get("description_hi") or ""
        image = data.get("image") or None  # full data URL

        try: lat = float(data.get("lat")) if data.get("lat") is not None else 28.6139
        except: lat = 28.6139
        try: lng = float(data.get("lng")) if data.get("lng") is not None else 77.2090
        except: lng = 77.2090

        if db:
            issue_id = _next_int_id("issues")
            doc = {
                # ── Main app's insert_issue schema (must match exactly) ──
                'id':           issue_id,
                'area':         area,
                'description':  desc,
                'tag':          tag,
                'user':         user,
                'lat':          lat,
                'lng':          lng,
                'image':        image,
                'severity':     severity,
                'landmark':     '',
                'contact':      '',
                'timestamp':    time.time(),     # float, NOT SERVER_TIMESTAMP
                'upvotes':      0,
                'priority':     0.0,
                'verified':     0,
                'is_verified':  False,
                'is_escalated': False,
                'status':       'open',
                'assigned_to':  None,
                # ── Camera-app extras (non-conflicting, main app ignores) ──
                'source':                'camera_app',
                'title':                 title,
                'title_hi':              title_hi,
                'description_hi':        desc_hi,
                'ai_confidence':         int(data.get("confidence", 0) or 0),
                'recommended_authority': data.get("recommended_authority", "MCD"),
                'hazard_level':          data.get("hazard_level", "medium"),
                'estimated_repair_time': data.get("estimated_repair_time", "3-7 days"),
            }
            db.collection("issues").document(str(issue_id)).set(doc)
            print(f"[SUBMIT OK] issue#{issue_id} by user={user} tag={tag}", flush=True)
            return jsonify({"status":"ok", "id": issue_id})

        return jsonify({"status":"ok", "id": f"CAM-{int(time.time())}", "note": "Firebase not configured"})

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/geocode")
def geocode():
    import urllib.request, urllib.parse
    lat = request.args.get("lat","")
    lng = request.args.get("lng","")
    try:
        url = "https://nominatim.openstreetmap.org/reverse?" + urllib.parse.urlencode({
            "format":"json","lat":lat,"lon":lng,"zoom":"14","addressdetails":"1"
        })
        req = urllib.request.Request(url, headers={"User-Agent":"AreaPulseAR/1.0"})
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.loads(r.read().decode())
        addr = data.get("address", {})
        area = (addr.get("suburb") or addr.get("neighbourhood") or
                addr.get("city_district") or addr.get("town") or
                addr.get("village") or addr.get("city") or
                addr.get("county") or "Unknown")
        city = addr.get("city") or addr.get("town") or addr.get("state_district") or ""
        return jsonify({"area": area, "city": city, "full": data.get("display_name","")})
    except Exception as e:
        return jsonify({"area":"Unknown","error":str(e)}), 500


@app.route("/api/health")
def health():
    return jsonify({"status":"ok","groq":bool(os.environ.get("GROQ_API_KEY")),"firebase":db is not None})


if __name__ == "__main__":
    port = int(os.environ.get("PORT",5001))
    app.run(debug=True, host="0.0.0.0", port=port)

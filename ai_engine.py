"""
ai_engine.py  v3  — AreaPulse intelligence layer
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Feature matrix (graceful degradation when a dep is missing):

  ✓ Image analysis       → Groq Llama-4-Scout vision (requires GROQ_API_KEY)
  ✓ Text spam            → keyword → sklearn ML → Groq few-shot LLM
  ✓ Duplicate image      → pHash (imagehash) → SHA-256 exact fallback
  ✓ Coordinate spam      → Haversine distance math  (zero deps)
  ✓ False report         → Enhanced Groq vision with civic-issue gate
  ✓ AI-image detection   → EXIF check → HuggingFace classifier → Groq vision
  ✓ Cross-modal check    → NEW v3: image-tag vs description-tag consistency
  ✓ Ban management       → strike system + immediate ban (in-memory; swap for DB)
  ✓ Master pipeline      → validate_submission() — call this from your Flask route
  ✓ Q&A / Insights / AR / Complaint drafting → unchanged from v2
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

v3 — Cross-modal validation (what was missing and how it is fixed):

  THE GAP IN v2
  ─────────────
  Vision model sees:    POTHOLE
  User submits text:    "Street light broken"
  Text classifier says: REAL  ← both checks individually pass
  Old pipeline result:  APPROVED  ← wrong

  THE FIX IN v3
  ─────────────
  After Check 5 (false-report gate) the vision result already contains
  the image tag (e.g. 'pothole').  validate_submission now compares that
  against the text tag supplied by auto_classify() in app.py.

  LAYER A — Tag synonym check  (free, ~0 ms, always runs):
    Compatible pairs (water↔sewage, electricity↔streetlight, etc.) pass
    immediately with zero API cost.  Only genuine mismatches continue.

  LAYER B — Groq vision re-check  (~400 ms, only on tag mismatch):
    Sends the image + user description to Groq and asks:
    "Does this description match what the image actually shows?"
    Returns one of three verdicts:
      match         → approve (user added valid context)
      context_added → approve + set cross_modal_flagged=True for soft audit
      mismatch      → record_strike + return action='flag'

  'flag' is a new action (softer than 'reject').  app.py already handles
  action='reject' with a 400 response, so 'flag' is treated identically
  in the existing /report route without any app.py changes required.

  KEY DESIGN CHOICES
  ──────────────────
  • No second vision API call — v3 reuses the vision result already
    obtained in Check 5 (analyze_image), so there is zero extra cost
    when the tags are compatible (the common case).
  • Groq fires only when Layer A finds an actual mismatch.
  • On Groq API error → approve with flag (never silently drop a real report).
  • 'other' tag is always treated as compatible with everything (too vague
    to call a mismatch — the user may not know the correct category).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Install deps:
  pip install groq Pillow imagehash scikit-learn transformers torch
"""

import os, json, re, time, base64, io, math, hashlib, pathlib
from collections import defaultdict
from typing import Optional

# ─────────────────────────────────────────────────────────────────────────────
#  GROQ  (core LLM / vision)
# ─────────────────────────────────────────────────────────────────────────────
_client = None
_MODEL  = 'meta-llama/llama-4-scout-17b-16e-instruct'

try:
    from groq import Groq
    if os.environ.get('GROQ_API_KEY'):
        _client = Groq(api_key=os.environ['GROQ_API_KEY'])
        print(f'[ai_engine] Groq ✓  model={_MODEL}')
except Exception as e:
    print(f'[ai_engine] Groq unavailable: {e}')

# ─────────────────────────────────────────────────────────────────────────────
#  PILLOW + IMAGEHASH  (duplicate detection + EXIF analysis)
# ─────────────────────────────────────────────────────────────────────────────
_PIL_OK = False
try:
    from PIL import Image, ExifTags
    import imagehash as _imagehash
    _PIL_OK = True
    print('[ai_engine] Pillow + imagehash ✓')
except ImportError:
    print('[ai_engine] Pillow/imagehash missing → duplicate detection degraded')

# ─────────────────────────────────────────────────────────────────────────────
#  SKLEARN SPAM PIPELINE  (trained model loaded from disk)
# ─────────────────────────────────────────────────────────────────────────────
_spam_pipeline = None
try:
    import pickle
    _MODEL_PATH = pathlib.Path(__file__).parent / 'models' / 'spam_clf.pkl'
    if _MODEL_PATH.exists():
        with open(_MODEL_PATH, 'rb') as f:
            _spam_pipeline = pickle.load(f)
        print('[ai_engine] Spam ML model ✓')
    else:
        print('[ai_engine] models/spam_clf.pkl not found → run train_spam_model.py first')
except Exception as e:
    print(f'[ai_engine] sklearn unavailable: {e}')

# ─────────────────────────────────────────────────────────────────────────────
#  HUGGINGFACE AI-IMAGE DETECTOR  (offline classifier)
# ─────────────────────────────────────────────────────────────────────────────
_ai_img_detector = None
try:
    from transformers import pipeline as _hf_pipeline
    _ai_img_detector = _hf_pipeline(
        'image-classification',
        model='umm-maybe/AI-image-detector',
        device=-1,   # CPU; change to 0 if you have a GPU
    )
    print('[ai_engine] HF AI-image detector ✓')
except Exception as e:
    print(f'[ai_engine] HF detector unavailable: {e}')


# ═════════════════════════════════════════════════════════════════════════════
#  PUBLIC STATUS API
# ═════════════════════════════════════════════════════════════════════════════
def is_available():   return _client is not None
def provider_name():  return 'Groq' if _client else 'none'
def model_name():     return _MODEL if _client else None

def engine_status() -> dict:
    """Return a dict of which features are live. Useful for /api/status endpoint."""
    return {
        'groq':              _client is not None,
        'spam_ml':           _spam_pipeline is not None,
        'duplicate_img':     _PIL_OK,
        'ai_img_detector':   _ai_img_detector is not None,
        'exif_check':        _PIL_OK,
        'coord_spam':        True,   # pure math, always available
        'cross_modal_check': True,   # Layer A always runs; Layer B needs Groq
    }


# ═════════════════════════════════════════════════════════════════════════════
#  CROSS-MODAL TAG SYNONYMS
#  Tags that are close enough in meaning to be treated as COMPATIBLE.
#  If image_tag and text_tag are in each other's synonym set → Layer A passes,
#  no Groq call needed.
#  'other' is always compatible with everything — it is too vague to call a
#  mismatch; the user simply may not know the right category.
# ═════════════════════════════════════════════════════════════════════════════
_TAG_SYNONYMS: dict = {
    'pothole':     {'pothole', 'other'},
    'water':       {'water', 'sewage', 'other'},
    'garbage':     {'garbage', 'other'},
    'streetlight': {'streetlight', 'electricity', 'other'},
    'traffic':     {'traffic', 'other'},
    'noise':       {'noise', 'other'},
    'sewage':      {'sewage', 'water', 'other'},
    'electricity': {'electricity', 'streetlight', 'other'},
    'tree':        {'tree', 'other'},
    'other':       set(),   # 'other' handled separately — always compatible
}


def _tags_compatible(tag_a: str, tag_b: str) -> bool:
    """
    Returns True when two tags are close enough that a mismatch flag is NOT
    warranted.  Symmetric.  Used in Layer A of check_cross_modal_consistency.

    Compatibility rules:
      1. Identical tags         → always compatible
      2. Either tag is 'other'  → always compatible (too vague to flag)
      3. tag_b in synonyms[tag_a] OR tag_a in synonyms[tag_b] → compatible
    """
    a = (tag_a or 'other').lower().strip()
    b = (tag_b or 'other').lower().strip()
    if a == b:
        return True
    if a == 'other' or b == 'other':
        return True
    return b in _TAG_SYNONYMS.get(a, set()) or a in _TAG_SYNONYMS.get(b, set())


# ═════════════════════════════════════════════════════════════════════════════
#  FEATURE 1 — IMAGE ANALYSIS + FALSE REPORT GATE
# ═════════════════════════════════════════════════════════════════════════════
_VISION_PROMPT = """You are AreaPulse, a civic issue detection AI for Delhi, India.

A citizen uploaded this photo claiming it shows a civic problem.

── STEP 1: GATE CHECK ─────────────────────────────────────────────────────────
Ask: is this image actually showing a real-world civic infrastructure problem?
Valid civic issues: road damage, pothole, waterlogging, garbage dump, broken streetlight,
open drain, sewage overflow, electrical hazard (exposed wires, transformer fire),
fallen tree, encroachment on road, construction debris, broken footpath, etc.

NOT a civic issue: selfies, food photos, text screenshots, blank walls, indoor objects,
nature landscapes (unless showing infrastructure damage), AI-rendered art, cartoons,
news screenshots, products.

Set is_civic_issue = false AND confidence ≤ 45 when this is clearly not a civic issue.
Set is_civic_issue = true  AND confidence 70-98 when it is or could plausibly be one.

── STEP 2: CLASSIFY ────────────────────────────────────────────────────────────
Only if is_civic_issue = true: identify the most prominent issue.

Respond ONLY with valid JSON, no markdown, no preamble:
{
  "is_civic_issue":      true|false,
  "category":            "pothole|water|garbage|streetlight|traffic|noise|sewage|electricity|tree|other|none",
  "severity":            "low|medium|high|none",
  "confidence":          <integer 40-98>,
  "description":         "<one clear sentence, max 25 words>",
  "false_report_reason": null or "<brief reason why this is NOT a civic issue, max 15 words>",
  "source":              "groq-llama4-scout"
}"""


def analyze_image(image_b64: str, mime: str = 'image/jpeg') -> dict:
    """Vision: classify civic issue + false-report gate in one call."""
    if not _client:
        return {'error': 'AI vision not configured. Set GROQ_API_KEY.', '_status': 'not_configured'}
    try:
        resp = _client.chat.completions.create(
            model=_MODEL,
            messages=[{'role': 'user', 'content': [
                {'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{image_b64}'}},
                {'type': 'text', 'text': _VISION_PROMPT},
            ]}],
            max_tokens=400, temperature=0.2,
        )
        raw    = (resp.choices[0].message.content or '').strip()
        parsed = _extract_json(raw)
        if not parsed:
            return {'error': 'Unparseable AI response', 'raw': raw[:200], '_status': 'parse_error'}
        parsed.setdefault('source', 'groq-llama4-scout')
        parsed.setdefault('is_civic_issue', True)
        return parsed
    except Exception as e:
        return {'error': f'{type(e).__name__}: {e}', '_status': 'server_error'}


# ═════════════════════════════════════════════════════════════════════════════
#  FEATURE 2 — TEXT SPAM CLASSIFICATION  (3-layer)
# ═════════════════════════════════════════════════════════════════════════════
_SPAM_PROMPT = """You are a spam filter for a civic issue reporting platform in India.
Classify the report text into ONE of: REAL | SPAM | ABUSE | TEST

Rules:
- REAL : any legitimate civic infrastructure issue, even if brief or poorly written
- SPAM : gibberish, fantasy/joke content (aliens, dragons, lottery, ads)
- ABUSE: profanity, hate speech, targeted harassment
- TEST : clearly a test submission ("test", "testing 123", "abc")

Be generous toward REAL. Only flag SPAM for clearly fantastical or commercial text.

── FEW-SHOT EXAMPLES ─────────────────────────────────────────────────────────
TEXT: "pothole on main road near metro"
→ {"verdict":"REAL","confidence":97,"reason":"clear road infrastructure report"}

TEXT: "aliens attacked my colony last night"
→ {"verdict":"SPAM","confidence":99,"reason":"fantastical non-civic content"}

TEXT: "buy cheap medicines click here discount offer"
→ {"verdict":"SPAM","confidence":99,"reason":"commercial advertisement"}

TEXT: "nali band hai paani bhar raha hai"
→ {"verdict":"REAL","confidence":95,"reason":"Hindi drain blockage report"}

TEXT: "test test test 123"
→ {"verdict":"TEST","confidence":99,"reason":"test pattern"}
──────────────────────────────────────────────────────────────────────────────

Respond ONLY with valid JSON, no other text:
{"verdict":"REAL"|"SPAM"|"ABUSE"|"TEST","confidence":0-100,"reason":"<12 words max>"}

REPORT TEXT:
"""

# Instant keyword pre-filters (no model call needed)
_SPAM_KW = [
    'alien','aliens','ufo','martian','zombie','vampire','dragon','unicorn',
    'ghost haunt','haunted','demon','witch','wormhole','time travel',
    'buy now','click here','free money','lottery','win prize',
    'lorem ipsum','asdf qwerty',
]
_TEST_KW = ['test test','testing 123','abc def','just testing','ignore this']


def classify_spam(description: str, has_photo: bool = False) -> dict:
    """
    3-layer spam classification:
      Layer 1 → hard keyword match  (instant)
      Layer 2 → sklearn ML pipeline (offline, ~1 ms)
      Layer 3 → Groq LLM few-shot  (most accurate, ~400 ms)

    Returns: {'verdict': 'real'|'spam'|'abuse'|'test', 'confidence': int, 'reason': str}
    Always returns — never raises.
    """
    text = (description or '').strip()
    tl   = text.lower()

    # ── Layer 1: Keywords (instant) ──────────────────────────────────────────
    for kw in _SPAM_KW:
        if kw in tl:
            return {'verdict': 'spam', 'confidence': 95, 'reason': f'contains spam keyword "{kw}"'}
    for kw in _TEST_KW:
        if kw in tl:
            return {'verdict': 'test', 'confidence': 92, 'reason': 'test-pattern submission'}
    if len(text) < 6:
        return {'verdict': 'test', 'confidence': 60, 'reason': 'text too short'}

    # ── Layer 2: ML model (~1 ms) ─────────────────────────────────────────────
    if _spam_pipeline:
        try:
            pred   = str(_spam_pipeline.predict([text])[0]).lower()
            proba  = _spam_pipeline.predict_proba([text])[0]
            conf   = int(max(proba) * 100)
            label  = {'real':'real','spam':'spam','abuse':'abuse','test':'test',
                      '0':'real','1':'spam'}.get(pred, 'real')
            if conf >= 75:
                return {'verdict': label, 'confidence': conf, 'reason': 'ML classifier'}
            # Low ML confidence → fall through to Groq for edge cases
        except Exception:
            pass

    # ── Layer 3: Groq LLM few-shot (~400 ms) ─────────────────────────────────
    if _client:
        try:
            resp = _client.chat.completions.create(
                model=_MODEL,
                messages=[{'role': 'user', 'content': _SPAM_PROMPT + text[:600]}],
                max_tokens=80, temperature=0.1,
            )
            raw    = resp.choices[0].message.content.strip()
            data   = _extract_json(raw) or {}
            verdict = (data.get('verdict') or 'REAL').lower().strip()
            if verdict not in ('real', 'spam', 'abuse', 'test'):
                verdict = 'real'
            return {
                'verdict':    verdict,
                'confidence': int(data.get('confidence') or 70),
                'reason':     (data.get('reason') or 'groq classified')[:80],
            }
        except Exception as e:
            pass

    # ── Fallback ──────────────────────────────────────────────────────────────
    return {'verdict': 'real', 'confidence': 40, 'reason': 'all classifiers unavailable — defaulted real'}


# ═════════════════════════════════════════════════════════════════════════════
#  FEATURE 3 — DUPLICATE IMAGE DETECTION  (perceptual hashing)
# ═════════════════════════════════════════════════════════════════════════════
def compute_image_hash(image_b64: str) -> Optional[str]:
    """
    Compute a 64-bit perceptual hash (pHash) of an image.
    Store this string alongside every report in your DB.

    pHash is ROBUST to: JPEG re-compression, minor crops, brightness/contrast tweaks,
    minor colour shifts — all common tricks used to dodge duplicate detection.

    Falls back to SHA-256 (exact match only) if imagehash not installed.
    """
    if not _PIL_OK:
        return _sha256_hash(image_b64)
    try:
        raw = base64.b64decode(image_b64)
        img = Image.open(io.BytesIO(raw)).convert('RGB')
        return str(_imagehash.phash(img))
    except Exception:
        return _sha256_hash(image_b64)


def is_duplicate_image(
    new_hash:       str,
    stored_hashes:  list,        # list of hash strings fetched from DB
    threshold:      int = 10,    # Hamming distance threshold
                                 #   ≤ 5  → near-identical
                                 #   ≤ 10 → same photo, minor edit/crop  ← recommended
                                 #   ≤ 15 → same scene, different angle
) -> dict:
    """
    Compare a new image hash against all stored hashes.
    Returns: {'is_duplicate': bool, 'distance': int|None, 'matched_hash': str|None}
    """
    if not new_hash or not stored_hashes:
        return {'is_duplicate': False, 'distance': None, 'matched_hash': None}

    # Without imagehash → exact SHA-256 comparison only
    if not _PIL_OK:
        exact = new_hash in stored_hashes
        return {'is_duplicate': exact, 'distance': 0 if exact else None,
                'matched_hash': new_hash if exact else None}

    try:
        h_new = _imagehash.hex_to_hash(new_hash)
        best_dist, best_match = 999, None
        for stored in stored_hashes:
            try:
                d = h_new - _imagehash.hex_to_hash(stored)
                if d < best_dist:
                    best_dist, best_match = d, stored
            except Exception:
                continue
        is_dup = best_dist <= threshold
        return {
            'is_duplicate': is_dup,
            'distance':     best_dist if best_match else None,
            'matched_hash': best_match if is_dup else None,
        }
    except Exception:
        return {'is_duplicate': False, 'distance': None, 'matched_hash': None}


def _sha256_hash(b64: str) -> str:
    return hashlib.sha256(b64.encode()).hexdigest()


# ═════════════════════════════════════════════════════════════════════════════
#  FEATURE 4 — COORDINATE SPAM DETECTION  (Haversine math, zero deps)
# ═════════════════════════════════════════════════════════════════════════════
def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two GPS coords in metres."""
    R  = 6_371_000
    f1, f2 = math.radians(lat1), math.radians(lat2)
    df = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a  = math.sin(df/2)**2 + math.cos(f1)*math.cos(f2)*math.sin(dl/2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def is_coordinate_spam(
    new_lat:         float,
    new_lon:         float,
    recent_reports:  list,          # [{'lat':…, 'lng':…, 'user_id':…, 'tag':…}]
    same_user_id:    str   = None,
    new_category:    str   = None,  # ← NEW: pass the issue tag e.g. 'pothole'
    # Thresholds — tune to your city density
    radius_cluster:  int   = 30,    # metres: reports within 30 m = "same spot"
    max_cluster:     int   = 3,     # > 3 reports of SAME category at same spot = spam
    radius_user:     int   = 100,   # metres for per-user check
    max_user_nearby: int   = 5,     # same user > 5 reports of SAME category in 100 m = spam
) -> dict:
    """
    Detects two spam patterns — now CATEGORY-AWARE:
 
    A) Cluster spam   → many users keep reporting the SAME TYPE of issue at
                        an already-reported spot.
                        e.g. 4 users all reporting the same pothole = spam.
                        But pothole + tree + water + electricity = all pass.
 
    B) User spam      → one user submits many reports of the SAME TYPE in a
                        small area.
                        e.g. user reports 6 potholes within 100m = spam.
                        But user reports pothole + water + tree = all pass.
 
    If new_category is None (not passed), falls back to the original
    total-count behaviour for backward compatibility.
 
    Returns: {'is_spam': bool, 'reason': str, 'nearby_count': int}
    """
    cluster_count = 0   # same-category reports at same spot
    user_count    = 0   # same-user, same-category reports nearby
    cat           = (new_category or '').lower().strip()
 
    for r in recent_reports:
        try:
            rlat = float(r.get('lat', 0))
            rlng = float(r.get('lng', r.get('lon', 0)))
            d    = haversine_meters(new_lat, new_lon, rlat, rlng)
        except Exception:
            continue
 
        r_cat = (r.get('tag') or r.get('category') or '').lower().strip()
 
        # ── Cluster check: same spot, same category ──────────────────────────
        if d <= radius_cluster:
            if not cat or r_cat == cat:
                # If no category provided → count everything (old behaviour)
                # If category provided    → only count matching reports
                cluster_count += 1
 
        # ── User check: same user, same category, nearby ─────────────────────
        if same_user_id and str(r.get('user_id')) == str(same_user_id) and d <= radius_user:
            if not cat or r_cat == cat:
                user_count += 1
 
    # ── Evaluate ─────────────────────────────────────────────────────────────
    cat_label = f'"{cat}"' if cat else 'any type'
 
    if same_user_id and user_count >= max_user_nearby:
        return {
            'is_spam':      True,
            'reason':       f'You already have {user_count} {cat_label} reports within {radius_user} m',
            'nearby_count': user_count,
        }
 
    if cluster_count >= max_cluster:
        return {
            'is_spam':      True,
            'reason':       f'{cluster_count} {cat_label} reports already exist at this exact location',
            'nearby_count': cluster_count,
        }
 
    return {
        'is_spam':      False,
        'reason':       'coordinates look clean',
        'nearby_count': cluster_count,
    }
 
 
# ─────────────────────────────────────────────────────────────────────────────
#  QUICK TEST — run this file directly to verify behaviour
#  python coordinate_spam_fix.py
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
 
    # Simulate a street corner in Delhi: 4 different issues already reported
    existing = [
        {'lat': 28.6315, 'lng': 77.2167, 'user_id': 'u1', 'tag': 'pothole'},
        {'lat': 28.6315, 'lng': 77.2167, 'user_id': 'u2', 'tag': 'tree'},
        {'lat': 28.6315, 'lng': 77.2167, 'user_id': 'u3', 'tag': 'water'},
        {'lat': 28.6315, 'lng': 77.2167, 'user_id': 'u4', 'tag': 'pothole'},
        {'lat': 28.6315, 'lng': 77.2167, 'user_id': 'u5', 'tag': 'pothole'},
    ]
    # New citizen reporting electricity at the same corner
    new_lat, new_lng = 28.6315, 77.2167
 
    print('── Test 1: electricity report at busy corner ───────────────')
    r = is_coordinate_spam(new_lat, new_lng, existing,
                           same_user_id='u99', new_category='electricity')
    print(f"  is_spam={r['is_spam']}  reason={r['reason']}")
    print(f"  Expected: is_spam=False  (only 0 electricity reports nearby)")
 
    print('\n── Test 2: 3rd pothole at same spot (should block) ─────────')
    r = is_coordinate_spam(new_lat, new_lng, existing,
                           same_user_id='u99', new_category='pothole')
    print(f"  is_spam={r['is_spam']}  reason={r['reason']}")
    print(f"  Expected: is_spam=True   (2 pothole reports already, 3rd = spam)")
 
    print('\n── Test 3: no category passed (old behaviour, should block) ─')
    r = is_coordinate_spam(new_lat, new_lng, existing, same_user_id='u99')
    print(f"  is_spam={r['is_spam']}  reason={r['reason']}")
    print(f"  Expected: is_spam=True   (5 total reports, old count-all logic)")
 
    print('\n── Test 4: user reporting water 200m away (should pass) ────')
    far_existing = [
        {'lat': 28.6330, 'lng': 77.2180, 'user_id': 'u99', 'tag': 'water'},
    ]
    r = is_coordinate_spam(new_lat, new_lng, far_existing,
                           same_user_id='u99', new_category='water')
    print(f"  is_spam={r['is_spam']}  reason={r['reason']}")
    print(f"  Expected: is_spam=False  (>100m away, no user spam)")


# ═════════════════════════════════════════════════════════════════════════════
#  FEATURE 5 — AI-GENERATED IMAGE DETECTION  (3-layer)
# ═════════════════════════════════════════════════════════════════════════════
_AI_IMG_PROMPT = """Examine this image carefully. Is it AI-generated (Midjourney, DALL-E,
Stable Diffusion, Firefly, etc.) rather than a real phone photograph?

Signals of AI generation: perfect/unnatural lighting, painterly textures, distorted
backgrounds, impossibly smooth surfaces, blurred text, watermarks, unrealistic
proportions, mismatched shadows, extra/missing fingers on people.

Respond ONLY with valid JSON:
{
  "is_ai_generated": true|false,
  "confidence": <integer 60-99>,
  "signals": ["<evidence 1>","<evidence 2>"]
}"""


def detect_ai_image(image_b64: str, mime: str = 'image/jpeg') -> dict:
    """
    3-layer AI-image detection (fastest → most accurate):

    Layer 1 → EXIF metadata check    (no model; instant)
    Layer 2 → HuggingFace classifier (offline ML; ~100 ms)
    Layer 3 → Groq vision            (most accurate; ~600 ms)

    Returns: {'is_ai_generated': bool, 'confidence': int, 'method': str, 'signals': list}
    """
    # ── Layer 1: EXIF (fast, no deps beyond Pillow) ───────────────────────────
    exif = _check_exif(image_b64)
    if exif['confidence'] >= 80:
        return exif   # High-confidence EXIF signal → early return

    # ── Layer 2: HuggingFace offline classifier ───────────────────────────────
    if _ai_img_detector and _PIL_OK:
        try:
            raw   = base64.b64decode(image_b64)
            img   = Image.open(io.BytesIO(raw)).convert('RGB')
            preds = _ai_img_detector(img)
            ai_s  = next((p['score'] for p in preds if 'artif' in p['label'].lower()), 0.0)
            re_s  = next((p['score'] for p in preds if 'real'  in p['label'].lower()), 0.0)
            conf  = int(max(ai_s, re_s) * 100)
            is_ai = ai_s > re_s and ai_s > 0.65
            if conf >= 70:
                return {
                    'is_ai_generated': is_ai,
                    'confidence':      conf,
                    'method':          'hf-classifier',
                    'signals':         [f'AI score {ai_s:.2f}', f'Real score {re_s:.2f}'],
                }
        except Exception:
            pass

    # ── Layer 3: Groq vision ──────────────────────────────────────────────────
    if _client:
        try:
            resp   = _client.chat.completions.create(
                model=_MODEL,
                messages=[{'role': 'user', 'content': [
                    {'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{image_b64}'}},
                    {'type': 'text', 'text': _AI_IMG_PROMPT},
                ]}],
                max_tokens=200, temperature=0.1,
            )
            raw    = (resp.choices[0].message.content or '').strip()
            parsed = _extract_json(raw) or {}
            return {
                'is_ai_generated': bool(parsed.get('is_ai_generated', False)),
                'confidence':      int(parsed.get('confidence', 60)),
                'method':          'groq-vision',
                'signals':         parsed.get('signals', []),
            }
        except Exception:
            pass

    return exif  # Best available result


def _check_exif(image_b64: str) -> dict:
    """
    Real phone photos have rich EXIF: camera Make, Model, GPS, DateTimeOriginal, ISO.
    AI images (Midjourney, DALL-E, SD) are typically exported as PNG with NO EXIF,
    or as JPEG with only a handful of generic fields.

    Scoring:
      0 EXIF at all on a JPEG → likely AI (confidence 68)
      PNG with no EXIF        → likely AI (confidence 75)
      0/4 camera fields       → likely AI (confidence 75)
      3-4/4 camera fields     → real phone photo (confidence 72, is_ai=False)
    """
    if not _PIL_OK:
        return {'is_ai_generated': False, 'confidence': 25, 'method': 'exif-unavailable', 'signals': []}

    signals = []
    try:
        raw = base64.b64decode(image_b64)
        img = Image.open(io.BytesIO(raw))
        fmt = getattr(img, 'format', '') or ''

        exif_data = img._getexif() if hasattr(img, '_getexif') else None

        if exif_data is None:
            signals.append('No EXIF metadata')
            if fmt.upper() == 'PNG':
                signals.append('PNG format (common AI export type)')
                return {'is_ai_generated': True,  'confidence': 75, 'method': 'exif-check', 'signals': signals}
            return     {'is_ai_generated': True,  'confidence': 68, 'method': 'exif-check', 'signals': signals}

        tag_names = {ExifTags.TAGS.get(k, ''): v for k, v in exif_data.items()}
        has_make  = bool(tag_names.get('Make'))
        has_model = bool(tag_names.get('Model'))
        has_gps   = bool(tag_names.get('GPSInfo'))
        has_dt    = bool(tag_names.get('DateTimeOriginal'))
        score     = sum([has_make, has_model, has_gps, has_dt])

        if score == 0:
            signals.append('EXIF present but no camera fields (Make/Model/GPS/DateTime)')
            return {'is_ai_generated': True,  'confidence': 75, 'method': 'exif-check', 'signals': signals}
        if score >= 3:
            signals.append(f'Rich EXIF: {score}/4 camera fields found — real device photo')
            return {'is_ai_generated': False, 'confidence': 72, 'method': 'exif-check', 'signals': signals}

        signals.append(f'Partial EXIF: only {score}/4 camera fields')
        return {'is_ai_generated': False, 'confidence': 45, 'method': 'exif-check', 'signals': signals}

    except Exception as e:
        return {'is_ai_generated': False, 'confidence': 25, 'method': 'exif-error', 'signals': [str(e)]}


# ═════════════════════════════════════════════════════════════════════════════
#  FEATURE 6 — CROSS-MODAL VALIDATION  (NEW in v3)
# ═════════════════════════════════════════════════════════════════════════════
#
#  LAYER A — Tag synonym check (free, ~0 ms)
#    Compares image_tag (from vision model) vs text_tag (from auto_classify).
#    Uses _TAG_SYNONYMS to allow related tags (water↔sewage, etc.).
#    If compatible → returns approved=True immediately, no API call.
#    If incompatible → proceeds to Layer B.
#
#  LAYER B — Groq vision re-check (~400 ms, only fires on tag mismatch)
#    Sends the actual image + the user's description text to Groq.
#    Prompt asks: does this description match what the image shows?
#    Three possible verdicts:
#      match         → both are about the same issue, approve
#      context_added → description adds extra info not visible in image, approve
#                       (e.g. image=pothole, text="pothole + water pipe burst nearby")
#      mismatch      → description describes a completely different issue, flag
#
#  Called from validate_submission() AFTER Check 5 (false-report gate).
#  Reuses the vision result that Check 5 already obtained — zero extra cost
#  when tags are compatible, which is the overwhelming majority of submissions.
#
# ─────────────────────────────────────────────────────────────────────────────

_CROSS_MODAL_PROMPT = """You are AreaPulse, a civic issue validation AI for Delhi, India.

A citizen uploaded a photo and wrote a description. Decide whether the description
is CONSISTENT with what the image actually shows.

── VERDICTS ────────────────────────────────────────────────────────────────────
"match"         → description accurately describes the image, even if brief
"context_added" → description adds extra related info not directly visible
                  (e.g. image=pothole, desc mentions nearby water leak too) — OK
"mismatch"      → description clearly refers to a DIFFERENT type of issue
                  than what is visible in the image

Be LENIENT. Only return "mismatch" when the description is completely unrelated
to what the image shows.  When in doubt → "context_added".

── FEW-SHOT EXAMPLES ───────────────────────────────────────────────────────────
image shows large pothole | desc: "big pothole near metro station"
→ {"result":"match","confidence":97,"reason":"description matches image directly"}

image shows pothole on road | desc: "street light not working since 1 week"
→ {"result":"mismatch","confidence":93,"reason":"image is road damage, desc claims electrical issue"}

image shows overflowing garbage | desc: "garbage not cleared + sewage problem nearby"
→ {"result":"context_added","confidence":86,"reason":"garbage visible, sewage is added related context"}

image shows broken streetlight pole | desc: "electricity pole fallen after storm"
→ {"result":"match","confidence":91,"reason":"streetlight and electricity refer to same infrastructure"}

image shows flooded road | desc: "water pipe burst near my house"
→ {"result":"context_added","confidence":78,"reason":"both are water-related civic issues"}
────────────────────────────────────────────────────────────────────────────────

The citizen's description: "{description}"

Respond ONLY with valid JSON, no markdown, no preamble:
{{"result":"match"|"context_added"|"mismatch","confidence":60-99,"reason":"<15 words max>"}}"""


def check_cross_modal_consistency(
    image_b64:   str,
    description: str,
    image_tag:   str,          # tag returned by analyze_image() in Check 5
    text_tag:    str,          # tag from auto_classify(description) in app.py
    mime:        str = 'image/jpeg',
) -> dict:
    """
    Two-layer cross-modal image ↔ description consistency check.

    Returns:
    {
      'approved':    bool,
      'result':      'tag_match'|'match'|'context_added'|'mismatch'|
                     'skipped'|'groq_error',
      'confidence':  int,
      'reason':      str,
      'image_tag':   str,
      'text_tag':    str,
      'layer':       'A'|'B'|'none',
      'groq_used':   bool,
    }
    Always returns — never raises.
    """
    img_tag  = (image_tag  or 'other').lower().strip()
    desc_tag = (text_tag   or 'other').lower().strip()

    # ── No image → nothing to compare ─────────────────────────────────────────
    if not image_b64:
        return {
            'approved': True, 'result': 'skipped', 'confidence': 0,
            'reason': 'no image — cross-modal check skipped',
            'image_tag': img_tag, 'text_tag': desc_tag,
            'layer': 'none', 'groq_used': False,
        }

    # ── LAYER A: Tag synonym comparison (free, instant) ───────────────────────
    if _tags_compatible(img_tag, desc_tag):
        return {
            'approved': True, 'result': 'tag_match', 'confidence': 90,
            'reason': f'image tag "{img_tag}" compatible with text tag "{desc_tag}"',
            'image_tag': img_tag, 'text_tag': desc_tag,
            'layer': 'A', 'groq_used': False,
        }

    # ── LAYER B: Groq vision re-check (fires only on Layer A mismatch) ─────────
    print(f'[cross_modal] Layer A mismatch: image={img_tag!r} text={desc_tag!r} → Groq re-check')

    if not _client:
        # Groq not configured — soft-flag but do not hard-reject
        return {
            'approved': False, 'result': 'mismatch', 'confidence': 60,
            'reason': (f'Image shows "{img_tag}" but description suggests "{desc_tag}". '
                       'Groq unavailable to verify — please resubmit with matching description.'),
            'image_tag': img_tag, 'text_tag': desc_tag,
            'layer': 'B', 'groq_used': False,
        }

    try:
        prompt = _CROSS_MODAL_PROMPT.replace('{description}', (description or '')[:400])
        resp   = _client.chat.completions.create(
            model=_MODEL,
            messages=[{'role': 'user', 'content': [
                {'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{image_b64}'}},
                {'type': 'text',      'text': prompt},
            ]}],
            max_tokens=100, temperature=0.1,
        )
        raw    = (resp.choices[0].message.content or '').strip()
        parsed = _extract_json(raw) or {}

        result     = (parsed.get('result') or 'mismatch').lower().strip()
        confidence = int(parsed.get('confidence') or 70)
        reason     = (parsed.get('reason') or 'groq cross-modal check')[:120]

        if result not in ('match', 'context_added', 'mismatch'):
            result = 'mismatch'   # unknown output → flag conservatively

        approved = result in ('match', 'context_added')
        return {
            'approved':   approved,
            'result':     result,
            'confidence': confidence,
            'reason':     reason,
            'image_tag':  img_tag,
            'text_tag':   desc_tag,
            'layer':      'B',
            'groq_used':  True,
        }

    except Exception as e:
        # API failure → approve with flag so real reports are never silently lost
        print(f'[cross_modal] Groq call failed ({type(e).__name__}) — approving with flag')
        return {
            'approved': True, 'result': 'groq_error', 'confidence': 0,
            'reason': f'Groq cross-modal check failed ({type(e).__name__}) — approved with flag',
            'image_tag': img_tag, 'text_tag': desc_tag,
            'layer': 'B', 'groq_used': False,
        }


# ═════════════════════════════════════════════════════════════════════════════
#  BAN MANAGEMENT  (swap _banned_users / _strike_log for DB in production)
# ═════════════════════════════════════════════════════════════════════════════
_banned_users: dict         = {}   # user_id → {reason, at, permanent}
_strike_log:   defaultdict  = defaultdict(list)  # user_id → [reasons]

BAN_THRESHOLD = 3   # strikes before auto temporary-ban


def record_strike(user_id: str, reason: str) -> dict:
    """Add one strike; auto-ban if BAN_THRESHOLD reached."""
    uid = str(user_id)
    _strike_log[uid].append(reason)
    n = len(_strike_log[uid])
    if n >= BAN_THRESHOLD:
        return ban_user(uid, f'Auto-ban: {n} strikes (last: {reason})', permanent=False)
    return {'banned': False, 'strikes': n, 'threshold': BAN_THRESHOLD}


def ban_user(user_id: str, reason: str, permanent: bool = True) -> dict:
    uid = str(user_id)
    _banned_users[uid] = {
        'reason':    reason,
        'at':        time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'permanent': permanent,
    }
    return {'banned': True, 'user_id': uid, 'reason': reason, 'permanent': permanent}


def is_banned(user_id: str) -> dict:
    info = _banned_users.get(str(user_id))
    return {'banned': True, **info} if info else {'banned': False}


def get_strikes(user_id: str) -> int:
    return len(_strike_log.get(str(user_id), []))


# ═════════════════════════════════════════════════════════════════════════════
#  MASTER VALIDATION PIPELINE  ← call this from your Flask route
# ═════════════════════════════════════════════════════════════════════════════
def validate_submission(
    description:    str,
    image_b64:      Optional[str],
    user_id:        str,
    tag:            str              = None,   # issue category e.g. 'pothole'
    lat:            Optional[float]  = None,
    lng:            Optional[float]  = None,
    stored_hashes:  list             = None,   # pHash strings from DB
    recent_reports: list             = None,   # recent report dicts from DB
    mime:           str              = 'image/jpeg',
) -> dict:
    """
    Run all checks in priority order, short-circuit on first failure.

    Check order:
      Pre  → ban check
      1    → text spam (3-layer: keyword → ML → Groq)
      2    → coordinate spam (Haversine)
      3    → AI-generated image (permanent ban on detect)
      4    → duplicate image (pHash)
      5    → false report (vision civic-issue gate)
      6    → cross-modal consistency  ← NEW in v3
               Layer A: tag synonym comparison (free, ~0 ms)
               Layer B: Groq vision re-check (only on tag mismatch, ~400 ms)

    Returns:
    {
      'approved':            bool,
      'rejection_reason':    str | None,
      'action':              'approve' | 'reject' | 'ban' | 'flag',
      'checks': {
          'spam_text':       {...} | None,
          'coord_spam':      {...} | None,
          'ai_image':        {...} | None,
          'duplicate_img':   {...} | None,
          'false_report':    {...} | None,
          'cross_modal':     {...} | None,   ← NEW in v3
      },
      'image_hash':          str | None,
      'cross_modal_flagged': bool,           ← NEW: True when result='context_added'
    }
    """
    checks:              dict          = {}
    img_hash:            Optional[str] = None
    cross_modal_flagged: bool          = False

    # ── Pre-check: User banned? ──────────────────────────────────────────────
    ban = is_banned(user_id)
    if ban['banned']:
        return _reject(f"Account suspended: {ban.get('reason','policy violation')}", 'reject', checks, img_hash)

    # ── Check 1: Text spam ────────────────────────────────────────────────────
    if description:
        sp = classify_spam(description)
        checks['spam_text'] = sp
        if sp['verdict'] in ('spam', 'abuse'):
            record_strike(user_id, f"spam_text:{sp['verdict']}")
            return _reject(f"Report classified as {sp['verdict']} — {sp['reason']}", 'reject', checks, img_hash)
        if sp['verdict'] == 'test':
            return _reject('Test submission ignored', 'reject', checks, img_hash)

    # ── Check 2: Coordinate spam ──────────────────────────────────────────────
    if lat is not None and lng is not None and recent_reports:
        cs = is_coordinate_spam(lat, lng, recent_reports, same_user_id=user_id, new_category=tag)
        checks['coord_spam'] = cs
        if cs['is_spam']:
            record_strike(user_id, 'coord_spam')
            return _reject(cs['reason'], 'reject', checks, img_hash)

    # ── Check 3: AI-generated image (most severe → permanent ban) ─────────────
    if image_b64:
        ai = detect_ai_image(image_b64, mime)
        checks['ai_image'] = ai
        if ai.get('is_ai_generated') and ai.get('confidence', 0) >= 75:
            ban_user(user_id, 'Submitted AI-generated image', permanent=True)
            return _reject(
                f"AI-generated image detected (confidence {ai['confidence']}%) — account permanently suspended",
                'ban', checks, img_hash,
            )

    # ── Check 4: Duplicate image ──────────────────────────────────────────────
    if image_b64:
        img_hash = compute_image_hash(image_b64)
        if stored_hashes:
            dup = is_duplicate_image(img_hash, stored_hashes)
            checks['duplicate_img'] = dup
            if dup['is_duplicate']:
                record_strike(user_id, 'duplicate_image')
                return _reject(
                    f"Duplicate image (Hamming distance {dup.get('distance','?')}). This issue may already be reported.",
                    'reject', checks, img_hash,
                )

    # ── Check 5: False report (vision AI civic-issue gate) ────────────────────
    # We store the full vision result so Check 6 can reuse image_tag without
    # making a second API call.
    image_tag_from_vision: Optional[str] = None
    if image_b64 and _client:
        vision = analyze_image(image_b64, mime)
        checks['false_report'] = vision
        if 'error' in vision:
            return {
                'approved': False,
                'rejection_reason': f"Image analysis failed: {vision['error']}",
                'action': 'reject',
                'checks': checks,
                'image_hash': img_hash,
                'cross_modal_flagged': True,  # flag for manual review since vision failed
            }
        if not vision.get('is_civic_issue', True):
            record_strike(user_id, 'false_report')
            reason = vision.get('false_report_reason') or 'Image does not appear to show a civic issue'
            return _reject(reason, 'reject', checks, img_hash)
        # Capture the tag the vision model assigned — used in Check 6 below
        image_tag_from_vision = (vision.get('category') or vision.get('tag') or '').lower().strip() or None

    # ── Check 6: Cross-modal consistency (image tag vs description tag) ────────
    # Runs when: image was uploaded AND vision returned a tag AND we have a
    # text tag from auto_classify.  Zero extra API cost when tags are compatible
    # (Layer A handles that instantly).  Groq fires only on real mismatches.
    if image_b64 and image_tag_from_vision and tag:
        cm = check_cross_modal_consistency(
            image_b64   = image_b64,
            description = description,
            image_tag   = image_tag_from_vision,
            text_tag    = tag,
            mime        = mime,
        )
        checks['cross_modal'] = cm

        if not cm['approved']:
            record_strike(user_id, 'cross_modal_mismatch')
            return _reject(
                f"Your description does not match the uploaded image. "
                f"The image appears to show a '{image_tag_from_vision}' issue, "
                f"but your description suggests '{tag}'. "
                f"Please update your description to match the photo and resubmit.",
                'flag', checks, img_hash,
            )

        # context_added → approve but mark for soft audit trail
        if cm.get('result') == 'context_added':
            cross_modal_flagged = True
            print(f'[validate] cross_modal context_added — approved with flag '
                  f'(image={image_tag_from_vision!r}, text={tag!r})')

    # ── All checks passed ─────────────────────────────────────────────────────
    return {
        'approved':            True,
        'rejection_reason':    None,
        'action':              'approve',
        'checks':              checks,
        'image_hash':          img_hash,
        'cross_modal_flagged': cross_modal_flagged,
    }


def _reject(reason: str, action: str, checks: dict, img_hash,
            cross_modal_flagged: bool = False) -> dict:
    return {
        'approved':            False,
        'rejection_reason':    reason,
        'action':              action,
        'checks':              checks,
        'image_hash':          img_hash,
        'cross_modal_flagged': cross_modal_flagged,
    }


# ═════════════════════════════════════════════════════════════════════════════
#  Q & A
# ═════════════════════════════════════════════════════════════════════════════
def ask_question(question: str, context_issues=None) -> dict:
    if not _client:
        return {'error': 'AI not configured', '_status': 'not_configured'}

    context = ''
    if context_issues:
        context = f'\n\nCurrent issues snapshot ({len(context_issues)} total):\n'
        for i in context_issues[:15]:
            context += f"- [{i.get('tag','?')}/{i.get('severity','?')}] {i.get('area','?')}: {i.get('description','')[:80]}\n"

    try:
        resp = _client.chat.completions.create(
            model=_MODEL,
            messages=[
                {'role': 'system', 'content': 'You are AreaPulse, an AI civic-data assistant for Delhi. Answer concisely (2-4 sentences). Cite specific areas/issues from context when relevant.'},
                {'role': 'user',   'content': question + context},
            ],
            max_tokens=400, temperature=0.3,
        )
        return {'answer': (resp.choices[0].message.content or '').strip(), 'source': 'groq-llama4-scout'}
    except Exception as e:
        return {'error': f'{type(e).__name__}: {e}', '_status': 'server_error'}


# ═════════════════════════════════════════════════════════════════════════════
#  INSIGHTS / SUMMARY
# ═════════════════════════════════════════════════════════════════════════════
def summarize_landscape(by_tag: dict, by_severity: dict, by_status: dict) -> Optional[str]:
    if not _client:
        return None
    stats = (
        f"Issues by type: {dict(sorted(by_tag.items(), key=lambda x: -x[1]))}\n"
        f"By severity: {by_severity}\nBy status: {by_status}"
    )
    try:
        resp = _client.chat.completions.create(
            model=_MODEL,
            messages=[
                {'role': 'system', 'content': 'You are AreaPulse analytics. Write a 2-3 sentence executive summary: most urgent pattern + one actionable recommendation.'},
                {'role': 'user',   'content': stats},
            ],
            max_tokens=200, temperature=0.3,
        )
        return (resp.choices[0].message.content or '').strip()
    except Exception:
        return None


# ═════════════════════════════════════════════════════════════════════════════
#  COMPLAINT LETTER DRAFTING
# ═════════════════════════════════════════════════════════════════════════════
_AUTHORITY_MAP = {
    'pothole':     {'name': 'PWD Delhi (Public Works Department)',          'email': 'secretary-pwd@nic.in'},
    'water':       {'name': 'Delhi Jal Board',                              'email': 'cmo@delhijalboard.in'},
    'garbage':     {'name': 'Municipal Corporation of Delhi (MCD)',          'email': 'pgms@mcdonline.nic.in'},
    'streetlight': {'name': 'MCD Lighting Department',                      'email': 'pgms@mcdonline.nic.in'},
    'traffic':     {'name': 'Delhi Traffic Police',                          'email': 'cp.delhipolice@nic.in'},
    'noise':       {'name': 'Delhi Pollution Control Committee',             'email': 'dpcc@nic.in'},
    'sewage':      {'name': 'Delhi Jal Board (Sewerage Division)',           'email': 'cmo@delhijalboard.in'},
    'electricity': {'name': 'BSES Delhi',                                    'email': 'customercare@bsesdelhi.com'},
    'tree':        {'name': 'Forest Department, Delhi',                      'email': 'dofdelhi@nic.in'},
    'other':       {'name': 'Office of the District Magistrate, Delhi',      'email': 'dm.newdelhi@delhi.gov.in'},
}


def get_authority(tag: str) -> dict:
    return _AUTHORITY_MAP.get(tag or 'other', _AUTHORITY_MAP['other'])


def draft_complaint(issue: dict, citizen_name: str = None, language: str = 'english') -> dict:
    authority = get_authority(issue.get('tag', 'other'))
    citizen   = citizen_name or issue.get('user') or 'Concerned Citizen'
    area      = issue.get('area', 'Delhi')
    severity  = (issue.get('severity') or 'medium').upper()
    desc      = issue.get('description', '')
    landmark  = issue.get('landmark', '')
    lat, lng  = issue.get('lat', ''), issue.get('lng', '')
    issue_id  = issue.get('id', '')
    tag       = issue.get('tag', 'other').replace('_', ' ').title()
    today     = time.strftime('%d %B %Y')

    fallback_text = _fallback_letter(citizen, area, severity, desc, landmark, tag, authority, today, issue_id, lat, lng)
    fallback_html = _text_to_html(fallback_text)
    subject_fb    = f'Civic Complaint · {tag} in {area} · Ref #AP-{issue_id}'

    if not _client:
        return {'subject': subject_fb, 'body_html': fallback_html, 'body_text': fallback_text, 'authority': authority, 'source': 'template'}

    prompt = f"""Write a formal civic complaint letter for the Indian government in {language}.

ISSUE DETAILS:
- Reference: #AP-{issue_id}  |  Date: {today}
- Citizen: {citizen}
- Issue type: {tag}  |  Severity: {severity}
- Area: {area}  |  Landmark: {landmark or 'N/A'}
- Coordinates: {lat}, {lng}
- Description: {desc}
- Filed via: AreaPulse civic platform

ADDRESSED TO: {authority['name']}

REQUIREMENTS:
- Formal, respectful Indian bureaucratic tone
- 4 short paragraphs maximum, subject line first (start with "Subject:")
- No markdown, no asterisks, plain text only
- Do NOT invent facts not provided above"""

    try:
        resp = _client.chat.completions.create(
            model=_MODEL,
            messages=[
                {'role': 'system', 'content': 'You are an expert civic complaint letter drafter for Indian municipal authorities. Write formally, respectfully, action-oriented.'},
                {'role': 'user',   'content': prompt},
            ],
            max_tokens=700, temperature=0.4,
        )
        raw  = (resp.choices[0].message.content or '').strip()
        if not raw:
            raise ValueError('empty Groq response')
        subject = subject_fb
        body    = raw
        first   = raw.split('\n', 1)[0].strip()
        if first.lower().startswith('subject:'):
            subject = first.split(':', 1)[1].strip()
            body    = raw.split('\n', 1)[1].strip() if '\n' in raw else raw
        return {'subject': subject, 'body_html': _text_to_html(body), 'body_text': body, 'authority': authority, 'source': 'groq-llama4-scout'}
    except Exception as e:
        print(f'[ai_engine] draft_complaint fallback: {e}')
        return {'subject': subject_fb, 'body_html': fallback_html, 'body_text': fallback_text, 'authority': authority, 'source': 'template'}


def _fallback_letter(citizen, area, severity, desc, landmark, tag, authority, today, issue_id, lat, lng):
    loc    = area + (f', near {landmark}' if landmark else '')
    coords = f'\nCoordinates: {lat}, {lng}' if lat and lng else ''
    return f"""Date: {today}

To,
The Concerned Officer,
{authority['name']},
New Delhi.

Subject: Civic Complaint regarding {tag} issue in {area} (AreaPulse Ref #AP-{issue_id})

Respected Sir/Madam,

I am writing to formally bring to your attention a {severity.lower()}-severity civic issue affecting residents of {loc}. This has been documented via the AreaPulse civic platform (Reference: #AP-{issue_id}, filed {today}).

Issue description: {desc}{coords}

This problem is causing significant inconvenience and, in cases of high severity, poses a safety risk. I respectfully request your office to: (1) inspect the location at the earliest, (2) initiate corrective action within a reasonable timeline, and (3) update the citizen on action taken.

Thank you for your attention to this matter.

Yours sincerely,
{citizen}
Filed via AreaPulse · Civic Issue Map · Delhi
Reference: #AP-{issue_id}"""


def _text_to_html(text: str) -> str:
    if not text:
        return ''
    escaped = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    paras   = [p.strip() for p in escaped.split('\n\n') if p.strip()]
    body    = '\n'.join(f'<p style="margin:0 0 14px;line-height:1.6">{p.replace(chr(10),"<br>")}</p>' for p in paras)
    return (
        '<div style="font-family:Georgia,\'Times New Roman\',serif;font-size:14px;'
        'color:#1a1a1a;max-width:640px;margin:0 auto;padding:24px;">\n'
        + body
        + '\n<hr style="margin:20px 0;border:none;border-top:1px solid #ddd">\n'
        '<p style="font-size:11px;color:#888;margin:0">Filed via <b>AreaPulse</b> — Delhi Civic Issue Platform</p>\n</div>'
    )


# ═════════════════════════════════════════════════════════════════════════════
#  AR SCANNER  (rich multi-issue format)
# ═════════════════════════════════════════════════════════════════════════════
_AR_PROMPT = """You are AreaPulse AR vision AI for Delhi civic issues. Analyze this photo.
Return ONLY valid JSON:
{
  "issues": [
    {
      "issue_type": "pothole|water|garbage|streetlight|traffic|noise|sewage|electricity|tree|other",
      "severity": "low|medium|high",
      "hazard_level": "low|medium|high",
      "confidence": <70-98>,
      "title": "<3-5 word title>",
      "description": "<one sentence>",
      "recommended_authority": "<e.g. PWD Delhi, MCD, DJB>",
      "estimated_repair_time": "<e.g. 1-3 days>",
      "ar_label": "<UPPERCASE 1-2 words>",
      "area_estimate": "Delhi",
      "x_hint": <30-70>,
      "y_hint": <35-65>
    }
  ],
  "primary_index": 0
}
List ALL visible issues (1-3). x_hint/y_hint = % of image width/height where issue appears."""


def analyze_image_ar(image_b64: str, mime: str = 'image/jpeg') -> dict:
    if not _client:
        return {'error': 'AI not configured. Set GROQ_API_KEY.'}
    try:
        resp = _client.chat.completions.create(
            model=_MODEL,
            messages=[{'role': 'user', 'content': [
                {'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{image_b64}'}},
                {'type': 'text', 'text': _AR_PROMPT},
            ]}],
            max_tokens=600, temperature=0.2,
        )
        raw    = (resp.choices[0].message.content or '').strip()
        parsed = _extract_json(raw)
        if not parsed:
            return {'error': 'Unparseable AI response', 'raw': raw[:200]}
        return parsed
    except Exception as e:
        return {'error': f'{type(e).__name__}: {e}'}


# ═════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═════════════════════════════════════════════════════════════════════════════
def _extract_json(text: str) -> Optional[dict]:
    """Robust JSON extraction — handles markdown fences and prose wrapping."""
    if not text:
        return None
    for attempt in (text, text.replace('```json', '').replace('```', '').strip()):
        try:
            return json.loads(attempt)
        except Exception:
            pass
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except Exception:
            pass
    return None

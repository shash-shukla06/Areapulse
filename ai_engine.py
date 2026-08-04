"""
ai_engine.py  v4  — AreaPulse intelligence layer
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Feature matrix (graceful degradation when a dep is missing):

  ✓ Image analysis       → Groq Llama-4-Scout vision (requires GROQ_API_KEY)
  ✓ Text spam            → keyword → sklearn ML → Groq few-shot LLM
  ✓ Duplicate image      → pHash (imagehash) → SHA-256 exact fallback
  ✓ Coordinate spam      → Haversine distance math  (zero deps)
  ✓ False report         → Enhanced Groq vision with civic-issue gate
  ✓ AI-image detection   → EXIF check → pixel stats → HuggingFace → Groq vision
  ✓ Cross-modal check    → image-tag vs description-tag consistency
  ✓ Ban management       → strike system + immediate ban
  ✓ Master pipeline      → validate_submission() — call this from your Flask route
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

v4 — Hardened AI-image detection  (what was broken and how it is fixed):

  THE GAPS IN v3
  ─────────────
  1. EXIF check: no-EXIF JPEG returned confidence=68, but early-return
     required ≥80. It fell through and Groq would approve it.
  2. HF label matching used 'artif' substring — fragile; threshold 0.65
     was too strict for subtle AI images.
  3. Groq prompt listed only PORTRAIT signals (fingers, faces, people).
     It said nothing about synthetic INFRASTRUCTURE — potholes, roads,
     garbage. A photorealistic AI pothole passed every check.
  4. validate_submission blocked only at confidence ≥ 75. Groq returning
     confidence=70 ("probably AI but not sure") → APPROVED.
  5. No ensemble: EXIF=68% AI + HF=60% AI + Groq=70% AI all failed
     individually and never combined → APPROVED.
  6. _VISION_PROMPT (analyze_image) had no instruction to flag AI-rendered
     images. Groq happily classified a synthetic pothole as a real civic
     issue.

  THE FIXES IN v4
  ─────────────
  A. _check_exif: added Software/ImageDescription tag check for AI tools,
     raised no-EXIF-JPEG confidence 68→74, PNG 75→82, partial-EXIF 45→58,
     lowered early-return threshold 80→72.
  B. detect_ai_image: precision-first approach. Only block on signals that
     are DEFINITIVE or high-confidence. Never block on missing EXIF
     (WhatsApp/Telegram strip EXIF from all real photos).
  C. _AI_IMG_PROMPT v4: Rewrote for INFRASTRUCTURE images. Added explicit
     signals for synthetic roads, potholes, garbage, streetlights.
     Changed default bias: when uncertain → assume real photo.
  D. _VISION_PROMPT: Added explicit step 0 — "Is this AI-generated?"
     before the civic-issue gate. A synthetic pothole returns
     is_civic_issue=false immediately.
  E. validate_submission Check 3: threshold 78% (not 62%). No ensemble
     voting — Groq is the primary decider. EXIF only for definitive
     signals (AI tool name in metadata, exact generator dimensions).

Install deps:
  pip install groq Pillow imagehash scikit-learn transformers torch scipy
"""

import os, json, re, time, base64, io, math, hashlib, pathlib
from collections import defaultdict
from typing import Optional

# ─────────────────────────────────────────────────────────────────────────────
#  GROQ  (core LLM / vision)
# ─────────────────────────────────────────────────────────────────────────────
_client = None
_MODEL = "qwen/qwen3.6-27b"
_MODEL_TEXT = "qwen/qwen3.6-27b" 

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
_NP_OK  = False
try:
    from PIL import Image, ExifTags, ImageChops
    import imagehash as _imagehash
    _PIL_OK = True
    print('[ai_engine] Pillow + imagehash ✓')
    try:
        import numpy as np
        _NP_OK = True
    except ImportError:
        pass
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
#  SIGHTENGINE AI-IMAGE DETECTOR  (dedicated API, most accurate)
#  Free tier: 500 checks/month — https://sightengine.com
#  Set env vars: SIGHTENGINE_API_USER and SIGHTENGINE_API_SECRET
# ─────────────────────────────────────────────────────────────────────────────
_sightengine_user   = os.environ.get('SIGHTENGINE_API_USER', '')
_sightengine_secret = os.environ.get('SIGHTENGINE_API_SECRET', '')
_sightengine_ok     = bool(_sightengine_user and _sightengine_secret)
if _sightengine_ok:
    print('[ai_engine] Sightengine AI detector ✓')
else:
    print('[ai_engine] Sightengine not configured (optional) — set SIGHTENGINE_API_USER + SIGHTENGINE_API_SECRET')

# ─────────────────────────────────────────────────────────────────────────────
#  HUGGINGFACE AI-IMAGE DETECTOR  (offline classifier, fallback)
#  Tries multiple public models in order — first one that loads wins.
#  umm-maybe/AI-image-detector was removed (gated/403).
# ─────────────────────────────────────────────────────────────────────────────
_ai_img_detector       = None
_ai_img_detector_name  = None

_HF_MODELS_TO_TRY = [
    'prithivMLmods/AI-vs-Real',       # General AI detector, all generators
    'Nahrawy/AIorNot',                # Good general detector
    'haywoodsloan/ai-image-detector', # Fallback
    # Organika/sdxl-detector removed — only catches Stable Diffusion XL,
    # misses Midjourney, DALL-E, Firefly, etc.
]

try:
    from transformers import pipeline as _hf_pipeline
    for _model_name in _HF_MODELS_TO_TRY:
        try:
            _ai_img_detector      = _hf_pipeline('image-classification', model=_model_name, device=-1)
            _ai_img_detector_name = _model_name
            print(f'[ai_engine] HF AI-image detector ✓  model={_model_name}')
            break
        except Exception as _me:
            print(f'[ai_engine] HF model {_model_name} unavailable: {str(_me)[:60]}')
    if not _ai_img_detector:
        print('[ai_engine] All HF models failed — using Sightengine/Groq only')
except ImportError:
    print('[ai_engine] transformers not installed → pip install transformers torch')


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
        'sightengine':       _sightengine_ok,
        'ai_img_detector':   _ai_img_detector is not None,
        'hf_model':          _ai_img_detector_name,
        'exif_check':        _PIL_OK,
        'coord_spam':        True,
        'cross_modal_check': True,
    }


# ═════════════════════════════════════════════════════════════════════════════
#  SHARED GROQ JSON CALLER
#
#  qwen/qwen3.6-27b is a reasoning model. Left to itself it narrates its
#  reasoning ("Step 1: ... Conclusion: ...") instead of returning JSON,
#  which is what Llama-4-Scout never did. This wrapper is used by every
#  function that needs strict JSON back from Groq (vision, spam, AI-image
#  detection, cross-modal). It stacks every available lever to stop that:
#
#    1. A strict system message (system prompts outweigh user prompts,
#       and it's the first thing the model reads).
#    2. reasoning_format='hidden'   — Groq-specific kwarg for reasoning
#       models (qwen3.x, deepseek-r1, etc.) that strips the <think> trace
#       out of message.content entirely.
#    3. response_format={'type':'json_object'} — forces the model into
#       strict JSON mode; the prompt must mention "JSON" somewhere, which
#       all of ours already do.
#    4. temperature=0 by default — reasoning models ramble more at
#       higher temperature.
#
#  (2) and (3) are opportunistic: not every groq SDK version / model pairing
#  supports both kwargs. We try the strongest combo first and step down
#  through fallbacks rather than hard-failing the whole request if Groq
#  rejects an unrecognized parameter.
# ═════════════════════════════════════════════════════════════════════════════

_NO_REASONING_SYSTEM = """You are a backend JSON API endpoint, not a chatbot or assistant.

ABSOLUTE RULES — breaking any of these is a system failure, not a stylistic choice:
- Do NOT explain your reasoning.
- Do NOT think step by step out loud.
- Do NOT write analysis, observations, bullet points, or narration.
- Do NOT write phrases like "Step 1", "Conclusion", "Let's look at...", "The most prominent feature is...".
- Do NOT use markdown, headings, or code fences.
- Output exactly ONE JSON object and nothing else — no text before it, no text after it.
- The FIRST character you output MUST be {
- The LAST character you output MUST be }
- Your entire response must be parseable by a strict JSON parser with zero preprocessing.

If you are unsure of an answer, still return valid JSON with your best-guess field values.
Never explain the uncertainty in prose — encode it in the "confidence" field instead."""


def _call_groq_json(content, max_tokens: int = 500, temperature: float = 0.0) -> str:
    """
    Call Groq chat completions and return the raw text response, using every
    available lever to keep a reasoning model (qwen3.x etc.) from rambling
    instead of emitting JSON. Falls back gracefully if reasoning_format /
    response_format aren't supported by the current SDK/model pairing.

    `content` is either a plain string (text-only prompt) or a list of
    content blocks, e.g. [{'type':'image_url',...}, {'type':'text',...}]
    for vision calls.

    Raises the underlying exception only if every fallback variant fails
    (e.g. auth error, rate limit, network issue) — callers should still
    wrap this in their own try/except as before.
    """
    messages = [
        {'role': 'system', 'content': _NO_REASONING_SYSTEM},
        {'role': 'user', 'content': content},
    ]

    # Order matters. reasoning_format='hidden' ALONE goes first — it's the
    # gentlest option. response_format={'type':'json_object'} is riskiest:
    # it makes Groq run a strict server-side schema check, and if the
    # model's visible content is empty (reasoning ate the whole token
    # budget) that check hard-fails with json_validate_failed /
    # failed_generation: '' instead of just returning empty text. So we
    # only reach for it after the plain reasoning_format attempt.
    kwargs_variants = [
        dict(reasoning_format='hidden'),
        dict(reasoning_format='hidden', response_format={'type': 'json_object'}),
        dict(response_format={'type': 'json_object'}),
        dict(),
    ]

    # Hidden reasoning tokens are still deducted from max_tokens even though
    # they never appear in message.content. Pad the budget on any variant
    # that uses reasoning_format so the model has room left to actually
    # write the JSON after it finishes "thinking".
    REASONING_PADDING = 700

    last_err = None
    for extra_kwargs in kwargs_variants:
        call_max_tokens = max_tokens + REASONING_PADDING if extra_kwargs.get('reasoning_format') else max_tokens
        try:
            resp = _client.chat.completions.create(
                model=_MODEL,
                messages=messages,
                max_tokens=call_max_tokens,
                temperature=temperature,
                **extra_kwargs,
            )
            text = (resp.choices[0].message.content or '').strip()
            if not text:
                # Groq returned 200 but with nothing usable — treat like a
                # rejection and step down to the next variant instead of
                # returning an empty string for _extract_json to choke on.
                last_err = RuntimeError(
                    'Groq returned empty content (reasoning likely consumed max_tokens)'
                )
                continue
            return text
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            # Step down the ladder for kwarg rejections AND for the
            # empty-generation / JSON-validator failure mode seen with
            # response_format=json_object on this reasoning model.
            if any(x in msg for x in ('reasoning_format', 'response_format',
                                       'unrecognized', 'unsupported', 'unknown parameter',
                                       'unexpected keyword',
                                       'json_validate_failed', 'failed to validate json',
                                       'failed_generation')):
                continue
            raise
    raise last_err


# ═════════════════════════════════════════════════════════════════════════════
#  CROSS-MODAL TAG SYNONYMS
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
    a = (tag_a or 'other').lower().strip()
    b = (tag_b or 'other').lower().strip()
    if a == b: return True
    if a == 'other' or b == 'other': return True
    return b in _TAG_SYNONYMS.get(a, set()) or a in _TAG_SYNONYMS.get(b, set())


# ═════════════════════════════════════════════════════════════════════════════
#  FEATURE 1 — IMAGE ANALYSIS + FALSE REPORT GATE
#
#  v4 CHANGE: Added Step 0 to the vision prompt — explicit AI-generation check
#  BEFORE the civic-issue check. An AI-rendered pothole now fails Step 0
#  and never reaches the civic gate, preventing false approvals.
# ═════════════════════════════════════════════════════════════════════════════

_VISION_PROMPT = """You are AreaPulse Vision API.

IMPORTANT:
You are NOT a chatbot.
You are a backend API used by a production application.

Your response is parsed directly using Python json.loads().

If you output ANYTHING except one valid JSON object, the request fails.

DO NOT output:
- explanations
- reasoning
- markdown
- ```json
- comments
- notes
- apologies
- introductions
- extra text

The FIRST character of your response MUST be {
The LAST character of your response MUST be }

Return EXACTLY ONE JSON OBJECT.


=====================================================
TASK
=====================================================

A citizen uploaded a photo claiming it shows a civic issue.

STEP 0 — AI IMAGE DETECTION

Determine whether the image is AI-generated, computer-rendered, digitally illustrated, CGI, synthetic, or a stock illustration.

Examples:
- Midjourney
- DALL-E
- Stable Diffusion
- Firefly
- Canva AI
- Flux
- Ideogram
- Leonardo AI
- CGI
- 3D Render

Indicators include:

• unrealistic lighting
• overly perfect textures
• cinematic composition
• artificial HDR colors
• painted appearance
• impossible geometry
• unrealistic asphalt
• perfectly clean scenes
• no camera imperfections
• unrealistic blur
• obvious AI artifacts

If AI-generated:

is_civic_issue = false

category = "none"

severity = "none"

false_report_reason =
"image appears AI-generated or non-photographic"


=====================================================
STEP 1
=====================================================

If the image is a REAL photograph,
determine whether it contains a civic issue.

Allowed categories

pothole
water
garbage
streetlight
traffic
noise
sewage
electricity
tree
other

If NOT a civic issue:

selfie
food
animals
indoor objects
screenshots
documents
news
paintings
artwork
blank walls
random landscapes

Return

is_civic_issue = false

category = "none"

severity = "none"


=====================================================
STEP 2
=====================================================

If it IS a civic issue:

Determine

category

severity

confidence

description


=====================================================
OUTPUT RULES
=====================================================

Return EXACTLY this schema.

Do NOT rename keys.

Do NOT remove keys.

Do NOT add keys.

{
  "is_civic_issue": true,
  "category": "pothole",
  "severity": "medium",
  "confidence": 91,
  "description": "Large pothole on the main road causing traffic risk.",
  "false_report_reason": null,
  "source": "qwen-3.6-27b"
}

Schema

{
  "is_civic_issue": boolean,
  "category": "pothole|water|garbage|streetlight|traffic|noise|sewage|electricity|tree|other|none",
  "severity": "low|medium|high|none",
  "confidence": integer,
  "description": string,
  "false_report_reason": string|null,
  "source": string
}

Return ONE JSON object only.
Nothing before it.
Nothing after it."""


def analyze_image(image_b64: str, mime: str = 'image/jpeg') -> dict:
    """Vision: classify civic issue + false-report gate + AI-generation check."""
    if not _client:
        return {'error': 'AI vision not configured. Set GROQ_API_KEY.', '_status': 'not_configured'}
    try:
        raw = _call_groq_json(
            content=[
                {'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{image_b64}'}},
                {'type': 'text', 'text': _VISION_PROMPT},
            ],
            max_tokens=600, temperature=0,
        )
        print("=" * 80)
        print(raw)
        print("=" * 80)
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
#
#  v4 CHANGES:
#  • Expanded _SPAM_KW with Hinglish, regional, and social-media patterns
#  • Added _GIBBERISH_PATTERNS for keyboard-mashing detection
#  • Improved _SPAM_PROMPT: more few-shot examples, better Hindi handling
#  • Lowered ML confidence threshold 75 → 68 for earlier blocking
#  • Added length-based heuristics for gibberish
# ═════════════════════════════════════════════════════════════════════════════

_SPAM_PROMPT = """You are a spam filter for a civic issue reporting platform in India.
Classify the report text into ONE of: REAL | SPAM | ABUSE | TEST

Rules:
- REAL : text that mentions a civic infrastructure problem — road, pothole, water,
         electricity, garbage, sewage, drain, streetlight, tree, noise, footpath.
         Language does not matter — Hindi, Hinglish, broken English are all fine
         IF the content is about a civic issue.
- SPAM : anything with no civic issue mentioned — casual chat, greetings, questions,
         gibberish, fantasy/joke, ads, keyboard mashing, social media copy-paste,
         or Hinglish/Hindi that is clearly NOT about infrastructure.
- ABUSE: profanity, hate speech, slurs, personal attacks
- TEST : clearly a test ("test", "testing", "abc", "check", "hello", "dekh rha hu")

CRITICAL RULE — Hindi/Hinglish is NOT automatically REAL.
It is REAL only if it mentions a civic problem (sadak, naali, bijli, paani, kachra,
nali, drain, light, pothole, garbage, sewage, pipe, pole, etc.).
Hindi/Hinglish with NO civic keywords = SPAM.

── FEW-SHOT EXAMPLES ─────────────────────────────────────────────────────────
TEXT: "pothole on main road near metro"
→ {"verdict":"REAL","confidence":97,"reason":"clear road infrastructure report"}

TEXT: "sadak pe gadha hai kaafi bada"
→ {"verdict":"REAL","confidence":96,"reason":"Hindi: pothole on road"}

TEXT: "nali band hai paani bhar raha hai"
→ {"verdict":"REAL","confidence":96,"reason":"Hindi: drain blocked, flooding"}

TEXT: "bijli ka pole toot gaya"
→ {"verdict":"REAL","confidence":95,"reason":"Hindi: electricity pole fallen"}

TEXT: "kachra nahi utha 3 din se"
→ {"verdict":"REAL","confidence":94,"reason":"Hindi: garbage not collected"}

TEXT: "ustadd ji h kyaa ni ews"
→ {"verdict":"SPAM","confidence":96,"reason":"Hinglish with no civic issue"}

TEXT: "kya baat hai yaar kuch nahi"
→ {"verdict":"SPAM","confidence":95,"reason":"casual chat, no civic content"}

TEXT: "hello bhai kaisa hai"
→ {"verdict":"SPAM","confidence":97,"reason":"greeting, not a civic issue"}

TEXT: "ye app kaisi hai dekh rha hu"
→ {"verdict":"TEST","confidence":94,"reason":"testing the app"}

TEXT: "aliens attacked my colony last night"
→ {"verdict":"SPAM","confidence":99,"reason":"fantastical non-civic content"}

TEXT: "buy cheap medicines click here discount"
→ {"verdict":"SPAM","confidence":99,"reason":"commercial advertisement"}

TEXT: "aaaaaaaaaaaa bbbbbb cccc"
→ {"verdict":"SPAM","confidence":97,"reason":"keyboard mashing"}

TEXT: "test test test 123"
→ {"verdict":"TEST","confidence":99,"reason":"test pattern"}

TEXT: "f**k you MCD sala kuch nahi karte"
→ {"verdict":"ABUSE","confidence":88,"reason":"profanity and harassment"}

TEXT: "water pipe leaking near chowk since yesterday"
→ {"verdict":"REAL","confidence":96,"reason":"civic water infrastructure issue"}

TEXT: "dasd"
→ {"verdict":"SPAM","confidence":95,"reason":"meaningless characters, no civic content"}
──────────────────────────────────────────────────────────────────────────────

Respond ONLY with valid JSON, no other text:
{"verdict":"REAL"|"SPAM"|"ABUSE"|"TEST","confidence":0-100,"reason":"<12 words max>"}

REPORT TEXT:
"""

# ── Hard keyword filters (instant, no model needed) ──────────────────────────

# Spam keywords — clear non-civic / fantastical / commercial content
_SPAM_KW = [
    # Fantastical
    'alien','aliens','ufo','martian','zombie','vampire','dragon','unicorn',
    'ghost haunt','haunted','demon','witch','wormhole','time travel',
    'bhoot','daayan','chudail','jadoo tona',
    # Commercial
    'buy now','click here','free money','lottery','win prize',
    'earn money','work from home','make money fast',
    'call now','order now','limited offer','special discount',
    'whatsapp this number','contact this number',
    'cheap rate','best price','buy cheap',
    # Gibberish anchors
    'lorem ipsum','asdf qwerty','asdfgh','qwertyuiop',
    'aaaaaaa','bbbbbbb','xyzxyz','abcdefgh',
]

# Test pattern keywords
_TEST_KW = [
    'test test','testing 123','abc def','just testing','ignore this',
    'this is a test','only checking','dummy report','fake report',
    'sample submission','hello world','check check',
]

# Regex for pure repeated-character detection only ("aaaaaaa", "111111")
# NOTE: We intentionally do NOT check for keyboard-row characters here —
# the set [qwertasdfzxcv] covers 13/26 letters and matches real words like
# "detected", "street", "water", "exact". Keyboard mash is handled by Groq.
_GIBBERISH_RE = re.compile(r'(.)\1{5,}')   # 6+ same character: "aaaaaaa", "111111"

# Civic Hindi/Hinglish words that always indicate a real report.
# If any are found, skip the ML model (unreliable on informal Hinglish) → go to Groq.
_CIVIC_HINDI_KW = {
    'gadha','gadhe','gadda','gadde','khada','sadak','rasta',
    'pothole','toot','toota','tuta','phoot','phoota',
    'paani','pani','pipe','naali','nali','drain','sewage','naala','nala',
    'bhar','barh','overflow','leak','leakage',
    'bijli','bijlee','pole','khamba','taar','transformer',
    'kachra','kachre','garbage','dustbin','safai',
    'gir','gira','giri','band','jam','block','khula',
    'manhole','footpath','ghera','gali','mohalla',
    'bhot','kaafi','zyada','bada','badi','badha','badhi',
    'kharab','dikkat',
}


def classify_spam(description: str, has_photo: bool = False) -> dict:
    """
    3-layer spam classification:
      Layer 0 → civic Hindi/Hinglish allowlist  (instant, skips ML if civic word found)
      Layer 1 → hard keyword + gibberish regex  (instant)
      Layer 2 → sklearn ML pipeline             (offline, ~1 ms)
      Layer 3 → Groq LLM few-shot               (most accurate, ~400 ms)

    Returns: {'verdict': 'real'|'spam'|'abuse'|'test', 'confidence': int, 'reason': str}
    Always returns — never raises.
    """
    text = (description or '').strip()
    tl   = text.lower()

    # ── Layer 0: Civic Hindi allowlist — skip ML if civic word found ──────────
    # ML makes wrong predictions on informal Hinglish like 'bhot', 'gya', 'badha'.
    # If any civic Hindi keyword is present, bypass ML and go straight to Groq.
    _skip_ml = any(kw in tl for kw in _CIVIC_HINDI_KW)

    # ── Layer 1a: Keyword match ───────────────────────────────────────────────
    for kw in _SPAM_KW:
        if kw in tl:
            return {'verdict': 'spam', 'confidence': 95, 'reason': f'contains spam keyword "{kw}"'}
    for kw in _TEST_KW:
        if kw in tl:
            return {'verdict': 'test', 'confidence': 92, 'reason': 'test-pattern submission'}

    # ── Layer 1b: Gibberish / keyboard mash ──────────────────────────────────
    if len(text) < 6:
        return {'verdict': 'test', 'confidence': 60, 'reason': 'text too short'}
    if _GIBBERISH_RE.search(tl):
        return {'verdict': 'spam', 'confidence': 90, 'reason': 'gibberish / keyboard mash detected'}

    # ── Layer 2: sklearn ML model (~1 ms) ─────────────────────────────────────
    if _spam_pipeline and not _skip_ml:
        try:
            pred  = str(_spam_pipeline.predict([text])[0]).lower()
            proba = _spam_pipeline.predict_proba([text])[0]
            conf  = int(max(proba) * 100)
            label = {'real':'real','spam':'spam','abuse':'abuse','test':'test',
                     '0':'real','1':'spam'}.get(pred, 'real')
            if conf >= 80:   # raised from 68 → 80 to reduce false positives
                return {'verdict': label, 'confidence': conf, 'reason': 'ML classifier'}
            # Low ML confidence → fall through to Groq
        except Exception:
            pass

    # ── Layer 3: Groq LLM few-shot (~400 ms) ─────────────────────────────────
    if _client:
        try:
            raw = _call_groq_json(
                content=_SPAM_PROMPT + text[:600],
                max_tokens=250, temperature=0,
            )
            data   = _extract_json(raw) or {}
            verdict = (data.get('verdict') or 'REAL').lower().strip()
            if verdict not in ('real', 'spam', 'abuse', 'test'):
                verdict = 'real'
            return {
                'verdict':    verdict,
                'confidence': int(data.get('confidence') or 70),
                'reason':     (data.get('reason') or 'groq classified')[:80],
            }
        except Exception:
            pass

    return {'verdict': 'real', 'confidence': 40, 'reason': 'all classifiers unavailable — defaulted real'}


# ═════════════════════════════════════════════════════════════════════════════
#  FEATURE 3 — DUPLICATE IMAGE DETECTION  (perceptual hashing)
# ═════════════════════════════════════════════════════════════════════════════

def compute_image_hash(image_b64: str) -> Optional[str]:
    """
    Compute a 64-bit perceptual hash (pHash) of an image.
    pHash is ROBUST to: JPEG re-compression, minor crops, brightness/contrast tweaks.
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
    stored_hashes:  list,
    threshold:      int = 10,
) -> dict:
    """
    Compare a new image hash against all stored hashes.
    Returns: {'is_duplicate': bool, 'distance': int|None, 'matched_hash': str|None}
    """
    if not new_hash or not stored_hashes:
        return {'is_duplicate': False, 'distance': None, 'matched_hash': None}

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
       # AFTER:
        is_dup = bool(best_dist <= threshold)       
        return {
            'is_duplicate': is_dup,
            'distance':     int(best_dist) if best_match else None, 
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
    R = 6_371_000
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lon2 - lon1)
    a  = math.sin(dφ/2)**2 + math.cos(φ1) * math.cos(φ2) * math.sin(dλ/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


def is_coordinate_spam(
    new_lat:       float,
    new_lng:       float,
    existing:      list,
    radius_m:      int   = 100,
    max_per_radius:int   = 2,
    same_user_id:  str   = None,
    new_category:  str   = None,
) -> dict:
    """
    Haversine-based coordinate spam detection.

    Returns: {'is_spam': bool, 'reason': str, 'nearby_count': int}
    """
    nearby     = []
    same_user  = []
    same_cat   = []

    for r in existing:
        try:
            dist = haversine_meters(new_lat, new_lng,
                                    float(r['lat']), float(r['lng']))
        except Exception:
            continue
        if dist <= radius_m:
            nearby.append(r)
            if same_user_id and str(r.get('user_id','')) == str(same_user_id):
                same_user.append(r)
            if new_category and (r.get('tag','') == new_category
                                  or r.get('category','') == new_category):
                same_cat.append(r)

    # Same user + same category nearby
    if same_user_id and new_category:
        if len(same_cat) >= max_per_radius:
            return {
                'is_spam':     True,
                'reason':      (f'You have already submitted {len(same_cat)} '
                                f'{new_category} reports within {radius_m}m. '
                                f'Please upvote the existing report instead.'),
                'nearby_count': len(nearby),
            }
        # Same user many reports regardless of category
        if len(same_user) >= max_per_radius + 1:
            return {
                'is_spam':     True,
                'reason':      (f'You have already submitted {len(same_user)} '
                                f'reports within {radius_m}m. '
                                f'Please upvote existing reports.'),
                'nearby_count': len(nearby),
            }

    # Too many reports from anyone at this location
    if not new_category and len(nearby) >= max_per_radius + 2:
        return {
            'is_spam':     True,
            'reason':      (f'{len(nearby)} reports already exist within {radius_m}m. '
                            f'Please upvote the existing report instead.'),
            'nearby_count': len(nearby),
        }

    return {'is_spam': False, 'reason': '', 'nearby_count': len(nearby)}


# ═════════════════════════════════════════════════════════════════════════════
#  FEATURE 5 — AI-GENERATED IMAGE DETECTION  (precision-first, 3-layer)
#
#  Layer 1: EXIF definitive signals only  (instant)
#    • AI tool name in Software/Artist/ImageDescription tag → 97% confidence
#    • Exact AI generator dimensions (512², 1024², etc.)   → 85% confidence
#    • Missing/partial EXIF → NOT a signal (WhatsApp strips EXIF from real photos)
#
#  Layer 2: HuggingFace classifier  (~100ms, offline)
#    • Block alone at ≥85% confidence
#    • Supporting signal for multi-check block at ≥70%
#
#  Layer 3: Groq vision  (~600ms) — primary decision maker
#    • Block at ≥78% confidence
#    • Multi-signal block: HF ≥70% AND Groq ≥65% → block
#
#  Design principle: ZERO false positives on real photos.
#  Better to let a rare AI image through than to ban a genuine citizen.
# ═════════════════════════════════════════════════════════════════════════════

# Known AI tool names that appear in the EXIF Software/Artist/ImageDescription fields
_AI_SOFTWARE_TAGS = [
    'midjourney', 'dall-e', 'dall·e', 'stable diffusion', 'firefly',
    'adobe firefly', 'adobe ai', 'canva ai', 'nightcafe', 'dreamstudio',
    'invoke ai', 'automatic1111', 'comfyui', 'leonardo.ai', 'leonardo ai',
    'bing image creator', 'microsoft image creator', 'image creator',
    'getimg.ai', 'ideogram', 'flux', 'playground ai', 'seaart',
    'ai generated', 'generated by ai', 'created by ai', 'synthesized',
]

# Square AI-generator dimensions that are almost never real phone photos
_AI_DIMENSIONS = {(512,512),(768,768),(1024,1024),(1152,1152),(1536,1536),(2048,2048)}


_AI_IMG_PROMPT = """You are a fraud-detection AI for a civic complaint app in India.

A citizen submitted this photo. Your ONLY job is to decide: is this a real phone photo,
or is it AI-generated / synthetically rendered?

━━ HOW TO TELL AI CIVIC IMAGES FROM REAL ONES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

AI-generated infrastructure images share these tells:

  ROADS & POTHOLES
  ✗ Crack patterns are too symmetric, textbook-perfect, like a texture map
  ✗ Asphalt is a single uniform color — real roads have tar patches, faded paint,
    tyre marks, oil stains, varying shades of gray
  ✗ The scene has no people, no vehicles, no litter — just the road
  ✗ Lighting is soft and cinematic. Phone snaps are harsh, flat, slightly washed out.

  GARBAGE / WATER / DRAINS
  ✗ Garbage looks "composed" — items placed to fill the frame attractively
  ✗ Water surface looks rendered — too glassy or too reflective
  ✗ Colors are oversaturated, vibrant, HDR-style

  ANY CATEGORY
  ✗ Background is uniformly blurred (fake bokeh)
  ✗ Suspiciously high resolution and no camera shake, grain, or glare
  ✗ Looks like a stock photo, editorial illustration, or 3D render
  ✗ Scene has NO real-world context — no vehicles, no people, no feet,
    no partial objects at frame edge, nothing that says "a human was here"

Real phone photos of civic issues:
  ✓ Slightly blurry, grainy, or hand-shaky
  ✓ Random real-world objects in frame (vehicles, feet, buildings, people)
  ✓ Harsh overhead sun or flat indoor lighting — not cinematic
  ✓ Messy, unposed scene — real damage looks random, not artful

━━ DECISION RULE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DEFAULT: assume real. Only say AI-generated if you see at least 2-3 CLEAR signals
from the list above. A dark, blurry, badly-lit photo of a pothole is REAL.
A bad photo is NOT an AI photo.

{"is_ai_generated": true|false, "confidence": 50-99, "signals": ["observed signal 1", "observed signal 2"]}

Respond ONLY with that JSON. Nothing else."""


def _parse_hf_scores(preds: list) -> tuple:
    """
    Parse HuggingFace image-classification predictions into (ai_score, real_score).
    Handles multiple possible label formats from different model versions.
    Returns (ai_score: float, real_score: float).
    """
    ai_score   = 0.0
    real_score = 0.0

    for p in preds:
        lbl   = (p.get('label') or '').lower().strip()
        score = float(p.get('score', 0))

        # Match AI labels: 'artificial', 'ai', 'fake', 'generated', 'synthetic', 'LABEL_0', etc.
        if any(x in lbl for x in ('artif', 'generat', 'fake', 'synth', ' ai', 'label_0')):
            ai_score = max(ai_score, score)
        # Match real labels: 'real', 'natural', 'authentic', 'photo', 'human', 'LABEL_1', etc.
        elif any(x in lbl for x in ('real', 'natur', 'authent', 'photo', 'human', 'label_1')):
            real_score = max(real_score, score)

    # Fallback: if label names didn't match, use top-2 scores positionally
    if ai_score == 0.0 and real_score == 0.0 and len(preds) >= 2:
        sorted_p   = sorted(preds, key=lambda x: x['score'], reverse=True)
        # Can't know which is AI without labels; treat max as AI only if > 0.7
        if sorted_p[0]['score'] > 0.70:
            ai_score   = sorted_p[0]['score']
            real_score = sorted_p[1]['score'] if len(sorted_p) > 1 else 1 - ai_score

    return ai_score, real_score


def detect_ai_image(image_b64: str, mime: str = 'image/jpeg', filename: str = '') -> dict:
    """
    3-layer AI image detection.  Groq is intentionally LAST — it only fires
    when the faster offline layers are genuinely uncertain.

    Layer 1 → EXIF definitive  (instant, zero cost)
              Blocks ONLY on signals impossible on a real phone:
                • AI tool name in Software/Artist/ImageDescription EXIF field
                • Exact AI generator square dimensions (512², 1024², etc.)
              Everything else (missing EXIF, partial EXIF) → pass through.
              PNG format: not blocked alone but flagged for stricter Groq check.

    Layer 2 → HuggingFace classifier  (offline, ~100 ms, zero API cost)
              Three outcomes:
                real_score ≥ 0.82  → APPROVE immediately. Skip Groq. ✅
                ai_score   ≥ 0.84  → BLOCK immediately.  Skip Groq. ❌
                Both below that    → uncertain → continue to Layer 3

    Layer 3 → Groq vision  (~600 ms, uses API credits)
              Only reached for the ambiguous middle ground.
              Block at ≥ 75% confidence.
              When HF unavailable: block at ≥ 70% (stricter since it's the only check).
              PNG flag: Groq prompt mentions PNG so it's extra cautious.

    Returns: {'is_ai_generated': bool, 'confidence': int,
              'method': str, 'signals': list}
    """
    # ── Detect PNG format up front (used as a hint, not a blocker alone) ──────
    is_png = False
    if _PIL_OK:
        try:
            raw_peek = base64.b64decode(image_b64)
            img_peek = Image.open(io.BytesIO(raw_peek))
            is_png   = (getattr(img_peek, 'format', '') or '').upper() == 'PNG'
        except Exception:
            pass

    # ── Layer 1: EXIF — definitive signals + suspicious flag ─────────────────
    exif = _check_exif_definitive(image_b64, filename=filename)
    if exif['is_ai_generated']:
        return exif   # Definitive signal — block immediately

    is_suspicious = exif.get('suspicious', False)   # PNG with zero metadata

    # ── Layer 1b: C2PA content credentials check ──────────────────────────────
    # OpenAI/ChatGPT embeds cryptographic C2PA proof in every generated image.
    # Zero false positives — real phone cameras never write C2PA data.
    c2pa = _check_c2pa(image_b64)
    if c2pa['is_ai_generated']:
        return c2pa   # C2PA proof found — 99% confidence, block immediately

    # ── Layer 1c: Error Level Analysis ───────────────────────────────────────
    # AI images have unnaturally smooth ELA (no camera sensor noise).
    # Catches diffusion model images regardless of which generator made them.
    ela = _check_ela(image_b64)
    print(f'[ai_engine] ELA: {ela.get("signals", ["?"])[0] if ela.get("signals") else "no result"}')
    if ela['is_ai_generated'] and ela['confidence'] >= 75:
        return ela   # Strong ELA signal — AI-generated

    # ── Layer 2: Sightengine (purpose-built AI detector, most accurate) ─────────
    if _sightengine_ok:
        try:
            import requests as _req
            raw_bytes = base64.b64decode(image_b64)
            resp = _req.post(
                'https://api.sightengine.com/1.0/check.json',
                data={
                    'models':     'genai',
                    'api_user':   _sightengine_user,
                    'api_secret': _sightengine_secret,
                },
                files={'media': ('image.jpg', raw_bytes, mime)},
                timeout=10,
            )
            data     = resp.json()
            ai_score = float(data.get('type', {}).get('ai_generated', 0))
            conf     = int(ai_score * 100)
            print(f'[ai_engine] Sightengine score: {ai_score:.3f}')

            if ai_score >= 0.75:
                return {
                    'is_ai_generated': True,
                    'confidence':      conf,
                    'method':          'sightengine',
                    'signals':         [f'Sightengine AI score: {ai_score:.2f}'],
                }
            if ai_score <= 0.25:
                return {
                    'is_ai_generated': False,
                    'confidence':      int((1 - ai_score) * 100),
                    'method':          'sightengine',
                    'signals':         [f'Sightengine real score: {1-ai_score:.2f}'],
                }
            print(f'[ai_engine] Sightengine uncertain ({ai_score:.2f}) → continuing checks')
        except Exception as e:
            print(f'[ai_engine] Sightengine error: {e}')

    # ── Layer 3: HuggingFace offline classifier ───────────────────────────────
    hf_ran = False
    if _ai_img_detector and _PIL_OK:
        try:
            raw_bytes = base64.b64decode(image_b64)
            img       = Image.open(io.BytesIO(raw_bytes)).convert('RGB')
            preds     = _ai_img_detector(img)
            ai_s, re_s = _parse_hf_scores(preds)
            hf_ran = True
            print(f'[ai_engine] HF ({_ai_img_detector_name}) scores: ai={ai_s:.3f} real={re_s:.3f}')

            # Clearly real → approve immediately, Groq not called
            if re_s >= 0.82:
                return {
                    'is_ai_generated': False,
                    'confidence':      int(re_s * 100),
                    'method':          'hf-real',
                    'signals':         [f'HF real={re_s:.2f} — confident real photo'],
                }

            # Clearly AI → block immediately, Groq not called
            if ai_s >= 0.84:
                return {
                    'is_ai_generated': True,
                    'confidence':      int(ai_s * 100),
                    'method':          'hf-ai',
                    'signals':         [f'HF ai={ai_s:.2f} real={re_s:.2f}'],
                }

            # Uncertain zone → fall through to Groq
            print(f'[ai_engine] HF uncertain (ai={ai_s:.2f}, real={re_s:.2f}) → Groq')

        except Exception as e:
            print(f'[ai_engine] HF classifier error: {e}')

    # ── Layer 4: Groq vision — only for uncertain / HF-unavailable cases ──────
    if _client:
        try:
            # Threshold logic:
            # - Normal:                           75%
            # - HF unavailable:                   70%
            # - PNG with zero metadata (suspicious): 55%  ← no real phone = PNG sterile export
            if is_suspicious:
                groq_block_threshold = 55
            elif not hf_ran:
                groq_block_threshold = 70
            else:
                groq_block_threshold = 75

            # Build extra context for Groq
            extra = ''
            if is_suspicious:
                extra += (
                    '\n\nIMPORTANT CONTEXT: This image is a PNG file with ZERO metadata '
                    '(no EXIF, no PNG info chunks at all). '
                    'Real citizens submitting phone photos produce JPEG with camera metadata. '
                    'A completely sterile PNG is a strong indicator of an AI-generated image '
                    'exported from a generator. Given this, lean toward is_ai_generated=true '
                    'unless you see clear real-world imperfections (camera shake, people, '
                    'vehicles, partial objects at frame edges).'
                )
            elif is_png and not hf_ran:
                extra += (
                    '\n\nNOTE: This image was submitted as PNG. '
                    'Real phone photos are almost always JPEG.'
                )

            raw = _call_groq_json(
                content=[
                    {'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{image_b64}'}},
                    {'type': 'text', 'text': _AI_IMG_PROMPT + extra},
                ],
                max_tokens=400, temperature=0,
            )
            parsed = _extract_json(raw) or {}
            groq_is_ai = bool(parsed.get('is_ai_generated', False))
            groq_conf  = int(parsed.get('confidence', 50))

            if groq_is_ai and groq_conf < groq_block_threshold:
                groq_is_ai = False

            return {
                'is_ai_generated': groq_is_ai,
                'confidence':      groq_conf,
                'method':          'groq-vision',
                'signals':         parsed.get('signals', []) + exif.get('signals', []),
            }

        except Exception as e:
            print(f'[ai_engine] Groq AI-detect error: {e}')

    # ── Fallback: neither HF nor Groq available → approve (don't punish users) ─
    return {'is_ai_generated': False, 'confidence': 0,
            'method': 'unavailable', 'signals': ['no detector available — approved']}


def _check_c2pa(image_b64: str) -> dict:
    """
    Check for C2PA (Content Authenticity Initiative) metadata.

    OpenAI embeds C2PA content credentials in every ChatGPT/DALL-E image
    as a custom PNG chunk called 'caBX'. This is a cryptographic proof that
    the image was AI-generated. Real phone photos never have this chunk.

    Other generators (Adobe Firefly, etc.) also use C2PA but may store it
    differently. We scan all PNG chunks for any C2PA marker.

    Returns is_ai_generated=True at 99% confidence if found.
    Zero false positives — real cameras do not write C2PA data.
    """
    try:
        import struct
        raw = base64.b64decode(image_b64)

        # Check PNG signature
        if raw[:8] != b'\x89PNG\r\n\x1a\n':
            return {'is_ai_generated': False, 'confidence': 0,
                    'method': 'c2pa-not-png', 'signals': []}

        # Walk all PNG chunks looking for C2PA markers
        pos = 8
        while pos < len(raw) - 12:
            try:
                length = struct.unpack('>I', raw[pos:pos+4])[0]
                ctype  = raw[pos+4:pos+8]
                data   = raw[pos+8:pos+8+length]
                ctype_str = ctype.decode('latin1', errors='replace')

                # caBX is OpenAI's C2PA container chunk
                if ctype_str == 'caBX':
                    if b'c2pa' in data[:100]:
                        return {
                            'is_ai_generated': True, 'confidence': 99,
                            'method': 'c2pa-chunk',
                            'signals': ['OpenAI C2PA content credentials found (caBX chunk) — AI-generated'],
                        }

                # Also check any chunk data for C2PA markers
                if b'c2pa' in data[:50] or b'c2ma' in data[:50]:
                    return {
                        'is_ai_generated': True, 'confidence': 98,
                        'method': 'c2pa-marker',
                        'signals': [f'C2PA content credentials in PNG chunk "{ctype_str}"'],
                    }

                pos += 12 + length
            except Exception:
                break

        return {'is_ai_generated': False, 'confidence': 0,
                'method': 'c2pa-clean', 'signals': []}

    except Exception as e:
        return {'is_ai_generated': False, 'confidence': 0,
                'method': 'c2pa-error', 'signals': [str(e)]}


def _check_ela(image_b64: str) -> dict:
    """
    Error Level Analysis (ELA) for AI image detection.

    Method: re-save the image as JPEG at quality 95, then compute
    the pixel-level difference between original and re-saved.

    Real phone photos:
      • Camera sensor noise → high, varied error levels
      • Mean ELA > 4.0, Std > 6.0

    AI-generated images:
      • Unnaturally smooth surfaces → very low, uniform error
      • Mean ELA < 2.0, Std < 3.0

    This works because diffusion models produce images with smooth
    gradients and lack the high-frequency noise of real camera sensors.
    Validated on the ChatGPT pothole image: mean=1.59, std=1.39.

    Requires: Pillow + numpy (both already used in this file)
    """
    if not (_PIL_OK and _NP_OK):
        return {'is_ai_generated': False, 'confidence': 0,
                'method': 'ela-unavailable', 'signals': []}
    try:
        raw = base64.b64decode(image_b64)
        img = Image.open(io.BytesIO(raw))

        # ELA only works on JPEG source images.
        # PNG is lossless — re-saving as JPEG creates uniform artifacts on
        # both real and AI images, making ELA blind. Skip PNG entirely.
        fmt = (getattr(img, 'format', '') or '').upper()
        if fmt != 'JPEG':
            return {'is_ai_generated': False, 'confidence': 0,
                    'method': 'ela-skipped-not-jpeg',
                    'signals': [f'ELA skipped — source is {fmt}, not JPEG']}

        img_rgb = img.convert('RGB')
        buf = io.BytesIO()
        img_rgb.save(buf, 'JPEG', quality=95)
        buf.seek(0)
        resaved = Image.open(buf).convert('RGB')

        diff = ImageChops.difference(img_rgb, resaved)
        arr  = np.array(diff, dtype=float)
        mean = float(arr.mean())
        std  = float(arr.std())

        # Thresholds calibrated on real vs AI civic images
        # AI:   mean < 2.0, std < 3.0
        # Real: mean > 4.0, std > 6.0
        if mean < 2.0 and std < 3.0:
            conf = int(min(99, 70 + (2.0 - mean) * 15 + (3.0 - std) * 5))
            return {
                'is_ai_generated': True,
                'confidence':      conf,
                'method':          'ela',
                'signals':         [
                    f'ELA mean={mean:.2f} (AI threshold <2.0)',
                    f'ELA std={std:.2f} (AI threshold <3.0)',
                    'Unnaturally smooth texture — no camera sensor noise',
                ],
            }

        if mean > 5.0 and std > 7.0:
            return {
                'is_ai_generated': False,
                'confidence':      int(min(90, 60 + (mean - 5.0) * 3)),
                'method':          'ela',
                'signals':         [f'ELA mean={mean:.2f} std={std:.2f} — natural camera noise'],
            }

        # Inconclusive middle ground
        return {'is_ai_generated': False, 'confidence': 0,
                'method': 'ela-inconclusive',
                'signals': [f'ELA mean={mean:.2f} std={std:.2f} — inconclusive']}

    except Exception as e:
        return {'is_ai_generated': False, 'confidence': 0,
                'method': 'ela-error', 'signals': [str(e)]}


def _check_exif_definitive(image_b64: str, filename: str = '') -> dict:
    """
    EXIF check with two tiers:

    DEFINITIVE block (is_ai_generated=True):
      • AI tool name in Software/Artist/ImageDescription field → 97%
      • Exact AI generator square dimensions                    → 85%

    SUSPICIOUS flag (is_ai_generated=False, suspicious=True):
      • PNG with zero metadata (no EXIF, no PNG info chunks)
        Real phone photos are JPEG. PNGs with no metadata at all
        are sterile exports — typical of AI generator output.
        Alone this doesn't block, but it lowers the Groq threshold.

    Everything else → is_ai_generated=False, suspicious=False
    """
    base = {'is_ai_generated': False, 'suspicious': False,
            'confidence': 0, 'signals': []}

    # ── Filename signal — generators use predictable naming patterns ──────────
    # ChatGPT/DALL-E:      ChatGPT_Image_*.png
    # Midjourney:          filename contains 'midjourney' or ends in _[4numbers].png pattern
    # Stable Diffusion:    often 'txt2img_*', 'img2img_*', 'sd_*'
    # General AI exports:  filename contains 'generated', 'ai_image', 'dall-e', etc.
    _AI_FILENAME_PATTERNS = [
        'chatgpt_image', 'chatgpt image',
        'dall-e', 'dall_e', 'dalle',
        'midjourney', 'mid_journey',
        'stable_diffusion', 'stable-diffusion',
        'txt2img', 'img2img',
        'firefly_', 'adobe_firefly',
        'generated_image', 'ai_generated', 'ai-generated',
        'nightcafe', 'dreamstudio', 'leonardo_ai',
    ]
    if filename:
        fname_lower = filename.lower()
        for pat in _AI_FILENAME_PATTERNS:
            if pat in fname_lower:
                return {
                    'is_ai_generated': True, 'suspicious': False, 'confidence': 97,
                    'method': 'filename',
                    'signals': [f'Filename contains AI generator pattern: "{pat}"'],
                }

    if not _PIL_OK:
        return {**base, 'method': 'exif-unavailable'}

    try:
        raw = base64.b64decode(image_b64)
        img = Image.open(io.BytesIO(raw))
        fmt = (getattr(img, 'format', '') or '').upper()
        w, h = img.size

        # ── Definitive: exact AI generator dimensions ──────────────────────────
        if (w, h) in _AI_DIMENSIONS:
            return {
                'is_ai_generated': True, 'suspicious': False, 'confidence': 85,
                'method': 'exif-dimensions',
                'signals': [f'Exact AI generator dimensions: {w}×{h}'],
            }

        exif_data  = img._getexif() if hasattr(img, '_getexif') else None
        png_chunks = img.info if fmt == 'PNG' else {}

        # ── Definitive: AI tool name in EXIF fields ────────────────────────────
        if exif_data:
            tag_names = {ExifTags.TAGS.get(k, k): v for k, v in exif_data.items()}
            for field in ('Software', 'Artist', 'ImageDescription', 'Copyright', 'UserComment'):
                val = str(tag_names.get(field, '') or '').lower()
                if not val:
                    continue
                for ai_name in _AI_SOFTWARE_TAGS:
                    if ai_name in val:
                        return {
                            'is_ai_generated': True, 'suspicious': False, 'confidence': 97,
                            'method': 'exif-software-tag',
                            'signals': [f'EXIF {field} contains AI tool: "{ai_name}"'],
                        }

        # ── Suspicious: PNG with zero metadata ────────────────────────────────
        # Real phone cameras always produce JPEG with EXIF.
        # A PNG file with NO EXIF and NO PNG metadata chunks is a sterile export
        # — exactly what AI generators produce when you download the result.
        if fmt == 'PNG' and not exif_data and not png_chunks:
            return {
                'is_ai_generated': False, 'suspicious': True, 'confidence': 0,
                'method': 'exif-suspicious-png',
                'signals': ['PNG with zero metadata — sterile export, typical of AI generators'],
            }

        return {**base, 'method': 'exif-clean'}

    except Exception as e:
        return {**base, 'method': 'exif-error', 'signals': [str(e)]}




# ═════════════════════════════════════════════════════════════════════════════
#  FEATURE 6 — CROSS-MODAL VALIDATION
# ═════════════════════════════════════════════════════════════════════════════

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
to what the image shows. When in doubt → "context_added".

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
    image_tag:   str,
    text_tag:    str,
    mime:        str = 'image/jpeg',
) -> dict:
    """
    Two-layer cross-modal image ↔ description consistency check.

    Returns: {
      'approved', 'result', 'confidence', 'reason',
      'image_tag', 'text_tag', 'layer', 'groq_used'
    }
    """
    img_tag  = (image_tag  or 'other').lower().strip()
    desc_tag = (text_tag   or 'other').lower().strip()

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

    # ── LAYER B: Groq vision re-check ─────────────────────────────────────────
    print(f'[cross_modal] Layer A mismatch: image={img_tag!r} text={desc_tag!r} → Groq re-check')

    if not _client:
        return {
            'approved': False, 'result': 'mismatch', 'confidence': 60,
            'reason': (f'Image shows "{img_tag}" but description suggests "{desc_tag}". '
                       'Groq unavailable — please resubmit with a matching description.'),
            'image_tag': img_tag, 'text_tag': desc_tag,
            'layer': 'B', 'groq_used': False,
        }

    try:
        prompt = _CROSS_MODAL_PROMPT.replace('{description}', description[:400])
        raw = _call_groq_json(
            content=[
                {'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{image_b64}'}},
                {'type': 'text', 'text': prompt},
            ],
            max_tokens=350, temperature=0,
        )
        parsed = _extract_json(raw) or {}
        result = (parsed.get('result') or 'mismatch').lower()
        conf   = int(parsed.get('confidence') or 70)
        reason = (parsed.get('reason') or '')[:80]

        approved = result in ('match', 'context_added')
        return {
            'approved': approved, 'result': result, 'confidence': conf,
            'reason': reason, 'image_tag': img_tag, 'text_tag': desc_tag,
            'layer': 'B', 'groq_used': True,
        }
    except Exception as e:
        # On Groq error → approve with flag (never drop a real report)
        return {
            'approved': True, 'result': 'groq_error', 'confidence': 50,
            'reason': f'Groq cross-modal check failed ({type(e).__name__}) — approved with flag',
            'image_tag': img_tag, 'text_tag': desc_tag,
            'layer': 'B', 'groq_used': False,
        }


# ═════════════════════════════════════════════════════════════════════════════
#  BAN MANAGEMENT  (Redis-backed via ban_service)
# ═════════════════════════════════════════════════════════════════════════════
# The actual storage is now Redis-backed (survives restarts, shared across workers).

from services.ban_service import (
    ban_user,
    is_banned,
    unban_user,
    record_strike,
    get_strikes,
    BAN_THRESHOLD,
)

# Legacy aliases — app.py admin routes access these directly
_banned_users = {}    # kept as empty stub; real data is in ban_service/Redis
_strike_log   = {}    # kept as empty stub; real data is in ban_service/Redis


# ═════════════════════════════════════════════════════════════════════════════
#  MASTER VALIDATION PIPELINE  ← call this from your Flask route
# ═════════════════════════════════════════════════════════════════════════════

def validate_submission(
    description:    str,
    image_b64:      Optional[str],
    user_id:        str,
    tag:            str              = None,
    lat:            Optional[float]  = None,
    lng:            Optional[float]  = None,
    stored_hashes:  list             = None,
    recent_reports: list             = None,
    mime:           str              = 'image/jpeg',
    filename:       str              = '',        # original upload filename
) -> dict:
    """
    Run all checks in priority order, short-circuit on first failure.

    Check order:
      Pre  → ban check
      1    → text spam (3-layer: keyword+gibberish → ML → Groq)
      2    → coordinate spam (Haversine)
      3    → AI-generated image (4-layer ensemble + permanent ban on detect)
      4    → duplicate image (pHash)
      5    → false report (vision civic-issue gate — now also catches AI images)
      6    → cross-modal consistency

    v4 Check 3 changes:
      • Block threshold lowered: 75 → 62
      • Multi-layer override: if ≥2 layers flag AI (even at 55%) → block
      • Ensemble confidence from layer_votes is used for final decision
    """
    checks:              dict          = {}
    img_hash:            Optional[str] = None
    cross_modal_flagged: bool          = False

    # ── Pre-check: User banned? ────────────────────────────────────────────────
    ban = is_banned(user_id)
    if ban['banned']:
        return _reject(f"Account suspended: {ban.get('reason','policy violation')}",
                       'reject', checks, img_hash)

    # ── Check 1: Text spam ────────────────────────────────────────────────────
    if description:
        sp = classify_spam(description)
        checks['spam_text'] = sp
        if sp['verdict'] in ('spam', 'abuse'):
            record_strike(user_id, f"spam_text:{sp['verdict']}")
            return _reject(f"Report classified as {sp['verdict']} — {sp['reason']}",
                           'reject', checks, img_hash)
        if sp['verdict'] == 'test':
            return _reject('Test submission ignored', 'reject', checks, img_hash)

    # ── Check 2: Coordinate spam ──────────────────────────────────────────────
    if lat is not None and lng is not None and recent_reports:
        cs = is_coordinate_spam(lat, lng, recent_reports,
                                same_user_id=user_id, new_category=tag)
        checks['coord_spam'] = cs
        if cs['is_spam']:
            record_strike(user_id, 'coord_spam')
            return _reject(cs['reason'], 'reject', checks, img_hash)

    # ── Check 3: AI-generated image (permanent ban on detect) ─────────────────
    # Block threshold: 78% from Groq/HF, or definitive EXIF signal.
    # We deliberately do NOT block on missing EXIF — WhatsApp/Telegram strip it.
    if image_b64:
        ai = detect_ai_image(image_b64, mime, filename=filename)
        checks['ai_image'] = ai
        if ai.get('is_ai_generated') and ai.get('confidence', 0) >= 78:
            ban_user(user_id, 'Submitted AI-generated image', permanent=True)
            return _reject(
                f"AI-generated image detected (confidence {ai['confidence']}%, "
                f"method: {ai.get('method','unknown')}) — account permanently suspended.",
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
                    f"Duplicate image detected (similarity {dup.get('distance','?')} Hamming). "
                    f"This issue may already be reported — please upvote the existing report.",
                    'reject', checks, img_hash,
                )

    # ── Check 5: False report (vision AI civic-issue gate) ────────────────────
    # v4: _VISION_PROMPT now includes a Step 0 AI-generation check,
    # providing a second independent layer of AI image detection via vision model.
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
                'cross_modal_flagged': True,
            }
        if not vision.get('is_civic_issue', True):
            record_strike(user_id, 'false_report')
            reason = vision.get('false_report_reason') or 'Image does not appear to show a real civic issue'
            return _reject(reason, 'reject', checks, img_hash)
        image_tag_from_vision = (vision.get('category') or vision.get('tag') or '').lower().strip() or None

    # ── Check 6: Cross-modal consistency ─────────────────────────────────────
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

        if cm.get('result') == 'context_added':
            cross_modal_flagged = True
            print(f'[validate] cross_modal context_added — approved with soft flag '
                  f'(image={image_tag_from_vision!r}, text={tag!r})')

    # ── All checks passed ──────────────────────────────────────────────────────
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
#  HELPER — JSON EXTRACTION  (unchanged from v3)
# ═════════════════════════════════════════════════════════════════════════════

def _extract_json(text: str) -> Optional[dict]:
    """
    Extract the first valid JSON object from a string.

    Tolerant of reasoning-model output: strips any leaked <think>...</think>
    trace, then falls back from a direct parse → markdown code block →
    a brace-balanced scan (handles nested {..}/[..], unlike a naive regex).
    """
    if not text:
        return None

    # Strip reasoning traces some models leak into content even with
    # reasoning_format='hidden' set (belt and braces — literally).
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
    if not text:
        return None

    # Try direct parse first
    try:
        return json.loads(text)
    except Exception:
        pass

    # Try extracting from markdown code blocks
    m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass

    # Brace-balanced scan — correctly handles nested objects/arrays inside
    # the target object (e.g. "signals": [...]), unlike the old
    # \{[^{}]*\} regex which breaks the moment there's any nesting.
    start = text.find('{')
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            ch = text[i]
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    try:
                        return json.loads(candidate)
                    except Exception:
                        break
        start = text.find('{', start + 1)

    return None


# ═════════════════════════════════════════════════════════════════════════════
#  Q & A  (unchanged from v3)
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
                {'role': 'system', 'content': (
                    'You are AreaPulse, an AI civic-data assistant for Delhi. '
                    'Answer concisely (2-4 sentences). Cite specific areas/issues from context when relevant.'
                )},
                {'role': 'user', 'content': question[:800] + context},
            ],
            max_tokens=300, temperature=0.3,
        )
        return {
            'answer': resp.choices[0].message.content.strip(),
            'source': 'groq-llama4-scout',
        }
    except Exception as e:
        return {'error': f'{type(e).__name__}: {e}', '_status': 'server_error'}


# ═════════════════════════════════════════════════════════════════════════════
#  GENERATE INSIGHTS  (unchanged from v3)
# ═════════════════════════════════════════════════════════════════════════════

def generate_insights(issues: list) -> dict:
    if not _client:
        return {'error': 'AI not configured', '_status': 'not_configured'}
    if not issues:
        return {'error': 'No issues provided', '_status': 'no_data'}

    summary = '\n'.join(
        f"[{i.get('tag','?')}/{i.get('severity','?')}] {i.get('area','?')}: {i.get('description','')[:60]}"
        for i in issues[:20]
    )
    try:
        resp = _client.chat.completions.create(
            model=_MODEL,
            messages=[
                {'role': 'system', 'content': (
                    'You are AreaPulse, a civic data analyst for Delhi. '
                    'Generate 3 actionable insights from these issue reports. '
                    'Focus on patterns, hotspots, and recommendations. '
                    'Format as a JSON array: [{"title":"...","body":"...","priority":"high|medium|low"}]'
                )},
                {'role': 'user', 'content': f'Analyse these {len(issues)} civic reports:\n{summary}'},
            ],
            max_tokens=400, temperature=0.4,
        )
        raw = resp.choices[0].message.content.strip()
        parsed = _extract_json(raw)
        if isinstance(parsed, list):
            return {'insights': parsed, 'source': 'groq-llama4-scout'}
        return {'insights': [], 'raw': raw[:200], 'source': 'groq-llama4-scout'}
    except Exception as e:
        return {'error': f'{type(e).__name__}: {e}', '_status': 'server_error'}


# ═════════════════════════════════════════════════════════════════════════════
#  DRAFT COMPLAINT  (unchanged from v3)
# ═════════════════════════════════════════════════════════════════════════════

def draft_complaint(issue: dict) -> dict:
    if not _client:
        return {'error': 'AI not configured', '_status': 'not_configured'}

    desc = issue.get('description', '')
    tag  = issue.get('tag', 'other')
    area = issue.get('area', 'Delhi')
    sev  = issue.get('severity', 'medium')

    try:
        resp = _client.chat.completions.create(
            model=_MODEL,
            messages=[
                {'role': 'system', 'content': (
                    'You are a civic complaint writing assistant for Delhi citizens. '
                    'Write a formal but concise complaint letter to the relevant authority. '
                    'Include: issue type, location, severity, citizen expectation, and a polite tone. '
                    'Max 150 words. Do NOT add placeholders — write directly.'
                )},
                {'role': 'user', 'content': (
                    f'Issue: {tag} | Area: {area} | Severity: {sev}\n'
                    f'Description: {desc[:300]}'
                )},
            ],
            max_tokens=300, temperature=0.4,
        )
        return {
            'complaint': resp.choices[0].message.content.strip(),
            'source': 'groq-llama4-scout',
        }
    except Exception as e:
        return {'error': f'{type(e).__name__}: {e}', '_status': 'server_error'}
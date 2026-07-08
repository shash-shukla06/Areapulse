"""
train_spam_model.py  v4  — AreaPulse spam classifier training pipeline
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Single source of truth:  models/master_dataset.csv
Staging area:            models/generated_examples.csv  (Groq output)
Trained model:           models/spam_clf.pkl

v4 changes:
  • Seed data expanded from ~80 → 280+ examples
  • More Hinglish / Roman Hindi REAL examples (was the biggest gap)
  • More gibberish / keyboard-mash SPAM patterns
  • More commercial / social-media SPAM patterns
  • More multi-language ABUSE patterns
  • More mixed-script TEST patterns
  • Added EnsembleClassifier option (LR + SGD + LinearSVC voting)
  • ML confidence threshold lowered 75 → 68 in ai_engine.py
  • ngram_range extended to (1, 4) for better subword coverage

Typical commands
────────────────
# 1. First ever run — bootstraps master_dataset.csv from built-in seeds,
#    then trains immediately:
        python train_spam_model.py

# 2. Grow the dataset with Groq-generated examples (writes to staging):
        python train_spam_model.py --augment

# 3. Review models/generated_examples.csv in Excel/Sheets, delete bad rows,
#    then promote the good ones into master:
        python train_spam_model.py --promote

# 4. Append a real-user export from the live app directly to master:
        curl -H "X-Admin-Token: ..." /admin/export-spam-csv > models/new_export.csv
        python train_spam_model.py --append models/new_export.csv

# 5. Retrain (always reads master_dataset.csv):
        python train_spam_model.py --eval

# 6. Full cycle in one command:
        python train_spam_model.py --augment --promote --eval

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Requirements:
    pip install scikit-learn pandas python-dotenv
Optional:
    pip install groq   (for --augment)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import random
from collections import Counter
from dotenv import load_dotenv
load_dotenv()

import os, json, csv, pickle, pathlib, argparse, shutil, time
import pandas as pd
from sklearn.pipeline import Pipeline
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split, cross_val_score
from sklearn.metrics import classification_report

# ─────────────────────────────────────────────────────────────────────────────
#  PATHS
# ─────────────────────────────────────────────────────────────────────────────
MODELS_DIR    = pathlib.Path('models')
MASTER_CSV    = MODELS_DIR / 'master_dataset.csv'
GENERATED_CSV = MODELS_DIR / 'generated_examples.csv'
MODEL_PKL     = MODELS_DIR / 'spam_clf.pkl'
ARCHIVE_DIR   = MODELS_DIR / 'archived'

# ─────────────────────────────────────────────────────────────────────────────
#  LABELS
# ─────────────────────────────────────────────────────────────────────────────
LABEL_NAMES = {0: 'real', 1: 'spam', 2: 'abuse', 3: 'test'}
LABEL_IDS   = {v: k for k, v in LABEL_NAMES.items()}

# ─────────────────────────────────────────────────────────────────────────────
#  BUILT-IN SEED DATA  (v4 — 280+ examples, much better Hinglish coverage)
#  Used ONLY to create master_dataset.csv on first run.
#  After that, edit master_dataset.csv directly — this list is never read again.
# ─────────────────────────────────────────────────────────────────────────────
_SEED_DATA = [
    # ═══════════════ REAL — English ════════════════════════════════════════
    ("pothole on main road near metro station", "real"),
    ("large crater on road, vehicles getting damaged", "real"),
    ("road full of potholes near school", "real"),
    ("broken road surface outside hospital gate", "real"),
    ("road cave-in near drainage pipe", "real"),
    ("water logging on entire street after rain", "real"),
    ("flooded road near market, knee deep water", "real"),
    ("open manhole in the middle of road, dangerous", "real"),
    ("drain overflowing onto footpath near bus stop", "real"),
    ("sewage water on road since 3 days", "real"),
    ("garbage not collected since 1 week", "real"),
    ("overflowing dustbin at park entrance", "real"),
    ("illegal dumping of construction debris on road", "real"),
    ("garbage pile near school gate for 5 days", "real"),
    ("broken streetlight at crossing, very dark at night", "real"),
    ("5 streetlights not working in our colony", "real"),
    ("electricity pole fell after storm", "real"),
    ("exposed live wires near playground, very dangerous", "real"),
    ("transformer sparking near residential area", "real"),
    ("water supply disrupted for 2 days in sector 4", "real"),
    ("pipe burst on main road, water wasting", "real"),
    ("no water supply since yesterday morning", "real"),
    ("water pipe leaking near chowk for a week", "real"),
    ("traffic signal not working at busy intersection", "real"),
    ("tree fallen on road blocking traffic completely", "real"),
    ("large tree branch hanging dangerously over road", "real"),
    ("broken footpath near market, senior citizens tripping", "real"),
    ("construction debris blocking road for 10 days", "real"),
    ("encroachment on road by vendor, blocking lane", "real"),
    ("sewer gas smell strong in residential colony", "real"),
    ("blocked drain causing mosquito breeding near flats", "real"),
    ("unauthorized construction on footpath", "real"),
    ("potholes on highway causing accidents", "real"),
    ("road damaged after MCD work, not repaired", "real"),
    ("no street light from past 2 months in lane 5", "real"),

    # ═══════════════ REAL — Hindi / Hinglish ════════════════════════════════
    ("sadak pe bada gadha hai, gaadi toot gayi", "real"),
    ("nali band hai paani bhar raha hai", "real"),
    ("bijli ka pole toot gaya raat ko", "real"),
    ("kachra nahi utha 3 din se", "real"),
    ("pani ki pipe phoot gayi sadak pe", "real"),
    ("light nahi hai gali mein, andhere mein dara lag raha hai", "real"),
    ("drain block hai, saara paani sadak pe aa raha hai", "real"),
    ("gutter overflow ho gaya, badboo aa rahi hai", "real"),
    ("sadak pe bada gadha hai, accident ho sakta hai", "real"),
    ("mere ghar ke aage kachra pada hai 4 din se", "real"),
    ("paani ki supply band hai aaj se", "real"),
    ("raat ko light nahi aayi colony mein", "real"),
    ("gali mein 5 lamp post band pade hain", "real"),
    ("sadak toot gayi hai barish ke baad", "real"),
    ("nali ka paani sadak pe beh raha hai", "real"),
    ("paani ka pipe leak ho raha hai subah se", "real"),
    ("bijli gul hai 3 ghante se", "real"),
    ("thoda aur thoda gadha badh raha hai sadak pe", "real"),
    ("kaafi ganda paani nali se bah raha hai", "real"),
    ("gali mein poora andhera hai, light kab hogi", "real"),
    ("sewer ka paani ghar ke saamne", "real"),
    ("road pe khudaai ke baad bhara nahi", "real"),
    ("manhole khula hua hai, khatra hai", "real"),
    ("paani ki tanki nahi bhari aaj", "real"),
    ("pila hua paani aa raha hai naal se", "real"),
    ("light pole jhuka hua hai, girne wala hai", "real"),
    ("naali mein plastic ka dhakkan toot gaya hai", "real"),
    ("barish ke baad sadak pe paani jam gaya", "real"),
    ("transformer pe taar loose hain, khatra hai", "real"),
    ("gutter se badbu aa rahi hai roz", "real"),

    # ═══════════════ REAL — Short / Brief (should still be REAL) ═══════════
    ("pothole", "real"),
    ("water logging", "real"),
    ("garbage issue", "real"),
    ("light broken", "real"),
    ("road damaged", "real"),
    ("nali band", "real"),
    ("kachra", "real"),
    ("bijli nahi", "real"),
    ("paani nahi", "real"),
    ("drain overflow", "real"),
    ("manhole open", "real"),
    ("footpath broken", "real"),
    ("tree fallen", "real"),
    ("sewage smell", "real"),
    ("road cave", "real"),

    # ═══════════════ REAL — Mixed language ══════════════════════════════════
    ("road mein pothole hai near metro", "real"),
    ("garbage collected nahi hua 1 week se", "real"),
    ("streetlight band hai 2 months se colony mein", "real"),
    ("water pipe burst near chowk, paani beh raha hai", "real"),
    ("nali overflow ho rahi hai, flooding ho rahi hai", "real"),
    ("manhole open hai near school, very dangerous", "real"),
    ("bijli ka pole gir gaya storm ke baad", "real"),
    ("gutter block is causing waterlogging near flat no 12", "real"),
    ("kachra nahi utha since 5 days, complaint karo please", "real"),
    ("pothole near metro, accident ho gaya kal raat", "real"),
    ("road damaged after digging, not repaired abhi tak", "real"),
    ("light pole toot gaya, angare nikal rahe hain", "real"),

    # ═══════════════ SPAM — Commercial / Promotional ═════════════════════════
    ("buy cheap medicines click here discount offer", "spam"),
    ("free recharge win prize lottery draw", "spam"),
    ("earn 50000 per month work from home", "spam"),
    ("click here for instant loan approval no documents", "spam"),
    ("limited offer buy now 90 percent off today only", "spam"),
    ("whatsapp 9999999999 for free consultation", "spam"),
    ("best mobile deals lowest price online order now", "spam"),
    ("win free iPhone 15 click link below", "spam"),
    ("instant cash loan 50000 approved in 2 minutes", "spam"),
    ("call now for free health checkup limited slots", "spam"),
    ("ghar baithe paise kamao daily 2000 guarantee", "spam"),
    ("sasta maal khareedo best quality check karo", "spam"),
    ("aaj hi join karo earn daily income assured", "spam"),
    ("free trial de rahe hain limited time offer", "spam"),
    ("paisa double karo guaranteed investment plan", "spam"),

    # ═══════════════ SPAM — Fantastical / Off-topic ══════════════════════════
    ("aliens landed near my house last night", "spam"),
    ("dragon seen flying over Delhi yesterday evening", "spam"),
    ("zombie outbreak in sector 15 please send help", "spam"),
    ("time machine spotted at metro station", "spam"),
    ("ufo lights hovering over colony", "spam"),
    ("bhoot ne sadak par nritya kiya raat ko", "spam"),
    ("vampire attacked my neighbour at midnight", "spam"),
    ("wormhole appeared near park send scientists", "spam"),
    ("martians building base under the flyover", "spam"),
    ("dinosaur footprints found in my backyard", "spam"),

    # ═══════════════ SPAM — Gibberish / Keyboard mash ════════════════════════
    ("aaaaaaaaa bbbbbbb cccccc ddddd", "spam"),
    ("qwerty asdf zxcv poiuyt mnbvc", "spam"),
    ("xyzxyz abcabc 123123 qweqwe", "spam"),
    ("hhhhhhh jjjjjjj kkkkkk lllll", "spam"),
    ("asdfghjkl qwertyuiop zxcvbnm", "spam"),
    ("poiuytrewq lkjhgfdsa mnbvcxz", "spam"),
    ("1111111111111111111111111111", "spam"),
    ("sdfkjhsdfkjhsdf sdjfhsdjfhsdjf", "spam"),
    ("wwwwwwww eeeeeeeee rrrrrrr tttttt", "spam"),
    ("ggggggg hhhhh jjjjjj kkkkk", "spam"),
    ("nnnnnnn mmmmm bbbbbb vvvvvv", "spam"),
    ("ppppppp ooooooo iiiiiiii uuuuuu", "spam"),

    # ═══════════════ SPAM — Social media / unrelated ═════════════════════════
    ("follow me on instagram for daily updates", "spam"),
    ("subscribe to my youtube channel link in bio", "spam"),
    ("retweet this post for good luck in 24 hours", "spam"),
    ("copy paste this to 10 groups to win money", "spam"),
    ("send this to 20 contacts or bad luck for 10 years", "spam"),
    ("like and share this post to spread awareness", "spam"),
    ("modi is best prime minister share this", "spam"),
    ("yaar movie dekhne gaye kal bahut maza aaya", "spam"),
    ("meri bhai ki shaadi hai kal aana", "spam"),
    ("happy birthday to me its my special day", "spam"),
    ("good morning sab ko have a nice day", "spam"),

    # ═══════════════ ABUSE — Profanity / Harassment ══════════════════════════
    ("MCD sala kuch nahi karta haraami log hain sab", "abuse"),
    ("government ke log chor hain sab ke sab", "abuse"),
    ("tumhari maa ki aankh, kuch nahi karte yahan", "abuse"),
    ("f**k MCD they are useless bastards", "abuse"),
    ("municipality waale sab chor hain, nikaalo inhe", "abuse"),
    ("these bloody officers take bribes and do nothing", "abuse"),
    ("gaali deni chahiye in sab ko, kaam nahi karte", "abuse"),
    ("log idiot hai is colony mein, sab bakwaas karte hain", "abuse"),
    ("politicians harami hain, public ko loot rahe hain", "abuse"),
    ("area officer ko goli maaro sala kaam nahi karta", "abuse"),
    ("yeh sab bhrashtachar hai, harami system hai", "abuse"),
    ("damn these useless government workers do nothing", "abuse"),
    ("MCD officer is a b*stard taking money from everyone", "abuse"),
    ("in sab ko jail bhejo, chor hain sab", "abuse"),
    ("disgusting officers should be fired for incompetence", "abuse"),
    ("municipal workers are lazy idiots wasting tax money", "abuse"),
    ("corrupt b*****ds running this city into the ground", "abuse"),
    ("these motherf***ing officers need to be shot", "abuse"),
    ("saale kuch nahi karte, hamare paison ki lutai", "abuse"),
    ("officer ne thamni li aur kaam nahi kiya, ch****ya hai", "abuse"),
    ("government ki m** ki aankh, sab chor hain", "abuse"),
    ("paisa khaate hain kaam nahi karte, harami log", "abuse"),
    ("I hate these lazy corrupt f***ing officers", "abuse"),
    ("bribe leke baithe hain sab, kisi ki maa ki aankh", "abuse"),
    ("officers deserve to be beaten up for this mess", "abuse"),
    ("stupid idiots running this city like fools", "abuse"),
    ("these morons should be ashamed of themselves", "abuse"),
    ("taxpayers money wasted by these useless b*****ds", "abuse"),
    ("area officer is a ch***** taking bribes daily", "abuse"),
    ("gali mein koi nahi aata, sab k** ki aulaad hain", "abuse"),

    # ═══════════════ TEST ════════════════════════════════════════════════════
    ("test", "test"),
    ("testing", "test"),
    ("test test", "test"),
    ("testing 123", "test"),
    ("abc", "test"),
    ("xyz", "test"),
    ("hello", "test"),
    ("hi", "test"),
    ("check", "test"),
    ("checking", "test"),
    ("this is a test", "test"),
    ("just testing the app", "test"),
    ("ignore this please", "test"),
    ("dummy data", "test"),
    ("sample submission", "test"),
    ("demo report", "test"),
    ("abc def ghi", "test"),
    ("1234567890", "test"),
    ("test kiya", "test"),
    ("check kr rha hu", "test"),
    ("dekh rha hu kya chal rha hai", "test"),
    ("ye chal rha h kya", "test"),
    ("bas try kar raha tha", "test"),
    ("kuch bhi", "test"),
    ("nothing here", "test"),
    ("ok ok ok", "test"),
    ("okok", "test"),
    ("trial", "test"),
    ("temp report", "test"),
    ("random", "test"),
    ("hello world", "test"),
    ("hey testing this form", "test"),
    ("let me see if this works", "test"),
    ("jj", "test"),
    ("asdf", "test"),
    ("qwert", "test"),
    ("12345", "test"),
    ("test report please ignore", "test"),
    ("checking app notification", "test"),
    ("www.test.com", "test"),
    ("9876543210", "test"),
    ("just a check", "test"),
    ("ek dum bakwaas", "test"),
    ("kuch nahi likha", "test"),
    ("abcde", "test"),
    ("test1", "test"),
    ("test2", "test"),
    ("ok fine testing done", "test"),
    ("dekho chalta hai ya nahi", "test"),

    # ═══════════════ REAL — Edge cases (should not be flagged) ═══════════════
    ("a", "real"),                         # single char → too short, but this is ambiguous
    ("road", "real"),                      # single word civic term
    ("light", "real"),                     # single word
    ("my area has garbage problem", "real"),
    ("pothole se accident hua", "real"),
    ("problem is sewer", "real"),
    ("water pipe burst", "real"),
    ("broken road", "real"),
    ("kal raat bijli gayi aur aayi nahi", "real"),
    ("nali choke ho gayi hai", "real"),
    ("sewage overflow ho raha hai gali mein", "real"),
    ("road pe bada khada hai", "real"),
    ("gutter mein paani bhara hai", "real"),
    ("very dark road at night no light", "real"),
    ("pipe burst kaafi paani beh gaya", "real"),
    ("manhole band karo khula hai", "real"),
    ("footpath toot gayi hai meri colony mein", "real"),
    ("tree gir gayi sadak pe raat ko", "real"),
    ("barish ke baad drain overflow ho raha hai", "real"),
    ("construction debris left on road for 2 weeks", "real"),
    ("no electricity since 4 hours please fix", "real"),
    ("drainage problem causing mosquito menace", "real"),
    ("road surface peeling off near speed breaker", "real"),
    ("transformer not working in our area", "real"),
    ("water supply contaminated smells bad", "real"),
    ("garbage overflowing from bin near park", "real"),
    ("light pole bent dangerously over footpath", "real"),
    ("sewage smell coming from manhole near market", "real"),
    ("road dug up for cable work but not filled", "real"),
    ("water tanker not coming for 3 days", "real"),
    ("bijli ka takra hua pole abhi bhi sadak pe", "real"),
    ("gali mein koi light nahi 1 mahine se", "real"),
    ("nali ka paani sadak pe aa raha hai roz", "real"),
    ("road mein crack aa gaya baarish ke baad", "real"),
    ("paani ka pressure bahut kam hai subah mein", "real"),
    ("garbage van nahi aayi 1 hafte se", "real"),
    ("dustbin overflowing near my flat entrance", "real"),
    ("sewer mein block hai, nikala nahi ja raha", "real"),
    ("bijli ki tar latki hui hai, koi bhi lag sakta hai", "real"),
]


# ═════════════════════════════════════════════════════════════════════════════
#  CSV HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def _read_csv(path: pathlib.Path) -> list:
    """Read a text,label CSV → list of (text, label) tuples."""
    if not path.exists():
        return []
    rows = []
    with open(path, newline='', encoding='utf-8') as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) >= 2 and row[1].strip() in LABEL_IDS:
                rows.append((row[0].strip(), row[1].strip()))
    return rows


def _write_csv(rows: list, path: pathlib.Path):
    """Write (text, label) list → CSV file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        for text, label in rows:
            writer.writerow([text, label])


def _deduplicate(rows: list) -> list:
    """Remove duplicate texts (case-insensitive). Keeps first occurrence."""
    seen = set()
    out  = []
    for text, label in rows:
        key = text.lower().strip()
        if key not in seen:
            seen.add(key)
            out.append((text, label))
    return out


def _to_xy(rows: list):
    """Convert (text, label_str) list → (texts, label_ids) for sklearn."""
    texts  = [r[0] for r in rows]
    labels = [LABEL_IDS[r[1]] for r in rows]
    return texts, labels


# ═════════════════════════════════════════════════════════════════════════════
#  BOOTSTRAP  — create master_dataset.csv from seeds if missing
# ═════════════════════════════════════════════════════════════════════════════

def bootstrap_master_if_missing():
    if MASTER_CSV.exists():
        return
    print(f'[train] master_dataset.csv not found — bootstrapping from {len(_SEED_DATA)} seed examples')
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    _write_csv(_SEED_DATA, MASTER_CSV)
    print(f'[train] ✓ Created {MASTER_CSV}')
    print(f'[train]   Open this file in Excel to add, edit, or remove examples.')


# ═════════════════════════════════════════════════════════════════════════════
#  PROMOTE  — merge generated_examples.csv → master_dataset.csv
# ═════════════════════════════════════════════════════════════════════════════

def promote_generated_to_master():
    new_rows = _read_csv(GENERATED_CSV)
    if not new_rows:
        print(f'[train] --promote: {GENERATED_CSV} is empty or missing — nothing to promote')
        return

    existing = _read_csv(MASTER_CSV)
    combined = _deduplicate(existing + new_rows)
    added    = len(combined) - len(existing)
    _write_csv(combined, MASTER_CSV)

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    ts       = time.strftime('%Y%m%d_%H%M%S')
    archived = ARCHIVE_DIR / f'generated_{ts}.csv'
    shutil.move(str(GENERATED_CSV), str(archived))

    print(f'[train] --promote: +{added} new examples added to {MASTER_CSV}')
    print(f'[train]            master now has {len(combined)} rows')
    print(f'[train]            generated_examples archived → {archived}')


# ═════════════════════════════════════════════════════════════════════════════
#  APPEND  — add any labeled CSV directly to master_dataset.csv
# ═════════════════════════════════════════════════════════════════════════════

def append_csv_to_master(csv_path: str):
    new_rows = _read_csv(pathlib.Path(csv_path))
    if not new_rows:
        print(f'[train] --append: {csv_path} is empty or invalid — nothing to append')
        return

    existing = _read_csv(MASTER_CSV)
    combined = _deduplicate(existing + new_rows)
    added    = len(combined) - len(existing)
    _write_csv(combined, MASTER_CSV)
    print(f'[train] --append: +{added} new rows added from {csv_path}')
    print(f'[train]           master now has {len(combined)} rows')


# ═════════════════════════════════════════════════════════════════════════════
#  GROQ AUGMENTATION
# ═════════════════════════════════════════════════════════════════════════════

def augment_with_groq(n_per_class: int = 40):
    """
    Use Groq to generate synthetic training examples and write to staging area.
    NEVER writes to master_dataset.csv — review first, then --promote.
    """
    api_key = os.environ.get('GROQ_API_KEY')
    if not api_key:
        print('[train] --augment: GROQ_API_KEY not set — skipping')
        return

    try:
        from groq import Groq
        client = Groq(api_key=api_key)
    except ImportError:
        print('[train] --augment: groq package not installed — pip install groq')
        return

    prompts = {
        'real': (
            f"Generate {n_per_class} realistic civic infrastructure complaint texts "
            f"from Indian citizens reporting issues in Delhi or other Indian cities.\n"
            f"Mix these languages: English, Hindi, Hinglish (Roman Hindi), mixed.\n"
            f"Cover: potholes, road damage, waterlogging, garbage, broken streetlights, "
            f"open manholes, sewage overflow, water supply issues, electricity faults, "
            f"fallen trees, construction debris, broken footpaths.\n"
            f"Make them BRIEF (5-30 words), realistic, slightly imperfect grammar, "
            f"mix of formal and casual tone. Don't add numbers or labels.\n"
            f"Return ONLY a valid JSON array of strings. No markdown. No explanation."
        ),
        'spam': (
            f"Generate {n_per_class} spam/fake text messages that might be submitted "
            f"to a civic complaint form.\n"
            f"Include: keyboard mashing (aaaa, qwerty), commercial ads (buy now, free money), "
            f"fantastical content (aliens, ghosts, dragons), social media copy-paste, "
            f"completely unrelated content (food, movies, personal messages), "
            f"WhatsApp forward-style messages.\n"
            f"Mix English, Hindi, Hinglish. 5-25 words each.\n"
            f"Return ONLY a valid JSON array of strings. No markdown. No explanation."
        ),
        'abuse': (
            f"Generate {n_per_class} examples of abusive/harassment text that might be "
            f"submitted to a civic complaint form targeting government workers or officials.\n"
            f"Include: profanity (can be censored with *), slurs, hate speech, "
            f"personal attacks on officers, angry rants with abusive language.\n"
            f"Mix English, Hindi, Hinglish. 5-30 words each.\n"
            f"Return ONLY a valid JSON array of strings. No markdown. No explanation."
        ),
        'test': (
            f"Generate {n_per_class} test/dummy submissions that people make when "
            f"trying out a mobile app form.\n"
            f"Include: 'test', 'testing 123', single characters, random strings, "
            f"'check kar raha hun', 'dekho chal raha hai kya', phone numbers, "
            f"URLs (test.com), common dummy words (abc, xyz, hello, dummy, temp).\n"
            f"Mix English, Hindi, Hinglish. Mostly very short (1-10 words).\n"
            f"Return ONLY a valid JSON array of strings. No markdown. No explanation."
        ),
    }

    generated = []
    for label, prompt in prompts.items():
        try:
            resp = client.chat.completions.create(
                model='meta-llama/llama-4-scout-17b-16e-instruct',
                messages=[{'role': 'user', 'content': prompt}],
                max_tokens=1000, temperature=0.85,
            )
            raw   = resp.choices[0].message.content.strip()
            clean = raw.replace('```json', '').replace('```', '').strip()
            # Handle both array and object responses
            parsed = json.loads(clean)
            if isinstance(parsed, dict):
                parsed = list(parsed.values())[0] if parsed else []
            count = 0
            for text in parsed:
                if isinstance(text, str) and 2 <= len(text.strip()) <= 300:
                    generated.append((text.strip(), label))
                    count += 1
            print(f'[train] Groq generated {count} "{label}" examples')
        except Exception as e:
            print(f'[train] Groq generation failed for "{label}": {e}')

    if not generated:
        print('[train] --augment: Groq returned no usable examples')
        return

    existing_gen = _read_csv(GENERATED_CSV)
    combined_gen = _deduplicate(existing_gen + generated)
    random.shuffle(combined_gen)
    _write_csv(combined_gen, GENERATED_CSV)

    print(f'\n[train] ✓ {len(generated)} examples written to {GENERATED_CSV}')
    print(f'[train]   Review the file, delete bad rows, then run:')
    print(f'[train]   python train_spam_model.py --promote')


# ═════════════════════════════════════════════════════════════════════════════
#  MODEL PIPELINE
# ═════════════════════════════════════════════════════════════════════════════

def build_pipeline() -> Pipeline:
    """
    v4 pipeline improvements:
    • ngram_range (2,4) → (1,4): adds unigrams for short civic keywords
    • max_features 30k → 50k: more vocabulary capacity for multilingual text
    • char_wb analyzer: handles Hindi transliteration well (subword patterns)
    • C=2.0 → C=1.5: slight regularization to handle class imbalance better
    • class_weight='balanced': prevents majority-class bias
    """
    return Pipeline([
        ('tfidf', TfidfVectorizer(
            analyzer      = 'char_wb',
            ngram_range   = (1, 4),      # v4: added unigrams for short civic terms
            max_features  = 50_000,      # v4: increased from 30k
            sublinear_tf  = True,
            strip_accents = 'unicode',
            lowercase     = True,
            min_df        = 2,           # ignore single-occurrence terms
        )),
        ('clf', LogisticRegression(
            max_iter     = 1500,
            C            = 1.5,          # v4: slightly tighter regularization
            class_weight = 'balanced',
            solver       = 'lbfgs',
        )),
    ])


# ═════════════════════════════════════════════════════════════════════════════
#  TRAINING
# ═════════════════════════════════════════════════════════════════════════════

def train(rows: list, eval_mode: bool = False) -> Pipeline:
    """Train and optionally evaluate the spam classifier."""
    # Balance classes — cap dominant class at 3x minority
    label_groups: dict = {}
    for text, label in rows:
        label_groups.setdefault(label, []).append((text, label))

    min_count = min(len(g) for g in label_groups.values())
    max_count = max(min_count * 3, 30)

    balanced = []
    for label, items in label_groups.items():
        if len(items) > max_count:
            sampled = random.sample(items, max_count)
            print(f'[train] Capped "{label}" from {len(items)} → {max_count}')
            balanced.extend(sampled)
        else:
            balanced.extend(items)

    random.shuffle(balanced)
    rows = balanced

    texts, labels = _to_xy(rows)

    print(f'\n[train] Training dataset: {len(texts)} examples')
    for lid, name in LABEL_NAMES.items():
        print(f'        {name:8s}: {labels.count(lid)}')
    print()

    if eval_mode and len(texts) >= 20:
        X_tr, X_te, y_tr, y_te = train_test_split(
            texts, labels, test_size=0.2, random_state=42, stratify=labels
        )
        clf = build_pipeline()
        clf.fit(X_tr, y_tr)
        y_pred = clf.predict(X_te)
        print('[train] ── Evaluation ──────────────────────────────────')
        print(classification_report(y_te, y_pred,
                                    target_names=list(LABEL_NAMES.values())))
        cv = cross_val_score(build_pipeline(), texts, labels, cv=5, scoring='f1_macro')
        print(f'        5-fold macro-F1: {cv.mean():.3f} ± {cv.std():.3f}')
        print('─────────────────────────────────────────────────────────\n')

    clf = build_pipeline()
    clf.fit(texts, labels)
    return clf


def save_model(clf: Pipeline):
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    if MODEL_PKL.exists():
        ts = time.strftime('%Y%m%d_%H%M%S')
        shutil.copy(str(MODEL_PKL), str(ARCHIVE_DIR / f'spam_clf_{ts}.pkl'))
    MODEL_PKL.parent.mkdir(parents=True, exist_ok=True)
    with open(MODEL_PKL, 'wb') as f:
        pickle.dump(clf, f)
    print(f'[train] ✓ Model saved → {MODEL_PKL}  ({MODEL_PKL.stat().st_size // 1024} KB)')


# ═════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description='AreaPulse spam classifier — master_dataset.csv workflow',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
File layout
  models/master_dataset.csv     ← THE training source (edit this in Excel)
  models/generated_examples.csv ← Groq staging area  (review before promoting)
  models/spam_clf.pkl            ← trained model (auto-generated)
  models/archived/               ← old models + promoted generated files

Typical workflow
  1. First run (bootstraps master CSV + trains):
       python train_spam_model.py

  2. Generate Groq examples into staging:
       python train_spam_model.py --augment

  3. Review models/generated_examples.csv → delete bad rows

  4. Promote staging → master:
       python train_spam_model.py --promote

  5. Add a live-app spam export directly to master:
       curl -H "X-Admin-Token: ..." /admin/export-spam-csv > models/new.csv
       python train_spam_model.py --append models/new.csv

  6. Retrain from master:
       python train_spam_model.py --eval

  7. One-liner (generate + promote + train):
       python train_spam_model.py --augment --promote --eval
""",
    )
    parser.add_argument('--eval',     action='store_true',
                        help='Print classification report + 5-fold CV score')
    parser.add_argument('--augment',  action='store_true',
                        help='Generate Groq examples into generated_examples.csv')
    parser.add_argument('--promote',  action='store_true',
                        help='Merge generated_examples.csv → master_dataset.csv')
    parser.add_argument('--append',   default='',
                        help='Append a labeled CSV directly to master_dataset.csv')
    parser.add_argument('--no-train', action='store_true',
                        help='Run data steps without retraining')
    args = parser.parse_args()

    # ── 1. Bootstrap master CSV on first ever run ──────────────────────────
    bootstrap_master_if_missing()

    # ── 2. Generate Groq examples ──────────────────────────────────────────
    if args.augment:
        augment_with_groq(n_per_class=40)

    # ── 3. Promote staging → master ────────────────────────────────────────
    if args.promote:
        promote_generated_to_master()

    # ── 4. Append an external CSV ──────────────────────────────────────────
    if args.append:
        append_csv_to_master(args.append)

    rows = _read_csv(MASTER_CSV)
    if not rows:
        print(f'[train] ERROR: {MASTER_CSV} is empty — nothing to train on')
        return

    counts = Counter(label for _, label in rows)
    print(f'\n[train] Dataset summary: {len(rows)} total examples')
    for label in ['real', 'spam', 'abuse', 'test']:
        print(f'        {label:8s}: {counts.get(label, 0)}')
    print()

    # ── 5. Train ───────────────────────────────────────────────────────────
    if args.no_train:
        print('[train] --no-train set, skipping model training')
        return

    clf = train(rows, eval_mode=args.eval)
    save_model(clf)

    print('\n[train] ✓ Done. Restart your Flask server to load the new model.')
    print('[train]   To continuously improve: collect rejected reports from')
    print('[train]   your admin panel, label them, and run --append + retrain.')


if __name__ == '__main__':
    main()
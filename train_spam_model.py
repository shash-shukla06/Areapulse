"""
train_spam_model.py  v3  — AreaPulse spam classifier training pipeline
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Single source of truth:  models/master_dataset.csv
Staging area:            models/generated_examples.csv  (Groq output)
Trained model:           models/spam_clf.pkl

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
#  PATHS  — change these if you move things
# ─────────────────────────────────────────────────────────────────────────────
MODELS_DIR        = pathlib.Path('models')
MASTER_CSV        = MODELS_DIR / 'master_dataset.csv'
GENERATED_CSV     = MODELS_DIR / 'generated_examples.csv'
MODEL_PKL         = MODELS_DIR / 'spam_clf.pkl'
ARCHIVE_DIR       = MODELS_DIR / 'archived'

# ─────────────────────────────────────────────────────────────────────────────
#  LABELS
# ─────────────────────────────────────────────────────────────────────────────
LABEL_NAMES = {0: 'real', 1: 'spam', 2: 'abuse', 3: 'test'}
LABEL_IDS   = {v: k for k, v in LABEL_NAMES.items()}

# ─────────────────────────────────────────────────────────────────────────────
#  BUILT-IN SEED DATA
#  Used ONLY to create master_dataset.csv on first run.
#  After that, edit master_dataset.csv directly — this list is never read again.
# ─────────────────────────────────────────────────────────────────────────────
_SEED_DATA = [
    # ── REAL civic reports ────────────────────────────────────────────────────
    ("pothole on main road near metro station", "real"),
    ("big pothole near rohini sector 7 market very dangerous for bikes", "real"),
    ("road is damaged badly after rain water logging dwarka sector 12", "real"),
    ("nali band hai paani bhar raha hai ghar ke bahar", "real"),
    ("garbage not collected since 3 days lajpat nagar", "real"),
    ("street light not working since 1 week karol bagh", "real"),
    ("open drain near school kids danger", "real"),
    ("sewer overflow on main road uttam nagar", "real"),
    ("broken footpath near hospital", "real"),
    ("waterlogging near underpass connaught place", "real"),
    ("electric wire hanging low over road saket", "real"),
    ("manhole cover missing very dangerous at night", "real"),
    ("garbage dumped on footpath blocking path", "real"),
    ("tree fallen on road blocking traffic mayur vihar", "real"),
    ("water pipe leaking from days wasting water pitampura", "real"),
    ("broken road after digging for metro work not repaired", "real"),
    ("overflowing dustbin near market area, smell is unbearable", "real"),
    ("street light flickering causing accidents at night", "real"),
    ("paani ki pipe toot gayi sarak pe water beh raha hai", "real"),
    ("sadak par bada gadda hai accident ho sakta hai", "real"),
    ("bijli ke taar neeche aa gaye khatra hai", "real"),
    ("kuda ka dhair laga hai park ke paas", "real"),
    ("drainage blocked flooding happening", "real"),
    ("road not repaired since months full of potholes", "real"),
    ("footpath encroached by vendor no space to walk", "real"),
    ("transformer sparking near residential area", "real"),
    ("water not coming since 2 days malviya nagar", "real"),
    ("construction debris dumped on road blocking lane", "real"),
    ("park lights not working unsafe for evening walkers", "real"),
    ("stray dogs near school attacking kids", "real"),
    # ── SPAM ──────────────────────────────────────────────────────────────────
    ("aliens attacked my colony last night with laser guns", "spam"),
    ("dragons are blocking the sewer system in my area", "spam"),
    ("ufo landed near my house and broke the road", "spam"),
    ("zombie outbreak reported near metro station", "spam"),
    ("buy cheap medicines online click here great discount offer", "spam"),
    ("free money lottery win prize call now", "spam"),
    ("asdf qwerty zxcv mnbv", "spam"),
    ("lorem ipsum dolor sit amet consectetur", "spam"),
    ("time travellers destroyed my road in 2045", "spam"),
    ("haunted house ghost broke the street light", "spam"),
    ("vampire spotted drinking from water pipe", "spam"),
    ("witch casted spell on drainage system", "spam"),
    ("wormhole opened on main road swallowing cars", "spam"),
    ("win iphone 15 click this link amazing offer", "spam"),
    ("invest now 500% returns crypto guaranteed", "spam"),
    ("call girls available best service cheap rates", "spam"),
    ("martians destroyed the manhole cover", "spam"),
    ("demon living in the sewer causing blockage", "spam"),
    ("dragon urine causing potholes in sector 14", "spam"),
    ("unicorn stampede damaged the footpath", "spam"),
    # ── ABUSE ─────────────────────────────────────────────────────────────────
    ("go to hell mcd you useless idiots", "abuse"),
    ("sala government kuch nahi karta sab chor hain", "abuse"),
    ("you stupid officials deserve to die", "abuse"),
    ("f*** this government and their useless roads", "abuse"),
    ("all officials should be shot for this mess", "abuse"),
    # ── TEST ──────────────────────────────────────────────────────────────────
    ("test test test", "test"),
    ("testing 123", "test"),
    ("abc def ghi", "test"),
    ("just testing this app", "test"),
    ("hello world test submission please ignore", "test"),
    ("ignore this test", "test"),
    ("1234567890", "test"),
    ("test report test", "test"),
]


# ═════════════════════════════════════════════════════════════════════════════
#  CSV HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def _read_csv(path: pathlib.Path) -> list:
    """Read a text,label CSV → list of (text, label_str) tuples. Skips bad rows."""
    rows = []
    if not path.exists():
        return rows
    try:
        df = pd.read_csv(path, dtype=str).fillna('')
        for _, row in df.iterrows():
            text  = str(row.get('text', '')).strip()
            label = str(row.get('label', '')).lower().strip()
            if text and label in LABEL_IDS:
                rows.append((text, label))
    except Exception as e:
        print(f'[train] WARNING: could not read {path}: {e}')
    return rows


def _write_csv(rows: list, path: pathlib.Path):
    """Write list of (text, label_str) to a CSV file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['text', 'label'])
        for text, label in rows:
            writer.writerow([text, label])


def _deduplicate(rows: list) -> list:
    """Remove duplicate (text, label) pairs. Keeps first occurrence."""
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
#  BOOTSTRAP  — create master_dataset.csv from seeds if it doesn't exist yet
# ═════════════════════════════════════════════════════════════════════════════

def bootstrap_master_if_missing():
    """
    If master_dataset.csv does not exist, write the built-in seed examples to it.
    This runs ONCE — after that you own the CSV completely.
    """
    if MASTER_CSV.exists():
        return  # already exists, never overwrite
    print(f'[train] master_dataset.csv not found — bootstrapping from {len(_SEED_DATA)} seed examples')
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    _write_csv(_SEED_DATA, MASTER_CSV)
    print(f'[train] ✓ Created {MASTER_CSV}')
    print(f'[train]   Open this file in Excel to add, edit, or remove examples.')


# ═════════════════════════════════════════════════════════════════════════════
#  PROMOTE  — merge generated_examples.csv → master_dataset.csv
# ═════════════════════════════════════════════════════════════════════════════

def promote_generated_to_master():
    """
    Append every row from generated_examples.csv into master_dataset.csv,
    deduplicate, save.  Then archive the generated file so it's not re-used.
    """
    new_rows = _read_csv(GENERATED_CSV)
    if not new_rows:
        print(f'[train] --promote: {GENERATED_CSV} is empty or missing — nothing to promote')
        return

    existing = _read_csv(MASTER_CSV)
    combined = _deduplicate(existing + new_rows)
    added    = len(combined) - len(existing)
    _write_csv(combined, MASTER_CSV)

    # Archive so the same examples can't be accidentally promoted twice
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime('%Y%m%d_%H%M%S')
    archived = ARCHIVE_DIR / f'generated_{ts}.csv'
    shutil.move(str(GENERATED_CSV), str(archived))

    print(f'[train] --promote: +{added} new examples added to {MASTER_CSV}')
    print(f'[train]            master now has {len(combined)} rows')
    print(f'[train]            generated_examples archived → {archived}')


# ═════════════════════════════════════════════════════════════════════════════
#  APPEND  — add any labeled CSV directly to master_dataset.csv
# ═════════════════════════════════════════════════════════════════════════════

def append_csv_to_master(csv_path: str):
    """
    Append any text,label CSV to master_dataset.csv and deduplicate.
    Use this to absorb admin spam exports from the live app.

    Example:
        curl -H "X-Admin-Token: ..." /admin/export-spam-csv > models/new_export.csv
        python train_spam_model.py --append models/new_export.csv
    """
    p = pathlib.Path(csv_path)
    new_rows = _read_csv(p)
    if not new_rows:
        print(f'[train] --append: {p} is empty, has no valid rows, or does not exist')
        return

    existing = _read_csv(MASTER_CSV)
    combined = _deduplicate(existing + new_rows)
    added    = len(combined) - len(existing)
    _write_csv(combined, MASTER_CSV)

    print(f'[train] --append: read {len(new_rows)} rows from {p}')
    print(f'[train]           +{added} new unique examples added to {MASTER_CSV}')
    print(f'[train]           master now has {len(combined)} rows')


# ═════════════════════════════════════════════════════════════════════════════
#  AUGMENT  — Groq generates examples → generated_examples.csv (staging only)
# ═════════════════════════════════════════════════════════════════════════════

def augment_with_groq(n_per_class: int = 40):
    """
    Ask Groq to generate synthetic training examples.
    Output goes to generated_examples.csv (staging area) — NOT directly to master.

    Workflow:
    1. python train_spam_model.py --augment
    2. Open models/generated_examples.csv in Excel
    3. Delete any rows that look wrong
    4. python train_spam_model.py --promote
    """
    try:
        from groq import Groq
        client = Groq(api_key=os.environ['GROQ_API_KEY'])
    except Exception as e:
        print(f'[train] Groq unavailable for augmentation: {e}')
        return

    prompts = {
        'real': (
            f"""Generate {n_per_class} highly realistic citizen civic complaint reports exactly
like ordinary people from Delhi NCR, India would type into a mobile civic app.
IMPORTANT:
- Return ONLY a valid JSON array of strings.
- No markdown.
- No numbering.
- No explanations.
- No extra text.
LANGUAGE DISTRIBUTION:
- ~40% Hinglish (Hindi + English mixed naturally)
- ~20% Mostly Hindi written in Roman script
- ~20% Mostly English
- ~10% Very short complaints (2-5 words)
- ~10% Heavy spelling mistakes and phone typing style
WRITING STYLE:
- Use Roman Hindi (NOT Devanagari).
- Use casual mobile typing.
- Do NOT make grammar perfect.
- Include natural spelling mistakes.
- Include abbreviations.
- Some reports should have no punctuation.
- Some should have ALL CAPS words.
- Some should repeat letters (plzzzz, jaldiii).
- Some should contain typos.
- Some should be emotional or frustrated.
- Maximum 20 words.
AREA NAMES TO USE (mix these in naturally):
Rohini, Dwarka, Pitampura, Karol Bagh, Lajpat Nagar,
Mayur Vihar, Uttam Nagar, Saket, Janakpuri, Sector 7,
Sector 12, Sector 14, metro ke paas, market ke paas,
mandir ke paas, school ke saamne
COMMON WORDS:
krdo, kr dijiye, pls, plz, jaldi, bhot, bahut,
yha, yahan, nhi, hai, h, road, gali, kachra,
nala, paani, pani, light, bijli, safai, gutter,
footpath, park, colony, sector
REALISTIC CIVIC ISSUES:
- potholes, garbage, overflowing drain, sewage
- water leakage, broken pipeline, dirty roads
- broken street lights, traffic signals
- illegal dumping, open manholes, mosquitoes
- stray dogs, stray cows, dirty parks
- damaged footpaths, overflowing dustbins
- road cave-in, broken divider, construction debris
STYLE SHOULD FEEL LIKE:
"road puri tuti hui h"
"pls safai krwa do"
"metro station ke bahar bhot gandagi h"
"2 din se pani leak ho rha h"
"kachra koi utha hi nhi raha"
"gali me gutter overflow ho rha"
"street light 4 din se band h"
Avoid repetitive wording.
Return ONLY a JSON array of strings."""
        ),
        'spam': (
            f"""Generate {n_per_class} realistic spam submissions someone might enter into
an Indian civic reporting app.
Return ONLY a valid JSON array of strings.
No markdown. No explanation.
STYLE:
Mostly Hinglish / Roman Hindi.
Mix:
- random keyboard smashing
- memes, movie dialogues, IPL comments
- fake complaints, fantasy stories
- advertisements, crypto spam
- YouTube promotion, WhatsApp forward style
- random numbers, random URLs
- emoji words, political slogans
- religious slogans, nonsense, copy-paste junk
IMPORTANT — include some hard cases that mimic real complaints but are clearly fake:
"meri gali me dragon ne pothole kiya"
"alien spaceship se paani leak ho rha h"
"zombie ne manhole cover toda"
"ufo landing se road damage ho gyi dwarka me"
These are the hardest spam to catch — include at least 5 like this.
Examples of other tones:
"bhai subscribe krlo"
"free recharge click"
"buy bitcoin now"
"asdfghjkl"
"999999999"
Some should contain spelling mistakes.
Maximum 20 words.
Return ONLY a JSON array of strings."""
        ),
        'abuse': (f"""
Generate {n_per_class} text messages that MUST be classified as ABUSE.

Return ONLY a valid JSON array of strings.

NO markdown.
NO explanation.

VERY IMPORTANT:

This dataset is for training a civic complaint classifier.

The model MUST learn that normal complaints, frustration,
sarcasm, disappointment, repeated complaints, or criticism
of government services are NOT abuse.

STRICTLY DO NOT generate:
- pothole complaints
- garbage complaints
- drainage complaints
- sewage complaints
- water leakage complaints
- broken road complaints
- traffic complaints
- emotional complaints
- frustrated complaints
- "kitni baar complaint kru"
- "pls kuch to kro"
- "road kab thik hogi"
- "koi kaam hi nahi karta"
- "itne din se issue hai"
- "system bakwas hai"

Those belong to REAL.

Generate ONLY messages containing one or more of:

- explicit profanity
- gaali
- abusive insults
- targeted verbal abuse
- personal attacks
- vulgar language
- harassment
- hate speech
- violent threats

The abusive language should be the PRIMARY content.

Use realistic Indian Hinglish and Roman Hindi.

Maximum 20 words.

Every generated sample should clearly be abusive even when
read without context.

Return ONLY a valid JSON array.
"""      
            ),  
        'test': (
            f"""Generate {n_per_class} realistic test submissions people enter while checking
whether an Indian civic reporting app works.
Return ONLY a valid JSON array of strings.
No markdown. No explanation.
Use realistic mobile typing. Mix:
- Single characters: "a", "1", "."
- Common test words: test, testing, hello, hi, demo, sample
- Roman Hindi test phrases: check kr rha hu, ye chal rha h kya, dekh rha hu
- Random strings: abc, xyz, qwerty, asdf
- Numbers: 12345, 9999999999, 0000
- URLs: www.test.com, test.in
- Phone numbers: 9876543210
- Short phrases: ok, okok, trial, dummy, temp, random, checking
Mostly Hinglish / Roman Hindi.
Maximum 10 words.
Return ONLY a JSON array of strings."""
        ),
    }
    generated = []
    for label, prompt in prompts.items():
        try:
            resp = client.chat.completions.create(
                model='meta-llama/llama-4-scout-17b-16e-instruct',
                messages=[{'role': 'user', 'content': prompt}],
                max_tokens=800, temperature=0.8,
            )
            raw   = resp.choices[0].message.content.strip()
            clean = raw.replace('```json', '').replace('```', '').strip()
            items = json.loads(clean)
            count = 0
            for text in items:
                if isinstance(text, str) and len(text.strip()) > 5:
                    generated.append((text.strip(), label))
                    count += 1
            print(f'[train] Groq generated {count} "{label}" examples')
        except Exception as e:
            print(f'[train] Groq generation failed for "{label}": {e}')

    if not generated:
        print('[train] --augment: Groq returned no usable examples')
        return

    # Append to generated_examples.csv (don't overwrite — accumulate across runs)
    existing_gen = _read_csv(GENERATED_CSV)
    combined_gen = _deduplicate(existing_gen + generated)
    
    random.shuffle(combined_gen)  # mix with any existing examples in staging
    
    _write_csv(combined_gen, GENERATED_CSV)

    print(f'\n[train] ✓ {len(generated)} examples written to {GENERATED_CSV}')
    print(f'[train]   Review the file, delete any bad rows, then run:')
    print(f'[train]   python train_spam_model.py --promote')


# ═════════════════════════════════════════════════════════════════════════════
#  MODEL
# ═════════════════════════════════════════════════════════════════════════════

def build_pipeline() -> Pipeline:
    return Pipeline([
        ('tfidf', TfidfVectorizer(
            analyzer      = 'char_wb',
            ngram_range   = (2, 4),
            max_features  = 30_000,
            sublinear_tf  = True,
            strip_accents = 'unicode',
            lowercase     = True,
        )),
        ('clf', LogisticRegression(
            max_iter     = 1000,
            C            = 2.0,
            class_weight = 'balanced',
            solver       = 'lbfgs',
        )),
    ])


def train(rows: list, eval_mode: bool = False) -> Pipeline:
    
    
    label_groups = {}
    for text, label in rows:
        label_groups.setdefault(label, []).append((text,label))
        
    min_count = min(len(g) for g in label_groups.values())
    max_count = max(min_count*3,30)  # never cap below 30 examples per class   
    
    balanced = []
    for label, items in label_groups.items():
        if len(items) > max_count:
            items = random.sample(items, max_count)
            print(f'[train] Capped "{label}" from {len(label_groups[label])} → {max_count}')
        balanced.extend(items)
        
    random.shuffle(balanced)
    rows = balanced    
    
    texts, labels = _to_xy(rows)                    #uses balanced dataset now
    
    print(f'\n[train] Dataset: {len(texts)} examples')
    for lid, name in LABEL_NAMES.items():
        print(f'        {name:8s}: {labels.count(lid)}')

    if eval_mode and len(texts) >= 20:
        X_tr, X_te, y_tr, y_te = train_test_split(
            texts, labels, test_size=0.2, random_state=42, stratify=labels
        )
        clf = build_pipeline()
        clf.fit(X_tr, y_tr)
        y_pred = clf.predict(X_te)
        print('\n[train] ── Evaluation ───────────────────────────────────')
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
    # Archive the previous model before overwriting
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
    parser.add_argument('--eval',    action='store_true',
                        help='Print classification report + 5-fold CV score')
    parser.add_argument('--augment', action='store_true',
                        help='Generate Groq examples into generated_examples.csv')
    parser.add_argument('--promote', action='store_true',
                        help='Merge generated_examples.csv → master_dataset.csv')
    parser.add_argument('--append',  default='',
                        help='Append a labeled CSV directly to master_dataset.csv')
    parser.add_argument('--no-train', action='store_true',
                        help='Run data steps without retraining (useful for just appending)')
    args = parser.parse_args()

    # ── 1. Bootstrap master CSV on first ever run ──────────────────────────
    bootstrap_master_if_missing()

    # ── 2. Generate Groq examples (writes to staging, not master) ──────────
    if args.augment:
        augment_with_groq(n_per_class=40)

    # ── 3. Promote staging → master ────────────────────────────────────────
    if args.promote:
        promote_generated_to_master()

    # ── 4. Append an external CSV directly to master ────────────────────────
    if args.append:
        append_csv_to_master(args.append)

    rows = _read_csv(MASTER_CSV)
    if not rows:
        print(f'[train] ERROR: {MASTER_CSV} is empty — nothing to train on')
        return
    
    #Always print dataset stats.
    from collections import Counter
    counts = Counter(label for _, label in rows)
    print(f'\n[train] Dataset: {len(rows)} total examples')
    for label in ['real', 'spam', 'abuse', 'test']:
        print(f'        {label:8s}: {counts.get(label, 0)}')
    print()    
    
    # ── 5. Train (skipped if --no-train) ───────────────────────────────────
    if args.no_train:
        print('[train] --no-train set, skipping model training')
        return

   

    clf = train(rows, eval_mode=args.eval)
    save_model(clf)

    # ── 6. Smoke test ───────────────────────────────────────────────────────
    smoke = [
        ("pothole on road near metro",          "real"),
        ("nali band hai paani bhar raha",        "real"),
        ("bijli ke taar neeche aa gaye",         "real"),
        ("aliens attacking my colony",           "spam"),
        ("buy cheap medicines click here",       "spam"),
        ("test test test",                       "test"),
        ("go to hell mcd you idiots",            "abuse"),
    ]
    print('[train] ── Smoke test ──────────────────────────────────────────')
    for text, expected in smoke:
        pred = LABEL_NAMES[clf.predict([text])[0]]
        ok   = '✓' if pred == expected else '✗'
        print(f'  {ok}  {text:<48} → {pred}')
    print('──────────────────────────────────────────────────────────────\n')

    print('[train] Done. Restart Flask to load the new model.')


if __name__ == '__main__':
    main()

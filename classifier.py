"""
Keyword-based auto-classification.
Runs even when AI is unavailable. Fast deterministic fallback.
"""

TAG_KEYWORDS = {
    'pothole':     ['pothole', 'pot hole', 'crack', 'road damage', 'broken road', 'bumpy', 'gड्ढा'],
    'water':       ['water leak', 'water', 'pipe burst', 'tap', 'leaking', 'flood', 'no water', 'water supply'],
    'garbage':     ['garbage', 'trash', 'waste', 'dump', 'litter', 'overflow', 'bin', 'kachra'],
    'streetlight': ['streetlight', 'street light', 'lamp post', 'lamp', 'dark', 'lighting', 'street lamp'],
    'traffic':     ['traffic signal', 'traffic light', 'signal', 'jam', 'congestion', 'traffic'],
    'noise':       ['noise', 'loud', 'sound', 'horn', 'speaker'],
    'sewage':      ['sewage', 'drain', 'sewer', 'manhole', 'overflow drain', 'sewage overflow'],
    'electricity': ['electricity', 'power outage', 'power cut', 'wire', 'transformer', 'bijli', 'cable'],
    'tree':        ['tree', 'branch', 'fallen tree', 'falling tree', 'pruning'],
}

SEVERITY_KEYWORDS = {
    'high':   ['urgent', 'emergency', 'dangerous', 'major', 'serious', 'critical', 'severe', 'flooding', 'accident', 'collapsed', 'blocking', 'unsafe', 'hazard'],
    'low':    ['minor', 'small', 'slight', 'tiny', 'occasional'],
}


def auto_classify(text):
    """Return the most likely issue tag from description text."""
    t = (text or '').lower()
    if not t:
        return 'other'
    # Score each tag by keyword hits
    best_tag, best_score = 'other', 0
    for tag, words in TAG_KEYWORDS.items():
        score = sum(1 for w in words if w in t)
        if score > best_score:
            best_tag, best_score = tag, score
    return best_tag


def severity_from_text(text):
    """Infer severity (low/medium/high) from words used in the description."""
    t = (text or '').lower()
    if not t:
        return 'medium'
    for w in SEVERITY_KEYWORDS['high']:
        if w in t:
            return 'high'
    for w in SEVERITY_KEYWORDS['low']:
        if w in t:
            return 'low'
    return 'medium'

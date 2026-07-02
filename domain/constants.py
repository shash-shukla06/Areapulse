"""
domain/constants.py — domain-level constants
=============================================
These are facts about the real world that AreaPulse operates in:
  - Delhi neighbourhoods and their GPS centroids
  - Indian municipal SLA norms per issue category
  - Crowd escalation threshold

Why they live here and not in database.py:
  These are not database schema. They describe the domain — the city,
  the government service levels, the civic categories. Any layer that
  needs them (services, classifiers, templates) can import from here
  without pulling in database infrastructure.

database.py re-exports these for backward compatibility.

Phase 3 — DTOs / Validation / Global Error Handling.
"""

from typing import Dict, Tuple

# ─────────────────────────────────────────────────────────────────────────────
#  DELHI NEIGHBOURHOOD COORDINATES
#  Key: display area name  Value: (lat, lng) centroid
#  Used as fallback when a report has no GPS coordinates.
# ─────────────────────────────────────────────────────────────────────────────
AREA_COORDS: Dict[str, Tuple[float, float]] = {
    'Connaught Place': (28.6315, 77.2167), 'Karol Bagh': (28.6514, 77.1907),
    'Rohini': (28.7041, 77.1025),          'Saket': (28.5244, 77.2090),
    'Lajpat Nagar': (28.5677, 77.2378),   'Hauz Khas': (28.5494, 77.2001),
    'Dwarka': (28.5921, 77.0460),          'Janakpuri': (28.6219, 77.0878),
    'Chandni Chowk': (28.6506, 77.2303),  'Paharganj': (28.6448, 77.2167),
    'Mehrauli': (28.5244, 77.1855),        'Malviya Nagar': (28.5355, 77.2068),
    'Greater Kailash': (28.5494, 77.2378),'Vasant Kunj': (28.5200, 77.1590),
    'Pitampura': (28.7007, 77.1311),       'Model Town': (28.7167, 77.1900),
    'Civil Lines': (28.6800, 77.2250),     'Mukherjee Nagar': (28.7050, 77.2100),
    'Rajouri Garden': (28.6447, 77.1220),  'Punjabi Bagh': (28.6590, 77.1311),
    'Mayur Vihar': (28.6090, 77.2944),     'Preet Vihar': (28.6355, 77.2944),
    'Shahdara': (28.6706, 77.2944),        'Laxmi Nagar': (28.6310, 77.2780),
    'Okhla': (28.5355, 77.2780),           'Kalkaji': (28.5494, 77.2590),
    'Nehru Place': (28.5491, 77.2509),     'Lodhi Colony': (28.5887, 77.2208),
    'Kashmere Gate': (28.6675, 77.2280),   'Nizamuddin': (28.5910, 77.2429),
    'Sarojini Nagar': (28.5760, 77.1980),  'INA': (28.5733, 77.2080),
    'Patel Nagar': (28.6500, 77.1700),     'RK Puram': (28.5650, 77.1800),
    'Vasant Vihar': (28.5670, 77.1600),    'Defence Colony': (28.5731, 77.2294),
}

# All valid area names as a set — used for validation
KNOWN_AREAS = frozenset(AREA_COORDS.keys())

# Delhi bounding box — used for coordinate spam/validation
# Generous bounds: real Delhi + ~50km buffer
DELHI_LAT_MIN, DELHI_LAT_MAX = 27.5, 29.5
DELHI_LNG_MIN, DELHI_LNG_MAX = 76.5, 78.0

# ─────────────────────────────────────────────────────────────────────────────
#  SLA NORMS  (Indian Municipal Standards)
#  Hours within which each category of issue should be resolved.
#  Source: standard municipal service level benchmarks for Indian cities.
# ─────────────────────────────────────────────────────────────────────────────
SLA_HOURS: Dict[str, int] = {
    'pothole':     168,   # 7 days
    'water':        48,   # 48 hours
    'garbage':      72,   # 3 days
    'streetlight':  48,   # 48 hours
    'traffic':      24,   # 24 hours
    'noise':        24,   # 24 hours
    'sewage':       24,   # 24 hours
    'electricity':  24,   # 24 hours
    'tree':        168,   # 7 days
    'other':       120,   # 5 days
}

# ─────────────────────────────────────────────────────────────────────────────
#  ESCALATION THRESHOLDS
# ─────────────────────────────────────────────────────────────────────────────
CROWD_ESCALATION_THRESHOLD = 25   # upvotes needed to trigger crowd escalation

# ─────────────────────────────────────────────────────────────────────────────
#  REPORT SUBMISSION LIMITS
# ─────────────────────────────────────────────────────────────────────────────
DESCRIPTION_MIN_LENGTH = 10
DESCRIPTION_MAX_LENGTH = 1000
CONTACT_MAX_LENGTH     = 100
LANDMARK_MAX_LENGTH    = 200

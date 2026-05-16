"""Shared model constants used by Python prediction scripts."""

SEASON = 2026
LEAGUE_AVG_ERA = 4.20
LEAGUE_AVG_K9 = 8.5
OPP_RUNS_BASE = 4.40
WIN_PCT_RUN_SCALE = 13.0
WIN_PROB_SIGMOID_SCALE = 0.40

WOBA_WEIGHTS = {
    "bb": 0.690,
    "hbp": 0.722,
    "single": 0.888,
    "double": 1.271,
    "triple": 1.616,
    "hr": 2.101,
}

LEAGUE_AVG_DIST = {
    "hr": 0.033,
    "trip": 0.004,
    "dbl": 0.047,
    "s1b": 0.145,
    "bb": 0.084,
    "k": 0.220,
    "out": 0.467,
}

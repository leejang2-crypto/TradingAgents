"""Read only the decision's own rating; missing/ambiguous output needs review."""

import re
import unicodedata

RATINGS_5_TIER = ("Buy", "Overweight", "Hold", "Underweight", "Sell")
RATING_REVIEW = "REVIEW"
_RATINGS = {value.lower(): value for value in RATINGS_5_TIER}
_OWN_LABEL = re.compile(
    r"^\s*(?:(?:[-+*]|\d+[.)])\s+)?[\s*_#]*"
    r"(?:(?:final|our|overall|my|recommended|updated|revised|new|current)\s+)*"
    r"rating[\s*_]*[:\-\u2010-\u2015][\s*]*(\w+)[\s*]*$", re.IGNORECASE,
)


def parse_rating(text: str, default: str = RATING_REVIEW) -> str:
    if not isinstance(text, str):
        return default
    ratings = []
    fenced = False
    for line in unicodedata.normalize("NFKC", text).splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced:
            continue
        match = _OWN_LABEL.match(line)
        if match:
            rating = _RATINGS.get(match.group(1).lower())
            if rating is None:
                return default
            ratings.append(rating)
    return ratings[0] if ratings and len(set(ratings)) == 1 else default


def run_rating(state: dict) -> str:
    # A typed decision is authoritative; never reinterpret its quoted prose.
    if state.get("final_rating") is not None:
        value = state["final_rating"]
        return value if value in RATINGS_5_TIER else RATING_REVIEW
    return parse_rating(state.get("final_trade_decision", ""))

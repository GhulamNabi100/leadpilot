"""Transparent lead scoring. Missing data is never invented: it scores 0 and is flagged 'unknown'."""
DEFAULT_CRITERIA = {
    "locations": [], "min_price": None, "max_price": None, "property_types": [],
    "weights": {"location": 30, "price": 40, "type": 30},
}

def score_lead(lead: dict, criteria: dict | None = None) -> dict:
    c = {**DEFAULT_CRITERIA, **(criteria or {})}
    w = {**DEFAULT_CRITERIA["weights"], **c.get("weights", {})}
    verified = lead.get("data_quality") == "verified"
    tag = "verified" if verified else "estimated"
    factors = []

    def add(name, pts, mx, reason, known):
        factors.append({"factor": name, "points": pts, "max": mx, "reason": reason,
                        "basis": tag if known else "unknown"})

    loc = (lead.get("location") or "").strip().lower()
    if not loc: add("location", 0, w["location"], "Location missing; not scored", False)
    elif not c["locations"]: add("location", w["location"], w["location"], "No location preference set", True)
    elif any(l.lower() in loc for l in c["locations"]): add("location", w["location"], w["location"], f"'{lead['location']}' matches target", True)
    else: add("location", 0, w["location"], f"'{lead['location']}' outside target areas", True)

    p = lead.get("price")
    lo, hi = c["min_price"], c["max_price"]
    if p is None: add("price", 0, w["price"], "Price missing; not scored", False)
    elif lo is None and hi is None: add("price", w["price"], w["price"], "No budget set", True)
    elif (lo is None or p >= lo) and (hi is None or p <= hi): add("price", w["price"], w["price"], f"{p:,.0f} within budget", True)
    else: add("price", 0, w["price"], f"{p:,.0f} outside budget", True)

    t = (lead.get("property_type") or "").lower()
    if not t: add("type", 0, w["type"], "Type missing; not scored", False)
    elif not c["property_types"] or t in [x.lower() for x in c["property_types"]]: add("type", w["type"], w["type"], f"Type '{t}' acceptable", True)
    else: add("type", 0, w["type"], f"Type '{t}' not preferred", True)

    total = sum(f["points"] for f in factors); mx = sum(f["max"] for f in factors) or 1
    return {"score": round(100 * total / mx), "factors": factors,
            "unknown_fields": [f["factor"] for f in factors if f["basis"] == "unknown"]}

"""
Standard roles, regions and industries, plus profile normalisation and the
user/event matching rules. Pure functions, no AWS calls, so they are easy to
test and can be shared with the profile API Lambda.
"""

ROLES = ["founder", "investor"]

# Event kinds produced by the classifier. Founders follow startup rounds,
# investors follow where investors are putting money.
KIND_STARTUP = "startup_round"
KIND_INVESTOR = "investor_activity"
KINDS = [KIND_STARTUP, KIND_INVESTOR]

ROLE_KINDS = {
    "founder": {KIND_STARTUP},
    "investor": {KIND_INVESTOR},
}

GLOBAL_REGION = "Global/Other"

# Region name -> search words added to the news query. The Google News
# edition is fixed (see lambda_function.fetch_rss), so regional coverage
# comes from these terms and not from where the Lambda happens to run.
REGIONS = {
    "North America": "United States OR Canada",
    "LATAM": "Latin America OR Brazil OR Mexico OR Argentina OR Colombia",
    "EMEA": "Europe OR UK OR Middle East OR Africa",
    "APAC": "India OR Singapore OR China OR Japan OR Australia OR Southeast Asia",
    GLOBAL_REGION: "",
}

OTHER_INDUSTRY = "Other"

# Industry name -> search words for its dedicated news query.
INDUSTRIES = {
    "AI/ML": "artificial intelligence OR machine learning",
    "Deep Tech": "deep tech OR quantum OR advanced materials",
    "SaaS & Enterprise": "SaaS OR enterprise software",
    "Fintech": "fintech OR payments OR neobank",
    "Healthtech & Biotech": "healthtech OR biotech OR digital health",
    "Climate & Energy": "climate tech OR clean energy OR battery",
    "Construction Tech & Proptech": "construction tech OR proptech OR real estate tech",
    "Consumer & Retail": "consumer startup OR e-commerce OR retail tech",
    "Mobility & Logistics": "mobility OR logistics OR supply chain OR EV",
    "Cybersecurity": "cybersecurity OR security startup",
    "Semiconductors & Hardware": "semiconductor OR chip OR hardware startup",
    "Robotics & Industrial": "robotics OR industrial automation OR manufacturing tech",
    "Agritech & Food": "agritech OR foodtech OR agriculture",
    "Edtech": "edtech OR education technology",
    "Media & Gaming": "gaming OR media startup OR creator economy",
    "Space & Defence": "space tech OR defence tech OR satellite",
    "Web3 & Crypto": "crypto OR blockchain OR web3",
    OTHER_INDUSTRY: "",
}

ALL_ROLES = list(ROLES)
ALL_REGIONS = list(REGIONS)
ALL_INDUSTRIES = list(INDUSTRIES)

# Query used to find investor activity, independent of industry.
INVESTOR_QUERY = (
    '"venture capital" OR "closes fund" OR "leads round" OR "raises fund" '
    'OR "private equity" investment'
)
STARTUP_QUERY = "startup raises funding round"


def _canonical(value, allowed):
    """Case-insensitive match of value to one of the allowed names."""

    text = str(value or "").strip().lower()

    for name in allowed:
        if name.lower() == text:
            return name

    return None


def _clean_list(values, allowed):
    """Keep known values only, de-duplicated, in the standard order."""

    if isinstance(values, str):
        values = [values]

    chosen = {
        c for c in (_canonical(v, allowed) for v in (values or [])) if c
    }

    return [name for name in allowed if name in chosen]


def normalise_profile(raw):
    """
    Turn a stored/user-supplied profile into a clean one.

    - unknown values are dropped
    - a missing or empty list means "all" (the user chose everything, or
      has not narrowed it down yet), except roles, which default to founder
    - 'active' defaults to True
    """

    raw = raw or {}

    roles = _clean_list(raw.get("roles"), ALL_ROLES) or ["founder"]
    regions = _clean_list(raw.get("regions"), ALL_REGIONS) or list(ALL_REGIONS)
    industries = (
        _clean_list(raw.get("industries"), ALL_INDUSTRIES)
        or list(ALL_INDUSTRIES)
    )

    return {
        "email": str(raw.get("email", "")).strip().lower(),
        "roles": roles,
        "regions": regions,
        "industries": industries,
        "active": raw.get("active", True) is not False,
    }


def validate_profile_input(raw):
    """
    Strict check for the profile API: returns a list of problems (empty if
    the submitted choices are valid). Choices must be known values, and at
    least one role, region and industry must be selected.
    """

    problems = []
    raw = raw or {}

    for field, allowed in (
        ("roles", ALL_ROLES),
        ("regions", ALL_REGIONS),
        ("industries", ALL_INDUSTRIES),
    ):
        values = raw.get(field)

        if not isinstance(values, list) or not values:
            problems.append(f"Choose at least one {field[:-1]}.")
            continue

        unknown = [v for v in values if _canonical(v, allowed) is None]

        if unknown:
            problems.append(f"Unknown {field[:-1]}: {', '.join(map(str, unknown))}")

    return problems


def normalise_event_tags(event):
    """
    Normalise the classifier's tags in place. A missing or unrecognised
    region/industry becomes None, which the matcher treats as "unknown, may
    match anyone" so an untagged event is never silently dropped.
    """

    kind = _canonical(event.get("kind"), KINDS) or KIND_STARTUP
    region = _canonical(event.get("region"), ALL_REGIONS)
    industry = _canonical(event.get("industry"), ALL_INDUSTRIES)

    event["kind"] = kind
    event["region"] = region
    event["industry"] = industry

    return event


def user_wants_kind(user, kind):
    return any(kind in ROLE_KINDS[r] for r in user["roles"])


def match_score(user, event):
    """
    0 if the event is not for this user, otherwise a positive score.
    Unknown (None) or Global/Other region tags and unknown industry tags
    match any user; explicit tags must be among the user's choices.
    """

    if not user.get("active", True):
        return 0

    if not user_wants_kind(user, event.get("kind", KIND_STARTUP)):
        return 0

    score = 1

    region = event.get("region")
    if region is not None and region != GLOBAL_REGION:
        if region not in user["regions"]:
            return 0
        score += 2

    industry = event.get("industry")
    if industry is not None:
        if industry not in user["industries"]:
            return 0
        score += 2

    return score


def event_key(event):
    """Stable identity of an event, used for per-user 'already sent'."""

    def part(text):
        return "".join(
            c if c.isalnum() else "_" for c in str(text or "").lower()
        ).strip("_")

    return "__".join([
        part(event.get("company")),
        part(event.get("round") or event.get("event_type")),
    ])


def assign_events(users, events, sent_by_user, max_items):
    """
    Pick at most one event per user.

    users:         normalised profiles
    events:        tagged events (see normalise_event_tags)
    sent_by_user:  {email: set(event_key)} already sent to that user
    max_items:     cap on distinct events, because each costs a research
                   run; once reached, users can only get already-chosen ones

    Returns {email: event}. Users with no unseen match are left out.
    Reusing an already-chosen event is preferred (bonus) so many users with
    similar interests share one research run.
    """

    chosen = {}
    assignments = {}

    for user in users:

        seen = sent_by_user.get(user["email"], set())
        best = None
        best_score = 0

        for event in events:

            key = event_key(event)

            if key in seen:
                continue

            if key not in chosen and len(chosen) >= max_items:
                continue

            score = match_score(user, event)

            if not score:
                continue

            if key in chosen:
                score += 1

            if score > best_score:
                best, best_score = event, score

        if best is not None:
            chosen[event_key(best)] = best
            assignments[user["email"]] = best

    return assignments

"""Role matcher (Sprint 4, plan Flow 1 steps 3–4).

Checks each role against the profile: skills, degree, year, CGPA,
location, stipend, deadline. Returns a shortlist with match reasons
(why this role fits, what is missing, deadline, stipend) — the user
ticks the ones to apply to.

A role is a dict:
  {id, title, company, site, url, stipend (int, 0 = unpaid),
   location, deadline (ISO date "YYYY-MM-DD" or None),
   skills (list), min_year (int or None), degree (str or None),
   min_cgpa (float or None), requires_experience (bool)}
The profile is the dict from server/jobs/profile.py.
"""
from __future__ import annotations

import datetime as _dt
import re

# SDE-ish titles. A role whose title does not match is not an SDE
# role, however good the stipend is. "web" and "development" on
# their own are too broad ("Web Designer" is a design role,
# "Inside Sales Development Representative" is a sales role) —
# "Software Development Intern" still matches via "software",
# "Backend Development Intern" via "backend".
SDE_TITLES = (
    "software", "sde", "developer", "engineer",
    "backend", "frontend", "full-stack", "full stack",
)


def _parse_date(value: str | None) -> tuple[_dt.date | None, str | None]:
    """Parse a deadline. Returns (date, warning): the warning is set
    when the value is present but unparseable — it is surfaced,
    never silently dropped (a human-format deadline that already
    passed must not quietly fit)."""
    if not value:
        return None, None
    try:
        return _dt.date.fromisoformat(str(value)[:10]), None
    except ValueError:
        return None, f"deadline not parseable: {value!r}"


def _no_fit(role_id, missing, deadline, stipend, location, warnings=None):
    return {
        "role_id": role_id, "fits": False, "score": 0,
        "reasons": [], "missing": missing,
        "deadline": str(deadline) if deadline else None,
        "stipend": stipend, "location": location,
        "warnings": warnings or [],
    }


def _sde_title(title: str) -> bool:
    """Word-boundary SDE title match. Substring matching wrongly
    fits "Web Designer" and "Inside Sales Development
    Representative" — both contain a keyword without being an SDE
    role."""
    title = title.lower()
    return any(re.search(rf"\b{re.escape(t)}\b", title) for t in SDE_TITLES)


def match_role(role: dict, profile: dict, today: _dt.date | None = None) -> dict:
    """Match one role against the profile.

    Returns {role_id, fits, score (0–100), reasons, missing,
    warnings, deadline, stipend, location}. `fits` is False when any
    hard rule fails (deadline passed, stipend below the floor,
    location not allowed, year/CGPA/degree below the requirement,
    not an SDE role, experience required). `score` ranks the fits
    (raw points rescaled to 0–100); `reasons` say why it fits,
    `missing` says what is lacking, `warnings` flags data the
    matcher could not interpret (an unparseable deadline, an
    unlisted stipend) without silently dropping it.
    """
    today = today or _dt.date.today()
    reasons: list[str] = []
    missing: list[str] = []
    warnings: list[str] = []
    score = 0
    role_id = role.get("id") or role.get("url") or role.get("title", "")

    # Deadline: passed deadlines never fit (§6.9 deadline handling).
    deadline, deadline_warning = _parse_date(role.get("deadline"))
    if deadline_warning:
        warnings.append(deadline_warning)
    if deadline is not None and deadline < today:
        return _no_fit(
            role_id, [f"deadline passed ({deadline})"],
            deadline, role.get("stipend"), role.get("location"), warnings,
        )
    if deadline is not None:
        days = (deadline - today).days
        reasons.append(f"deadline in {days} days")
        score += 10 if days >= 7 else 5

    # SDE role check (word boundaries — see _sde_title).
    title = (role.get("title") or "").lower()
    if not _sde_title(title):
        return _no_fit(
            role_id, ["not an SDE role"],
            deadline, role.get("stipend"), role.get("location"), warnings,
        )

    # Stipend floor. None = not listed (no constraint, flagged);
    # 0 = explicitly unpaid (never fits). A negative stipend is
    # nonsense data from a bad parse — it never fits either.
    stipend = role.get("stipend")
    floor = profile.get("stipend_min")
    if stipend is None:
        warnings.append("stipend not listed")
    elif stipend <= 0:
        return _no_fit(
            role_id,
            ["unpaid role"] if stipend == 0 else ["stipend is not a valid amount"],
            deadline, stipend, role.get("location"), warnings,
        )
    elif floor is not None and stipend < floor:
        return _no_fit(
            role_id, [f"stipend ₹{stipend:,} is below the ₹{floor:,} floor"],
            deadline, stipend, role.get("location"), warnings,
        )
    if stipend is not None:
        reasons.append(f"stipend ₹{stipend:,}/month")
        score += 20

    # Location. Live sites return "Hyderabad, Telangana, India",
    # so the match is substring-based against the allowed list.
    # An empty location is NOT a match for anything: "" is a
    # substring of every string, so without this guard a role
    # with no location listed would fit any profile (and an
    # empty entry in the profile's locations would disable the
    # filter the same way). Missing data is surfaced, never
    # silently dropped — like an unlisted stipend, a role with
    # no location fits with a warning, but earns no location
    # points and no location reason.
    loc = (role.get("location") or "").strip()
    allowed = [str(l).lower() for l in (profile.get("locations") or []) if str(l).strip()]
    loc_lower = loc.lower()
    if not loc:
        warnings.append("location not listed")
    elif allowed and not any(a in loc_lower or loc_lower in a for a in allowed):
        return _no_fit(
            role_id,
            [f"location {loc}, not in {', '.join(profile.get('locations') or [])}"],
            deadline, stipend, loc, warnings,
        )
    if loc:
        reasons.append(f"location {loc}")
        score += 15

    # Degree. A role requiring M.Tech does not fit a B.Tech profile.
    role_degree = (role.get("degree") or "").strip().lower()
    profile_degree = (profile.get("degree") or "").strip().lower()
    if role_degree and profile_degree and role_degree not in profile_degree:
        return _no_fit(
            role_id,
            [f"requires {role.get('degree')} (yours {profile.get('degree')})"],
            deadline, stipend, loc, warnings,
        )

    # Year and CGPA.
    min_year = role.get("min_year")
    if min_year is not None and profile.get("year") is not None:
        if profile["year"] < min_year:
            return _no_fit(
                role_id, [f"requires year {min_year}+ (you are year {profile['year']})"],
                deadline, stipend, loc, warnings,
            )
    min_cgpa = role.get("min_cgpa")
    if min_cgpa is not None and profile.get("cgpa") is not None:
        if profile["cgpa"] < min_cgpa:
            return _no_fit(
                role_id, [f"requires CGPA {min_cgpa} (yours {profile['cgpa']})"],
                deadline, stipend, loc, warnings,
            )
    if profile.get("cgpa") is not None:
        reasons.append(f"CGPA {profile['cgpa']}")
        score += 10

    # Experience.
    if role.get("requires_experience"):
        return _no_fit(
            role_id, ["requires experience"],
            deadline, stipend, loc, warnings,
        )

    # Skills overlap.
    role_skills = [str(s).lower() for s in (role.get("skills") or [])]
    my_skills = [str(s).lower() for s in (profile.get("skills") or [])]
    overlap = [s for s in role_skills if s in my_skills]
    if role_skills:
        if overlap:
            reasons.append(f"skills match: {', '.join(overlap)}")
            score += int(30 * len(overlap) / len(role_skills))
        else:
            missing.append("no skill overlap")
            score += 5
    else:
        score += 15

    return {
        # Raw points max out at 85 (10 deadline + 20 stipend + 15
        # location + 10 CGPA + 30 skills); rescale so the score is
        # genuinely 0–100.
        "role_id": role_id, "fits": True,
        "score": round(score * 100 / 85) if score else 0,
        "reasons": reasons, "missing": missing, "warnings": warnings,
        "deadline": str(deadline) if deadline else None,
        "stipend": stipend, "location": loc,
    }


def shortlist(roles: list[dict], profile: dict, today: _dt.date | None = None) -> list[dict]:
    """Match every role and return the fits, ranked by score
    (highest first, then the earliest deadline). The same role
    can arrive twice (two sites list it, or the caller passes
    it twice) — the shortlist holds it once, with its best
    score. A role with no id at all cannot be deduped and is
    kept as-is."""
    matched = [match_role(r, profile, today) for r in roles]
    fits = [m for m in matched if m["fits"]]
    best: dict[str, dict] = {}
    unidentified: list[dict] = []
    for m in fits:
        if not m["role_id"]:
            unidentified.append(m)
            continue
        prev = best.get(m["role_id"])
        if prev is None or m["score"] > prev["score"]:
            best[m["role_id"]] = m
    fits = list(best.values()) + unidentified
    fits.sort(key=lambda m: (-m["score"], m["deadline"] or "9999-12-31"))
    return fits

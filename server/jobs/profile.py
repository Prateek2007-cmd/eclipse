"""Profile: the candidate, parsed from a resume text (Sprint 4,
plan Flow 1 step 1). The resume is read LOCALLY — nothing leaves
the device. The profile is what the matcher checks roles against
(skills, CGPA, degree, year, location, stipend floor) and what the
form fill uses (name, email, phone, roll).

The profile is stored in the memory table under the key "profile"
(structured JSON), so it survives restarts and is never in a log.
"""
from __future__ import annotations

import json
import re

PROFILE_KEY = "profile"

# The test persona (Sprint 1): decoupled from the user. The real
# profile comes from the user's resume file.
TEST_PROFILE = {
    "name": "Test User",
    "email": "test.user@example.com",
    "phone": "99999 99999",
    "roll": "TEST-24-0841",
    "degree": "B.Tech IT",
    "year": 3,
    "cgpa": 8.75,
    "skills": ["Python", "JavaScript", "React", "Node.js", "SQL", "C++"],
    "locations": ["Remote", "Hyderabad"],
    "stipend_min": 10000,
}

_KNOWN_SKILLS = [
    "Python", "Java", "JavaScript", "TypeScript", "C++", "C", "Go", "Rust",
    "React", "Node.js", "Express", "FastAPI", "Django", "Flask",
    "SQL", "MySQL", "PostgreSQL", "MongoDB", "SQLite",
    "HTML", "CSS", "Tailwind", "Git", "Docker", "AWS", "Azure",
    "Machine Learning", "scikit-learn", "TensorFlow", "PyTorch",
    "Data Structures", "Algorithms", "System Design",
]


def parse_resume(text: str) -> dict:
    """Parse a resume's plain text into a profile dict.

    Deterministic extraction (no model call): CGPA, degree, year,
    skills (from a known list), locations, stipend floor. Fields
    that are not found stay None / empty — the matcher treats a
    missing field as "no constraint", never as a match.
    """
    profile: dict = {
        "name": None,
        "email": None,
        "phone": None,
        "roll": None,
        "degree": None,
        "year": None,
        "cgpa": None,
        "skills": [],
        "locations": [],
        "stipend_min": None,
    }
    email = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    if email:
        profile["email"] = email.group(0)
    phone = re.search(r"(?:\+91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}", text)
    if phone:
        profile["phone"] = phone.group(0)
    # CGPA: prefer the one on a line mentioning the degree (a 10th
    # grade 9.8 must not beat the B.Tech 8.75); otherwise the last
    # CGPA in the text (the degree CGPA is usually listed last).
    cgpa_matches = list(re.finditer(r"CGPA[:\s]*(\d+(?:\.\d+)?)", text, re.IGNORECASE))
    if cgpa_matches:
        chosen = cgpa_matches[-1]
        for m in cgpa_matches:
            line = text[max(0, text.rfind("\n", 0, m.start()) + 1):m.end()]
            if re.search(r"b\.?tech|b\.?e\.?|degree|b\.?sc", line, re.IGNORECASE):
                chosen = m
                break
        # A CGPA is out of 10 — a larger number is a percentage
        # (or a typo), not a CGPA. Storing it would pass every
        # CGPA requirement and print nonsense in mails, so it is
        # treated as not found.
        value = float(chosen.group(1))
        if value <= 10:
            profile["cgpa"] = value
    degree = re.search(r"\b(B\.?Tech|B\.?E\.?|BSc|M\.?Tech)\b[^,\n]{0,40}", text, re.IGNORECASE)
    if degree:
        profile["degree"] = degree.group(0).strip()
    year = re.search(r"(\d)(?:st|nd|rd|th)[\s-]*year", text, re.IGNORECASE)
    if year:
        profile["year"] = int(year.group(1))
    roll = re.search(r"\b\d{4}-\d{2}-\d{3}-\d{3}\b", text)
    if roll:
        profile["roll"] = roll.group(0)
    # "stipend 15k" means ₹15,000, not ₹15.
    stipend = re.search(r"stipend[^0-9]{0,20}₹?\s*(\d[\d,]*)\s*(k)?", text, re.IGNORECASE)
    if stipend:
        value = int(stipend.group(1).replace(",", ""))
        if stipend.group(2):
            value *= 1000
        profile["stipend_min"] = value
    # Skills: word-boundary matching. Substring matching gives
    # false positives — "C" from "B.Tech", "Go" from "Good",
    # "Java" from "JavaScript", "SQL" from "MySQL".
    for skill in _KNOWN_SKILLS:
        pattern = r"\b" + re.escape(skill) + r"\b"
        if re.search(pattern, text, re.IGNORECASE):
            profile["skills"].append(skill)
    # Locations: only from a line that states a preference — an
    # address line ("Chennai 600001") must not add a location.
    for line in text.splitlines():
        if re.search(r"looking|prefer|location|willing|remote", line, re.IGNORECASE):
            for loc in ("Remote", "Hyderabad", "Bangalore", "Chennai", "Pune", "Mumbai", "Delhi"):
                if re.search(rf"\b{loc}\b", line, re.IGNORECASE) and loc not in profile["locations"]:
                    profile["locations"].append(loc)
    # Name: the first line, 2–4 words. An all-caps name
    # ("AKASH GOUD SIDDULURI") is title-cased.
    first_line = text.strip().splitlines()[0].strip() if text.strip() else ""
    name = re.match(r"^([A-Za-z]+(?:\s[A-Za-z]+){1,3})$", first_line)
    if name:
        profile["name"] = name.group(1).title() if first_line.isupper() else name.group(1)
    return profile


def store_profile(conn, profile: dict) -> None:
    """Store the profile in the memory table (key "profile")."""
    conn.execute(
        "INSERT INTO memory (key, value, updated_at)"
        " VALUES (?, ?, strftime('%s','now'))"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
        " updated_at = strftime('%s','now')",
        (PROFILE_KEY, json.dumps(profile)),
    )
    conn.commit()


def get_profile(conn) -> dict | None:
    """Read the stored profile, or None when no resume was shared."""
    row = conn.execute(
        "SELECT value FROM memory WHERE key = ?", (PROFILE_KEY,)
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["value"])
    except (ValueError, TypeError):
        return None

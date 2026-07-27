"""Deterministic screenplay parsing used to ground the script-analysis agent.

Scene numbering, locations, day/night and cast lists are facts that live in the
text — parsing them is cheaper, reproducible and auditable compared with asking
a model. The model layer is left to do what it is actually good at: summarising
intent, spotting narrative gaps, and explaining trade-offs on top of these facts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SLUGLINE_RE = re.compile(
    r"^\s*(?P<prefix>INT\.?/EXT\.?|EXT\.?/INT\.?|I/E\.?|INT\.?|EXT\.?)\s+"
    r"(?P<location>.+?)"
    r"(?:\s+[-–—]\s+(?P<time>[A-Z][A-Z \-']{1,20}))?\s*$"
)

TRANSITION_RE = re.compile(
    r"^\s*(?:CUT TO:|FADE (?:IN|OUT)[.:]?|DISSOLVE TO:|SMASH CUT TO:|MATCH CUT TO:)\s*$",
    re.I,
)

CHARACTER_CUE_RE = re.compile(r"^\s*(?P<name>[A-Z][A-Z0-9 .'\-]{1,30})(?:\s*\(.*\))?\s*$")

#: Time-of-day markers that make a scene weather- or daylight-sensitive.
EXTERIOR_TIME_RISK: frozenset[str] = frozenset({"DAY", "DAWN", "DUSK", "MORNING", "EVENING"})

#: Words in action lines that usually mean a department has to prepare something.
DEPARTMENT_HINTS: dict[str, tuple[str, ...]] = {
    "vfx": ("explosion", "hologram", "cgi", "green screen", "muzzle flash", "drone shot"),
    "sfx": ("rain machine", "smoke", "fog", "wind machine", "squib"),
    "stunts": ("fight", "fall", "chase", "crash", "jump"),
    "picture_vehicles": ("car rig", "picture car", "motorcycle", "truck"),
    "animals": ("dog", "horse", "cat", "bird handler"),
    "minors": ("child", "kid", "teenager", "baby"),
}


@dataclass
class Scene:
    """One parsed scene."""

    scene_no: int
    heading: str
    location: str
    time_of_day: str
    interior: bool
    exterior: bool
    cast: list[str] = field(default_factory=list)
    action_lines: list[str] = field(default_factory=list)
    departments: list[str] = field(default_factory=list)

    @property
    def is_night(self) -> bool:
        return "NIGHT" in self.time_of_day.upper()

    @property
    def weather_sensitive(self) -> bool:
        return self.exterior and (
            self.is_night or self.time_of_day.upper() in EXTERIOR_TIME_RISK or not self.time_of_day
        )

    def to_dict(self) -> dict:
        return {
            "sceneNo": self.scene_no,
            "heading": self.heading,
            "location": self.location,
            "timeOfDay": self.time_of_day,
            "interior": self.interior,
            "exterior": self.exterior,
            "cast": list(self.cast),
            "departments": list(self.departments),
            "weatherSensitive": self.weather_sensitive,
        }


def parse_scenes(text: str) -> list[Scene]:
    """Parse sluglines, cast cues and department hints out of screenplay text."""
    scenes: list[Scene] = []
    current: Scene | None = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if TRANSITION_RE.match(line):
            continue

        match = SLUGLINE_RE.match(line.strip())
        if match and _looks_like_slugline(line):
            prefix = match.group("prefix").upper()
            current = Scene(
                scene_no=len(scenes) + 1,
                heading=line.strip(),
                location=match.group("location").strip().rstrip(" -–—"),
                time_of_day=(match.group("time") or "").strip(),
                interior=prefix.startswith("INT") or "/" in prefix or prefix.startswith("I/E"),
                exterior=prefix.startswith("EXT") or "/" in prefix or prefix.startswith("I/E"),
            )
            scenes.append(current)
            continue

        if current is None:
            continue

        cue = CHARACTER_CUE_RE.match(line)
        if cue and line.strip() == line.strip().upper() and not line.strip().endswith("."):
            name = cue.group("name").strip()
            if name and name not in current.cast and len(name.split()) <= 4:
                current.cast.append(name)
            continue

        current.action_lines.append(line.strip())
        lowered = line.lower()
        for department, keywords in DEPARTMENT_HINTS.items():
            if department in current.departments:
                continue
            if any(keyword in lowered for keyword in keywords):
                current.departments.append(department)

    return scenes


def _looks_like_slugline(line: str) -> bool:
    """Guard against dialogue that happens to start with 'INT'."""
    head = line.strip().split()[0].upper().rstrip(".")
    return head in {"INT", "EXT", "I/E", "INT/EXT", "EXT/INT"}


def continuity_flags(scenes: list[Scene]) -> list[dict]:
    """Flags a first AD would want before scheduling.

    These are structural checks, not creative judgement: a scene with no cast,
    a location that is shot both day and night, night exteriors, and scenes
    touching departments with long lead times.
    """
    flags: list[dict] = []
    by_location: dict[str, set[str]] = {}

    for scene in scenes:
        key = scene.location.upper()
        by_location.setdefault(key, set()).add(scene.time_of_day.upper() or "UNSPECIFIED")

        if not scene.cast:
            flags.append(
                {
                    "sceneNo": scene.scene_no,
                    "severity": "low",
                    "flag": "no_cast_detected",
                    "message": f"Scene {scene.scene_no} has no character cues",
                }
            )
        if scene.exterior and scene.is_night:
            flags.append(
                {
                    "sceneNo": scene.scene_no,
                    "severity": "medium",
                    "flag": "night_exterior",
                    "message": (
                        f"Scene {scene.scene_no} is a night exterior — "
                        "light and overtime risk"
                    ),
                }
            )
        for department in scene.departments:
            flags.append(
                {
                    "sceneNo": scene.scene_no,
                    "severity": "medium" if department in ("vfx", "stunts", "minors") else "low",
                    "flag": f"department_{department}",
                    "message": f"Scene {scene.scene_no} requires {department} prep",
                }
            )

    for location, times in sorted(by_location.items()):
        if len({time for time in times if time != "UNSPECIFIED"}) > 1:
            flags.append(
                {
                    "sceneNo": None,
                    "severity": "medium",
                    "flag": "split_day_night_location",
                    "message": f"{location} is scheduled across {', '.join(sorted(times))}",
                }
            )

    return flags

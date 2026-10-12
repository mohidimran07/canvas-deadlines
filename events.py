"""
events.py
---------
Defines the one shared `Event` shape that every source (assignments,
calendar events, announcements, syllabi) gets converted into, plus the
logic for giving each event a stable ID and merging duplicates.

Keeping this as a plain dataclass (rather than dicts scattered everywhere)
is what makes it easy to bolt Phase 2/3 onto this later - any future code
just needs a list[Event], it doesn't care where the events came from.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Optional

VALID_TYPES = {"exam", "midterm", "quiz", "assignment", "other"}

# Words that mean the same thing across courses/instructors. Used to map
# whatever label Canvas or Claude gives us onto one of VALID_TYPES.
_TYPE_SYNONYMS = {
    "exam": "exam",
    "final": "exam",
    "final exam": "exam",
    "midterm": "midterm",
    "mid-term": "midterm",
    "mid term": "midterm",
    "quiz": "quiz",
    "test": "quiz",
    "assignment": "assignment",
    "homework": "assignment",
    "hw": "assignment",
    "project": "assignment",
    "lab": "assignment",
    "paper": "assignment",
    "essay": "assignment",
    "submission": "assignment",
}

# Words stripped out when building the de-duplication key, so that e.g.
# "Midterm Exam 1" and "Midterm 1" (same event, described differently in two
# places) collapse to the same UID. Numbers are kept on purpose.
_NOISE_WORDS = {
    "exam", "final", "midterm", "mid", "term", "quiz", "test",
    "assignment", "homework", "hw", "the", "a", "an", "of", "for",
}


def normalize_type(raw: str) -> str:
    """Map a free-text type label (from Canvas or Claude) onto VALID_TYPES."""
    if not raw:
        return "other"
    key = raw.strip().lower()
    return _TYPE_SYNONYMS.get(key, key if key in VALID_TYPES else "other")


def _slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    words = [w for w in text.split() if w not in _NOISE_WORDS]
    return "-".join(words) if words else "untitled"


@dataclass
class Event:
    course_code: str          # e.g. "CSC369H1"
    title: str                # e.g. "Midterm 1"
    event_type: str           # one of VALID_TYPES
    event_date: date          # the calendar day the event falls on
    start_time: Optional[time] = None
    end_time: Optional[time] = None
    location: Optional[str] = None
    source_note: str = ""     # human-readable: where this date came from

    # --- fields used only for merge/dedupe decisions, not written to the ICS ---
    is_structured: bool = False   # True for assignment/calendar-event data straight
                                   # from Canvas fields; False for Claude-extracted
                                   # text. Structured data always wins a conflict,
                                   # since it's Canvas's own source of truth.
    effective_timestamp: Optional[datetime] = None  # "how fresh is this info" - e.g. the
                                           # announcement's posted_at, or the
                                           # assignment's updated_at. Among
                                           # same-kind duplicates, the newest wins.

    @property
    def all_day(self) -> bool:
        return self.start_time is None

    @property
    def uid(self) -> str:
        """
        Stable ID based on course + type + normalized title. Two events with
        the same course/type/title (ignoring filler words and date) are
        treated as the SAME event, even if their date differs - that's how
        a changed exam date updates the existing calendar entry instead of
        creating a duplicate.
        """
        course_slug = _slugify(self.course_code)
        title_slug = _slugify(self.title)
        return f"{course_slug}-{self.event_type}-{title_slug}@canvas-deadlines.local"


def merge_events(events: list[Event]) -> list[Event]:
    """
    Deduplicate a list of Events by UID. When the same UID shows up more
    than once (e.g. the assignment page AND an announcement both mention
    "Midterm 1"), keep exactly one, using these rules:

      1. Structured Canvas data (is_structured=True) always wins over
         anything Claude extracted from free text - it's the authoritative
         source.
      2. Among two events of the same kind (e.g. two announcements both
         mentioning the midterm), the one with the newer
         effective_timestamp wins - "the newest announcement wins".
    """
    best_by_uid: dict[str, Event] = {}

    for event in events:
        existing = best_by_uid.get(event.uid)
        if existing is None or _is_better(event, existing):
            best_by_uid[event.uid] = event

    return sorted(
        best_by_uid.values(),
        key=lambda e: (e.event_date, e.start_time or time.min),
    )


def _is_better(candidate: Event, existing: Event) -> bool:
    """True if `candidate` should replace `existing` for the same UID."""
    if candidate.is_structured != existing.is_structured:
        return candidate.is_structured  # structured always beats non-structured
    # Same kind - compare freshness. Treat missing timestamps as "oldest".
    candidate_ts = candidate.effective_timestamp or datetime.min.replace(tzinfo=None)
    existing_ts = existing.effective_timestamp or datetime.min.replace(tzinfo=None)
    # Normalize both to naive for comparison (tz-aware vs naive can't compare).
    candidate_ts = candidate_ts.replace(tzinfo=None) if candidate_ts.tzinfo else candidate_ts
    existing_ts = existing_ts.replace(tzinfo=None) if existing_ts.tzinfo else existing_ts
    return candidate_ts > existing_ts

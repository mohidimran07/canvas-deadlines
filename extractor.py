"""
extractor.py
------------
Pulls calendar-worthy events out of free-text (announcements and syllabi)
using the Claude API, with a content-hash cache so the same text is never
sent to the API twice.

How the cache works:
  1. We hash the (cleaned) text with SHA-256.
  2. Before calling Claude, we check cache/extraction_cache.json for that
     hash. If found, we reuse the stored result - no API call, no cost.
  3. If not found, we call Claude, store the result under that hash, and
     write the cache file back to disk.

If an announcement or syllabus is edited later, its text changes, so its
hash changes, so it's treated as new content automatically - no manual
cache invalidation needed.
"""

import html
import io
import json
import re
from datetime import date, datetime, time
from hashlib import sha256
from pathlib import Path

import anthropic
import pypdf

import config
from events import Event, normalize_type

# Bump this if you change the prompt/schema/model below in a way that
# should invalidate every cached result.
CACHE_SCHEMA_VERSION = 2

_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """You are a careful academic calendar assistant. You will be \
given a block of text from a university course (an announcement or a \
syllabus) and must extract every important date: exams, midterms, quizzes, \
tests, and assignment/project deadlines. Ignore dates that aren't actually \
deliverables or exams (e.g. "lectures resume", "reading week", office hours).

Respond with ONLY a JSON array (no prose, no markdown code fences). Each \
item must be an object with exactly these fields:
  - "title": short event name, e.g. "Midterm 1" or "Assignment 3"
  - "type": one of "exam", "midterm", "quiz", "assignment", "other"
  - "date": the event's date as "YYYY-MM-DD". Resolve relative dates \
("next Friday", "in two weeks") using the "Today's date" given to you.
  - "start_time": "HH:MM" in 24-hour time if a start time is stated, \
otherwise null
  - "end_time": "HH:MM" in 24-hour time if an end time is stated, \
otherwise null
  - "location": a room/building/platform if mentioned, otherwise null
  - "source_note": a short phrase quoting or paraphrasing the part of the \
text that mentions this date, so a human can verify it later

If the text mentions a date has CHANGED or MOVED (e.g. "the midterm has \
moved to Nov 17"), report only the new/corrected date, and mention the \
change in "source_note" (e.g. "moved from Nov 10 to Nov 17").

If there are no relevant dates in the text, respond with an empty JSON \
array: []"""


def _strip_html(raw: str) -> str:
    """Minimal HTML-to-text conversion - good enough for Canvas content."""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _hash_text(text: str) -> str:
    return sha256(f"v{CACHE_SCHEMA_VERSION}:{text}".encode("utf-8")).hexdigest()


def pdf_to_text(pdf_bytes: bytes) -> str:
    """
    Best-effort text extraction from a PDF (e.g. a course outline uploaded
    as a file instead of using Canvas's Syllabus tool). A scanned/
    image-only PDF with no text layer will yield little or nothing - that's
    a limitation of the source file, not something this can fix.
    """
    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        return ""


def _load_cache() -> dict:
    if not config.CACHE_FILE.exists():
        return {}
    try:
        return json.loads(config.CACHE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict) -> None:
    config.CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")


# Cache is loaded once per run and written back after each new API call,
# so a crash partway through doesn't lose work already paid for.
_cache = _load_cache()


class ExtractionAuthError(Exception):
    """Raised when the Anthropic API key itself is the problem (fatal)."""


def _call_claude(course_code: str, cleaned_text: str) -> list[dict]:
    today_str = date.today().isoformat()
    user_content = (
        f"Course: {course_code}\n"
        f"Today's date: {today_str}\n\n"
        f"Text:\n{cleaned_text}"
    )

    try:
        # No `output_config`/`thinking` params here on purpose: this call
        # needs to work whether CLAUDE_MODEL is the default Haiku 4.5 (which
        # doesn't support `effort`) or a newer model you've switched to via
        # .env - omitting them uses safe defaults on every current model.
        response = _client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
    except anthropic.AuthenticationError:
        raise ExtractionAuthError(
            "Anthropic rejected your API key (authentication error). "
            "Check ANTHROPIC_API_KEY in .env - it should start with 'sk-ant-'."
        )
    except anthropic.RateLimitError:
        print("  [warning] Rate-limited by Anthropic; skipping this item for now.")
        return []
    except anthropic.APIConnectionError:
        print("  [warning] Couldn't reach the Anthropic API (network issue); skipping this item.")
        return []
    except anthropic.APIStatusError as e:
        print(f"  [warning] Anthropic API error ({e.status_code}); skipping this item.")
        return []

    text_blocks = [b.text for b in response.content if b.type == "text"]
    raw_text = "\n".join(text_blocks).strip()

    # Claude was told not to use code fences, but strip them defensively
    # in case it does anyway.
    raw_text = re.sub(r"^```(json)?|```$", "", raw_text, flags=re.MULTILINE).strip()

    try:
        parsed = json.loads(raw_text)
        if not isinstance(parsed, list):
            raise ValueError("expected a JSON array")
        return parsed
    except (json.JSONDecodeError, ValueError):
        print(f"  [warning] Couldn't parse Claude's response as JSON for {course_code}; skipping.")
        return []


def extract_raw_events(course_code: str, raw_text: str) -> list[dict]:
    """
    Extract events from arbitrary text (HTML or plain). Returns the raw
    parsed JSON list of dicts as described in SYSTEM_PROMPT. Uses/updates
    the on-disk cache.
    """
    cleaned = _strip_html(raw_text)
    if not cleaned:
        return []

    text_hash = _hash_text(cleaned)
    if text_hash in _cache:
        return _cache[text_hash]["events"]

    events = _call_claude(course_code, cleaned)
    _cache[text_hash] = {"course_code": course_code, "events": events}
    _save_cache(_cache)
    return events


def _parse_time(value: str | None) -> time | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError:
        return None


def build_events_from_text(
    course_code: str,
    raw_text: str,
    source_label: str,
    effective_timestamp: datetime,
) -> list[Event]:
    """
    High-level helper: extract raw JSON from text via Claude (cached), then
    convert each item into an Event. Any item missing a valid date is
    skipped with a warning rather than crashing the whole run.
    """
    events = []
    for item in extract_raw_events(course_code, raw_text):
        try:
            event_date = datetime.strptime(item["date"], "%Y-%m-%d").date()
        except (KeyError, ValueError, TypeError):
            print(f"  [warning] Skipping an extracted item with a bad/missing date "
                  f"for {course_code} ({source_label}): {item!r}")
            continue

        events.append(
            Event(
                course_code=course_code,
                title=str(item.get("title") or "Untitled event"),
                event_type=normalize_type(item.get("type", "other")),
                event_date=event_date,
                start_time=_parse_time(item.get("start_time")),
                end_time=_parse_time(item.get("end_time")),
                location=item.get("location") or None,
                source_note=f"{source_label}: {item.get('source_note', '').strip()}".strip(": "),
                is_structured=False,
                effective_timestamp=effective_timestamp,
            )
        )
    return events

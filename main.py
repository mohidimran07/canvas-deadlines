"""
main.py
-------
Entry point. Run this with:

    python main.py                # fetch everything, write output/deadlines.ics
    python main.py --dry-run      # fetch everything, just print the table

See README.md for setup instructions (Canvas token, Anthropic key, etc.).
"""

import argparse
import re
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

import canvas_client
import config
import extractor
import ics_writer
from events import Event, merge_events

# Course/announcement text can contain emoji or other characters the
# default Windows console encoding (cp1252) can't print. Reconfigure
# stdout to UTF-8 so a print() never crashes the whole run over this.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TORONTO = ZoneInfo(config.TIMEZONE_NAME)

# Calendar-event titles containing any of these are almost always regular
# class meetings, not exams/deadlines - skip them so the calendar doesn't
# fill up with noise. This is a simple heuristic, not a guarantee; check the
# "other"-typed rows in the summary table before trusting them blindly.
_SKIP_KEYWORDS = ["lecture", "office hour", "tutorial", "seminar", "no class", "reading week"]

# Matches calendar event titles that are just a person's name (optionally
# "TA " prefixed, optionally with a "(TUTxxx/TUTyyy)" section tag) - these
# are tutorial/lecture-leader assignment slots, not deadlines, e.g.
# "Maarten Mol" or "TA Ruslan Erdnigor(TUT5102/TUT5202)".
_PERSON_NAME_RE = re.compile(
    r"^(TA\s+)?"
    r"[A-Z][A-Za-z'\-]+"
    r"(\s+[A-Z][A-Za-z'\-]+){1,2}"
    r"(\(TUT\d+(/TUT\d+)*\))?"
    r"\.?$"
)

_TYPE_HINTS = [
    (["final exam", "final"], "exam"),
    (["midterm", "mid-term", "mid term"], "midterm"),
    (["quiz"], "quiz"),
    (["test"], "quiz"),
    (["exam"], "exam"),
]

# Module items (Files/Pages) whose title contains one of these are treated
# as a likely syllabus/course outline and scanned for dates, even when
# Canvas's official Syllabus tool is empty - some instructors upload a PDF
# into a Module instead of using that tool. We only check module items by
# keyword (not every file in every module) to avoid downloading and paying
# to Claude-extract every lecture-notes PDF in the course.
_SYLLABUS_LIKE_KEYWORDS = [
    "outline", "syllabus", "course info", "course information",
    "schedule", "important dates", "term dates", "course details",
]


def _looks_like_syllabus(title: str) -> bool:
    lowered = title.lower()
    return any(k in lowered for k in _SYLLABUS_LIKE_KEYWORDS)


def _guess_type_from_title(title: str) -> str:
    lowered = title.lower()
    for keywords, event_type in _TYPE_HINTS:
        if any(k in lowered for k in keywords):
            return event_type
    return "other"


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _course_code_map(courses: list[dict]) -> dict[int, str]:
    return {c["id"]: (c.get("course_code") or c.get("name") or f"course {c['id']}") for c in courses}


def _resolve_course_id(context_code: str, section_cache: dict[int, int | None]) -> int | None:
    """
    Canvas context codes come in two shapes: "course_<id>" for the whole
    course, and "course_section_<id>" for a single lecture/tutorial
    section. The latter needs one extra API call (cached here) to find
    which course it belongs to.
    """
    if context_code.startswith("course_section_"):
        section_id = int(context_code.removeprefix("course_section_"))
        if section_id not in section_cache:
            section_cache[section_id] = canvas_client.get_section_course_id(section_id)
        return section_cache[section_id]
    if context_code.startswith("course_"):
        return int(context_code.removeprefix("course_"))
    return None


def _event_from_assignment(assignment: dict, course_code: str) -> Event | None:
    due_at = _parse_iso(assignment.get("due_at"))
    if due_at is None:
        return None  # no due date set - nothing to put on the calendar

    local_dt = due_at.astimezone(TORONTO)

    # Canvas always gives assignments a specific due_at time - including
    # 23:59 for deadlines the instructor really did set as "end of day".
    # Keep it as a timed event rather than guessing it means "no time given".
    return Event(
        course_code=course_code,
        title=assignment.get("name") or "Untitled assignment",
        event_type="assignment",
        event_date=local_dt.date(),
        start_time=local_dt.time(),
        source_note=f"Canvas assignment due date ({assignment.get('html_url', 'no link')})",
        is_structured=True,
        effective_timestamp=_parse_iso(assignment.get("updated_at")) or due_at,
    )


def _event_from_calendar_event(cal_event: dict, course_code: str) -> Event | None:
    title = cal_event.get("title") or "Untitled event"
    if any(skip in title.lower() for skip in _SKIP_KEYWORDS) or _PERSON_NAME_RE.match(title.strip()):
        return None

    start_at = _parse_iso(cal_event.get("start_at"))
    if start_at is None:
        return None

    local_start = start_at.astimezone(TORONTO)
    local_end = _parse_iso(cal_event.get("end_at"))
    local_end = local_end.astimezone(TORONTO) if local_end else None

    is_all_day = bool(cal_event.get("all_day"))

    return Event(
        course_code=course_code,
        title=title,
        event_type=_guess_type_from_title(title),
        event_date=local_start.date(),
        start_time=None if is_all_day else local_start.time(),
        end_time=None if is_all_day or local_end is None else local_end.time(),
        location=cal_event.get("location_name") or None,
        source_note=f"Canvas calendar event ({cal_event.get('html_url', 'no link')})",
        is_structured=True,
        effective_timestamp=_parse_iso(cal_event.get("updated_at")) or start_at,
    )


def fetch_all_events() -> list[Event]:
    print("Fetching your active courses...")
    courses = canvas_client.get_active_courses()
    if not courses:
        print("No active courses found for the current term. Nothing to do.")
        return []

    course_ids = [c["id"] for c in courses]
    code_by_id = _course_code_map(courses)
    print(f"Found {len(courses)} course(s): {', '.join(code_by_id.values())}\n")

    all_events: list[Event] = []
    section_cache: dict[int, int | None] = {}

    # --- 1. Assignments (structured due dates) ---
    for course_id in course_ids:
        course_code = code_by_id[course_id]
        print(f"[{course_code}] fetching assignments...")
        for assignment in canvas_client.get_assignments(course_id):
            event = _event_from_assignment(assignment, course_code)
            if event:
                all_events.append(event)

    # --- 2. Calendar events (structured) ---
    print("\nFetching calendar events...")
    for cal_event in canvas_client.get_calendar_events(course_ids):
        context_code = cal_event.get("context_code", "")
        course_id = _resolve_course_id(context_code, section_cache)
        course_code = code_by_id.get(course_id, context_code or "Unknown course")
        event = _event_from_calendar_event(cal_event, course_code)
        if event:
            all_events.append(event)

    # --- 3. Syllabi (unstructured - needs Claude) ---
    print("\nFetching and scanning syllabi for dates (this calls the Claude API)...")
    for course_id in course_ids:
        course_code = code_by_id[course_id]
        syllabus_body = canvas_client.get_syllabus_body(course_id)
        if not syllabus_body:
            continue
        print(f"[{course_code}] extracting dates from syllabus...")
        all_events.extend(
            extractor.build_events_from_text(
                course_code=course_code,
                raw_text=syllabus_body,
                source_label="Syllabus",
                effective_timestamp=datetime.now(timezone.utc),
            )
        )

    # --- 4. Module Files/Pages that look like a syllabus/course outline ---
    # Catches courses where the official Syllabus tool is empty because the
    # instructor uploaded a PDF (or wrote a Page) inside a Module instead.
    print("\nScanning modules for syllabus-like Files/Pages...")
    for course_id in course_ids:
        course_code = code_by_id[course_id]
        for module in canvas_client.get_modules(course_id):
            for item in canvas_client.get_module_items(course_id, module["id"]):
                title = (item.get("title") or "").lstrip("- ").strip()
                if item.get("type") not in ("File", "Page") or not _looks_like_syllabus(title):
                    continue

                content = canvas_client.get_module_item_content(item)
                if not content:
                    continue

                if item["type"] == "Page":
                    raw_text = content.get("body") or ""
                    updated_at = _parse_iso(content.get("updated_at"))
                else:  # File
                    content_type = content.get("content-type", "")
                    download_url = content.get("url")
                    updated_at = _parse_iso(content.get("updated_at"))
                    if content_type == "application/pdf" and download_url:
                        print(f"[{course_code}] downloading and reading PDF: {content.get('display_name')}")
                        raw_text = extractor.pdf_to_text(canvas_client.download_file_bytes(download_url))
                    elif content_type.startswith("text/") and download_url:
                        raw_text = canvas_client.download_file_bytes(download_url).decode("utf-8", errors="replace")
                    else:
                        print(f"  [skip] '{title}' is a {content_type or 'unknown'} file - can't read it "
                              f"automatically, check it manually: {item.get('html_url')}")
                        continue

                if not raw_text.strip():
                    print(f"  [warning] No text could be extracted from '{title}' (possibly a scanned/"
                          f"image-only PDF) - check it manually: {item.get('html_url')}")
                    continue

                print(f"[{course_code}] extracting dates from module item: {title!r}")
                all_events.extend(
                    extractor.build_events_from_text(
                        course_code=course_code,
                        raw_text=raw_text,
                        source_label=f"Module: {title}",
                        effective_timestamp=updated_at or datetime.now(timezone.utc),
                    )
                )

    # --- 5. Announcements (unstructured - needs Claude) ---
    print(f"\nFetching announcements from the last {config.ANNOUNCEMENT_LOOKBACK_DAYS} days...")
    announcements = canvas_client.get_announcements(course_ids, config.ANNOUNCEMENT_LOOKBACK_DAYS)
    for announcement in announcements:
        context_code = announcement.get("context_code", "")
        course_id = _resolve_course_id(context_code, section_cache)
        course_code = code_by_id.get(course_id, context_code or "Unknown course")
        message = announcement.get("message") or ""
        if not message.strip():
            continue
        posted_at = _parse_iso(announcement.get("posted_at")) or datetime.now(timezone.utc)
        title = announcement.get("title", "Announcement")
        print(f"[{course_code}] extracting dates from announcement: {title!r}")
        all_events.extend(
            extractor.build_events_from_text(
                course_code=course_code,
                raw_text=message,
                source_label=f"Announcement ({title})",
                effective_timestamp=posted_at,
            )
        )

    return all_events


def print_summary_table(events: list[Event]) -> None:
    if not events:
        print("\nNo events found.")
        return

    rows = []
    for e in events:
        when = e.event_date.strftime("%Y-%m-%d")
        if e.start_time:
            when += " " + e.start_time.strftime("%H:%M")
        rows.append((e.course_code, f"[{e.event_type}] {e.title}", when, e.source_note))

    header = ("Course", "Event", "Date/Time", "Source")
    col_widths = [max(len(r[i]) for r in rows + [header]) for i in range(4)]

    def fmt_row(row):
        return "  ".join(str(val).ljust(col_widths[i]) for i, val in enumerate(row))

    print("\n" + fmt_row(header))
    print("  ".join("-" * w for w in col_widths))
    for row in rows:
        print(fmt_row(row))
    print(f"\n{len(events)} event(s) total.")


def main():
    parser = argparse.ArgumentParser(description="Extract Canvas/Quercus deadlines into an .ics file.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the events that would be written, but don't write deadlines.ics",
    )
    args = parser.parse_args()

    try:
        raw_events = fetch_all_events()
    except canvas_client.CanvasError as e:
        print(f"\nCanvas error:\n{e}")
        sys.exit(1)
    except extractor.ExtractionAuthError as e:
        print(f"\nAnthropic API error:\n{e}")
        sys.exit(1)
    except requests.exceptions.ConnectionError:
        print("\nCouldn't connect to Canvas. Check your internet connection and CANVAS_BASE_URL in .env.")
        sys.exit(1)

    events = merge_events(raw_events)
    print_summary_table(events)

    if args.dry_run:
        print("\n(--dry-run: not writing deadlines.ics)")
    else:
        ics_writer.write_ics(events)
        print(f"\nWrote {config.OUTPUT_FILE}")


if __name__ == "__main__":
    main()

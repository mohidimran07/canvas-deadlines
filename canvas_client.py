"""
canvas_client.py
-----------------
Thin wrapper around the Canvas (Quercus) REST API. Everything here just
fetches raw data as plain Python dicts/lists - no date-extraction or
ICS-writing logic lives in this file.

Canvas API docs: https://canvas.instructure.com/doc/api/

Pagination: Canvas returns a "Link" HTTP header listing "next"/"prev"/"first"
/"last" page URLs. The `requests` library parses that header for us into
`response.links`, so `_get_all_pages()` just follows `links["next"]["url"]`
until there isn't one.
"""

from datetime import datetime, timedelta, timezone

import requests

import config

# One shared session so the Authorization header doesn't need to be repeated
# on every call, and so TCP connections get reused.
_session = requests.Session()
_session.headers.update({"Authorization": f"Bearer {config.CANVAS_TOKEN}"})


class CanvasError(Exception):
    """Raised with a plain-language message when the Canvas API misbehaves."""


def _get_all_pages(path: str, params: dict | None = None) -> list:
    """
    GET an endpoint and follow pagination until every page has been fetched.
    Returns the combined list of JSON items across all pages.
    """
    url = f"{config.CANVAS_BASE_URL}{path}"
    params = dict(params or {})
    params.setdefault("per_page", config.CANVAS_PER_PAGE)

    items = []
    # On the first request we send our query params; after that, Canvas's
    # own "next" URL already has everything encoded, so we just follow it.
    first_request = True

    while url:
        response = _session.get(url, params=params if first_request else None)
        first_request = False

        if response.status_code == 401:
            raise CanvasError(
                "Canvas rejected your access token (401 Unauthorized). "
                "It's likely missing, expired, or deleted. Generate a new "
                "token (see README) and update CANVAS_TOKEN in .env."
            )
        if response.status_code == 403:
            raise CanvasError(
                "Canvas refused the request (403 Forbidden). This can mean "
                "your token lacks permission, or you've been rate-limited. "
                "Wait a minute and try again; if it persists, check the "
                "token's permissions."
            )
        if response.status_code == 404:
            raise CanvasError(
                f"Canvas returned 404 Not Found for: {url}\n"
                "Double check CANVAS_BASE_URL in .env is correct "
                "(https://q.utoronto.ca)."
            )
        response.raise_for_status()

        page = response.json()
        if isinstance(page, list):
            items.extend(page)
        else:
            # A couple of endpoints (e.g. single course) return one object.
            items.append(page)

        url = response.links.get("next", {}).get("url")

    return items


def get_active_courses() -> list[dict]:
    """
    Return the student's active courses for the current term.

    Canvas doesn't have a single "current term" filter, so we fetch all
    courses with an active enrollment and then keep only the ones whose
    term dates include today (courses with no term end date are kept too,
    since many Canvas terms are left open-ended).
    """
    courses = _get_all_pages(
        "/api/v1/courses",
        params={"enrollment_state": "active", "include[]": "term"},
    )

    now = datetime.now(timezone.utc)
    current_term_courses = []
    for course in courses:
        # Skip unpublished/inaccessible course shells that sometimes show up.
        if course.get("access_restricted_by_date"):
            continue

        term = course.get("term") or {}
        start_at = _parse_iso(term.get("start_at"))
        end_at = _parse_iso(term.get("end_at"))

        if start_at and now < start_at:
            continue  # term hasn't started yet
        if end_at and now > end_at:
            continue  # term already ended

        current_term_courses.append(course)

    return current_term_courses


def get_assignments(course_id: int) -> list[dict]:
    """All assignments for a course, including those without a due date."""
    return _get_all_pages(f"/api/v1/courses/{course_id}/assignments")


def get_calendar_events(course_ids: list[int], days_back: int = 14, days_forward: int = 365) -> list[dict]:
    """
    Calendar events (not assignments - those come from get_assignments) for
    the given courses, within a date window.
    """
    if not course_ids:
        return []

    context_codes = [f"course_{cid}" for cid in course_ids]
    now = datetime.now(timezone.utc)
    return _get_all_pages(
        "/api/v1/calendar_events",
        params={
            "type": "event",
            "context_codes[]": context_codes,
            "start_date": (now - timedelta(days=days_back)).strftime("%Y-%m-%d"),
            "end_date": (now + timedelta(days=days_forward)).strftime("%Y-%m-%d"),
        },
    )


def get_announcements(course_ids: list[int], lookback_days: int) -> list[dict]:
    """Announcements posted in the last `lookback_days` days, across courses."""
    if not course_ids:
        return []

    context_codes = [f"course_{cid}" for cid in course_ids]
    start_date = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    return _get_all_pages(
        "/api/v1/announcements",
        params={
            "context_codes[]": context_codes,
            "start_date": start_date.strftime("%Y-%m-%d"),
        },
    )


def get_modules(course_id: int) -> list[dict]:
    """
    All modules in a course. Some instructors never fill in Canvas's
    official Syllabus tool and instead upload a PDF or write a Page inside
    a Module - get_module_items()/get_module_item_content() below are how
    we find and read that content.
    """
    try:
        return _get_all_pages(f"/api/v1/courses/{course_id}/modules")
    except CanvasError:
        return []  # Modules isn't enabled for this course - nothing to scan


def get_module_items(course_id: int, module_id: int) -> list[dict]:
    try:
        return _get_all_pages(f"/api/v1/courses/{course_id}/modules/{module_id}/items")
    except CanvasError:
        return []


def get_module_item_content(item: dict) -> dict | None:
    """
    Resolve a module item to its actual content. A module item's own "url"
    field (different from the module-items-LIST endpoint) points straight
    at either a Canvas File object (has "content-type" and a download
    "url") or a Page object (has a "body" HTML field). Returns None for
    item types with no useful fetchable text (SubHeader, ExternalUrl,
    Assignment, Quiz, Discussion, ...) - those are either not text content,
    or already covered by get_assignments().
    """
    if item.get("type") not in ("File", "Page"):
        return None
    url = item.get("url")
    if not url:
        return None
    response = _session.get(url)
    if response.status_code != 200:
        return None
    return response.json()


def download_file_bytes(download_url: str) -> bytes:
    """
    Download a Canvas file's actual bytes. Deliberately uses a plain,
    unauthenticated request rather than our Canvas session/token: these
    download URLs are already pre-signed (they work without auth), and
    they sometimes redirect to a non-Canvas storage domain - we don't want
    to send our Canvas bearer token anywhere outside Canvas itself.
    """
    response = requests.get(download_url, timeout=30)
    response.raise_for_status()
    return response.content


def get_section_course_id(section_id: int) -> int | None:
    """
    Resolve a course *section* ID to its parent course ID. Needed because
    some calendar events/announcements are scoped to a section (context
    code "course_section_<id>") rather than the whole course
    ("course_<id>") - e.g. a specific lecture section's midterm.
    """
    pages = _get_all_pages(f"/api/v1/sections/{section_id}")
    section = pages[0] if pages else {}
    return section.get("course_id")


def get_syllabus_body(course_id: int) -> str | None:
    """The course syllabus as raw HTML, or None if it's empty."""
    pages = _get_all_pages(
        f"/api/v1/courses/{course_id}",
        params={"include[]": "syllabus_body"},
    )
    course = pages[0] if pages else {}
    return course.get("syllabus_body") or None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    # Canvas timestamps look like "2026-09-01T00:00:00Z"
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

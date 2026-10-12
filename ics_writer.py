"""
ics_writer.py
-------------
Turns a list of events.Event objects into a valid .ics (iCalendar) file.

Each event gets the SAME icalendar UID every time it's written, generated
from events.Event.uid (course + type + normalized title). That's what makes
re-running this script update an existing calendar entry when a date
changes, instead of creating a duplicate - Apple Calendar (and any other
calendar app) matches on UID.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from icalendar import Calendar
from icalendar import Event as ICalEvent

import config
from events import Event

TORONTO = ZoneInfo(config.TIMEZONE_NAME)

# How long a timed event lasts when only a start time is given (no end time).
DEFAULT_EVENT_DURATION = timedelta(hours=1)


def build_calendar(events: list[Event]) -> Calendar:
    cal = Calendar()
    cal.add("prodid", "-//canvas-deadlines//Phase 1//EN")
    cal.add("version", "2.0")

    now_utc = datetime.now(timezone.utc)

    for event in events:
        ical_event = ICalEvent()
        ical_event.add("uid", event.uid)
        ical_event.add("summary", f"{event.course_code}: {event.title}")
        ical_event.add("dtstamp", now_utc)

        if event.all_day:
            # A bare `date` object tells icalendar to write an all-day
            # (VALUE=DATE) event, with no timezone involved.
            ical_event.add("dtstart", event.event_date)
            ical_event.add("dtend", event.event_date + timedelta(days=1))
        else:
            start_dt = datetime.combine(event.event_date, event.start_time, tzinfo=TORONTO)
            if event.end_time:
                end_dt = datetime.combine(event.event_date, event.end_time, tzinfo=TORONTO)
            else:
                end_dt = start_dt + DEFAULT_EVENT_DURATION
            ical_event.add("dtstart", start_dt)
            ical_event.add("dtend", end_dt)

        if event.location:
            ical_event.add("location", event.location)
        if event.source_note:
            ical_event.add("description", event.source_note)

        cal.add_component(ical_event)

    return cal


def write_ics(events: list[Event], output_path=None) -> None:
    output_path = output_path or config.OUTPUT_FILE
    output_path.parent.mkdir(parents=True, exist_ok=True)
    calendar = build_calendar(events)
    output_path.write_bytes(calendar.to_ical())

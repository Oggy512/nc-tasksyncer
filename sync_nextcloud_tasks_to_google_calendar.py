#!/usr/bin/env python3
"""
Project: Nextcloud Task to Google Calendar Sync
Version: 0.1.2
Synopsis:
    One-way synchronizes VTODO tasks from one Nextcloud CalDAV task list into
    one shared Google Calendar as normal calendar events.
Description:
    Nextcloud remains the authoritative task backend. Google Calendar is used
    only as a shared visibility and reminder layer. The script creates,
    updates, or deletes Google Calendar events based on the current VTODO state.
    Mapping is stored in Google Calendar extendedProperties.private, so no
    separate database is required.

Supported behavior in version 0.1.2:
    - Reads one specific Nextcloud CalDAV task list via a WebDAV REPORT request.
    - Parses VTODO entries.
    - Creates Google Calendar events for tasks with due dates.
    - Preserves VTODO DUE/DTSTART time in Google Calendar events.
    - Moves overdue open tasks to today while preserving the original due time.
    - Syncs RFC5545 task status into the event title and description.
    - Syncs RFC5545 task priority into the event title and description.
    - Expands recurring VTODOs (RRULE/RDATE/EXDATE) into individual Google events
      within a configurable sync horizon.
    - Deletes or marks events when tasks are completed or cancelled.
    - Deletes events when tasks disappear from the Nextcloud task list.

Known limitations:
    - One-way sync only: Nextcloud -> Google Calendar.
    - Recurring VTODOs are expanded into individual events, not Google recurring
      event series. This is intentional because status/overdue handling is more
      reliable for task semantics.
    - Calendar-side manual changes are overwritten.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from typing import Dict, Iterable, List, Optional, Set, Tuple
from xml.etree import ElementTree as ET

import requests
from dateutil import rrule, tz
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from icalendar import Calendar

APP_NAME = "nextcloud-task-sync"
APP_SOURCE = "nextcloud-task-google-calendar-sync"
APP_VERSION = "0.1.2"

NS = {
    "d": "DAV:",
    "cal": "urn:ietf:params:xml:ns:caldav",
}

STATUS_DEFINITIONS = {
    "NEEDS-ACTION": ("☐", "Handlungsbedarf"),
    "IN-PROCESS": ("▶", "In Bearbeitung"),
    "COMPLETED": ("✓", "Fertiggestellt"),
    "CANCELLED": ("✕", "Abgebrochen"),
}


@dataclass(frozen=True)
class Config:
    nc_caldav_url: str
    nc_username: str
    nc_password: str
    google_service_account_file: str
    google_calendar_id: str
    timezone_name: str
    sync_interval_seconds: int
    default_event_time: str
    event_duration_minutes: int
    reminder_minutes: int
    ignore_undated_tasks: bool
    overdue_mode: str
    completed_mode: str
    cancelled_mode: str
    dry_run: bool
    title_prefix_overdue: str
    status_title_mode: str
    priority_title_mode: str
    priority_high_prefix: str
    priority_medium_prefix: str
    priority_low_prefix: str
    priority_none_prefix: str
    recurrence_expand_days: int
    recurrence_lookback_days: int
    recurrence_include_latest_overdue: bool


@dataclass(frozen=True)
class TaskItem:
    uid: str
    summary: str
    description: str
    due_datetime: Optional[datetime]
    due_has_time: bool
    dtstart_datetime: Optional[datetime]
    dtstart_has_time: bool
    status: str
    percent_complete: int
    completed: bool
    cancelled: bool
    priority: int
    last_modified: Optional[str]
    url: Optional[str]
    raw_etag: Optional[str]
    rrule_text: Optional[str]
    recurrence_id: Optional[datetime]
    exdates: Tuple[datetime, ...]
    rdates: Tuple[datetime, ...]


@dataclass(frozen=True)
class EventPlan:
    source_key: str
    task_uid: str
    summary: str
    description: str
    start: datetime
    end: datetime
    task_completed: bool
    task_cancelled: bool
    recurrence_instance: Optional[str]


def getenv_required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Required environment variable is missing: {name}")
    return value


def getenv_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_config() -> Config:
    return Config(
        nc_caldav_url=getenv_required("NC_CALDAV_URL").rstrip("/") + "/",
        nc_username=getenv_required("NC_USERNAME"),
        nc_password=getenv_required("NC_PASSWORD"),
        google_service_account_file=getenv_required("GOOGLE_SERVICE_ACCOUNT_FILE"),
        google_calendar_id=getenv_required("GOOGLE_CALENDAR_ID"),
        timezone_name=os.getenv("TZ", "Europe/Berlin"),
        sync_interval_seconds=int(os.getenv("SYNC_INTERVAL_SECONDS", "600")),
        default_event_time=os.getenv("DEFAULT_EVENT_TIME", "08:00"),
        event_duration_minutes=int(os.getenv("EVENT_DURATION_MINUTES", "15")),
        reminder_minutes=int(os.getenv("REMINDER_MINUTES", "0")),
        ignore_undated_tasks=getenv_bool("IGNORE_UNDATED_TASKS", True),
        overdue_mode=os.getenv("OVERDUE_MODE", "move_to_today"),
        completed_mode=os.getenv("COMPLETED_MODE", "mark_done"),
        cancelled_mode=os.getenv("CANCELLED_MODE", "mark_cancelled"),
        dry_run=getenv_bool("DRY_RUN", False),
        title_prefix_overdue=os.getenv("TASK_TITLE_PREFIX_OVERDUE", "⚠"),
        status_title_mode=os.getenv("STATUS_TITLE_MODE", "emoji"),
        priority_title_mode=os.getenv("PRIORITY_TITLE_MODE", "emoji"),
        priority_high_prefix=os.getenv("PRIORITY_HIGH_PREFIX", "🔴"),
        priority_medium_prefix=os.getenv("PRIORITY_MEDIUM_PREFIX", "🟠"),
        priority_low_prefix=os.getenv("PRIORITY_LOW_PREFIX", "🔵"),
        priority_none_prefix=os.getenv("PRIORITY_NONE_PREFIX", ""),
        recurrence_expand_days=int(os.getenv("RECURRENCE_EXPAND_DAYS", "180")),
        recurrence_lookback_days=int(os.getenv("RECURRENCE_LOOKBACK_DAYS", "30")),
        recurrence_include_latest_overdue=getenv_bool("RECURRENCE_INCLUDE_LATEST_OVERDUE", True),
    )


def setup_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )


def build_google_calendar_service(config: Config):
    scopes = ["https://www.googleapis.com/auth/calendar.events"]
    credentials = service_account.Credentials.from_service_account_file(
        config.google_service_account_file,
        scopes=scopes,
    )
    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


def fetch_nextcloud_vtodos(config: Config) -> Iterable[Tuple[Optional[str], str]]:
    report_body = """<?xml version="1.0" encoding="utf-8" ?>
<cal:calendar-query xmlns:d="DAV:" xmlns:cal="urn:ietf:params:xml:ns:caldav">
  <d:prop>
    <d:getetag />
    <cal:calendar-data />
  </d:prop>
  <cal:filter>
    <cal:comp-filter name="VCALENDAR">
      <cal:comp-filter name="VTODO" />
    </cal:comp-filter>
  </cal:filter>
</cal:calendar-query>
"""
    response = requests.request(
        "REPORT",
        config.nc_caldav_url,
        data=report_body.encode("utf-8"),
        headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
        auth=(config.nc_username, config.nc_password),
        timeout=60,
    )
    if response.status_code not in {207, 200}:
        raise RuntimeError(
            f"Nextcloud CalDAV REPORT failed: HTTP {response.status_code} {response.text[:500]}"
        )

    root = ET.fromstring(response.content)
    for resp in root.findall("d:response", NS):
        etag_el = resp.find(".//d:getetag", NS)
        cal_el = resp.find(".//cal:calendar-data", NS)
        if cal_el is None or not cal_el.text:
            continue
        yield (etag_el.text if etag_el is not None else None, cal_el.text)


def prop_to_str(value) -> str:
    if value is None:
        return ""
    return str(value)


def normalize_datetime(dt_value, local_tz) -> datetime:
    if isinstance(dt_value, datetime):
        if dt_value.tzinfo is None:
            return dt_value.replace(tzinfo=local_tz)
        return dt_value.astimezone(local_tz)
    if isinstance(dt_value, date):
        return datetime.combine(dt_value, dtime.min, tzinfo=local_tz)
    raise ValueError(f"Unsupported datetime value: {dt_value!r}")


def prop_to_datetime(value, local_tz) -> Tuple[Optional[datetime], bool]:
    if value is None:
        return None, False
    dt_value = getattr(value, "dt", value)
    if isinstance(dt_value, datetime):
        return normalize_datetime(dt_value, local_tz), True
    if isinstance(dt_value, date):
        return normalize_datetime(dt_value, local_tz), False
    return None, False


def prop_to_int(value, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_default_time(value: str) -> dtime:
    try:
        hour, minute = value.split(":", 1)
        return dtime(hour=int(hour), minute=int(minute))
    except Exception as exc:
        raise RuntimeError(f"Invalid DEFAULT_EVENT_TIME '{value}'. Expected HH:MM.") from exc


def vdatetime_tuple(prop, local_tz) -> Tuple[datetime, ...]:
    if prop is None:
        return tuple()
    props = prop if isinstance(prop, list) else [prop]
    values: List[datetime] = []
    for item in props:
        if hasattr(item, "dts"):
            for entry in item.dts:
                try:
                    values.append(normalize_datetime(entry.dt, local_tz))
                except Exception:
                    continue
        else:
            try:
                values.append(normalize_datetime(getattr(item, "dt", item), local_tz))
            except Exception:
                continue
    return tuple(values)


def rrule_to_text(value) -> Optional[str]:
    if value is None:
        return None
    try:
        raw = value.to_ical()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return raw
    except Exception:
        return str(value)


def parse_tasks(vtodo_payloads: Iterable[Tuple[Optional[str], str]], local_tz) -> Dict[str, TaskItem]:
    tasks: Dict[str, TaskItem] = {}
    for etag, payload in vtodo_payloads:
        try:
            calendar = Calendar.from_ical(payload)
        except Exception as exc:
            logging.warning("Skipping invalid iCalendar payload: %s", exc)
            continue

        for component in calendar.walk("VTODO"):
            uid = prop_to_str(component.get("UID")).strip()
            if not uid:
                logging.warning("Skipping VTODO without UID")
                continue

            status = prop_to_str(component.get("STATUS")).upper() or "NEEDS-ACTION"
            percent_complete = prop_to_int(component.get("PERCENT-COMPLETE"), 0)
            completed = bool(component.get("COMPLETED")) or status == "COMPLETED" or percent_complete >= 100
            cancelled = status == "CANCELLED"
            due_datetime, due_has_time = prop_to_datetime(component.get("DUE"), local_tz)
            dtstart_datetime, dtstart_has_time = prop_to_datetime(component.get("DTSTART"), local_tz)
            recurrence_id, _ = prop_to_datetime(component.get("RECURRENCE-ID"), local_tz)

            key = uid
            if recurrence_id is not None:
                key = f"{uid}::{recurrence_id.isoformat()}"

            task = TaskItem(
                uid=uid,
                summary=prop_to_str(component.get("SUMMARY")).strip() or "Untitled task",
                description=prop_to_str(component.get("DESCRIPTION")).strip(),
                due_datetime=due_datetime,
                due_has_time=due_has_time,
                dtstart_datetime=dtstart_datetime,
                dtstart_has_time=dtstart_has_time,
                status=status,
                percent_complete=percent_complete,
                completed=completed,
                cancelled=cancelled,
                priority=prop_to_int(component.get("PRIORITY"), 0),
                last_modified=prop_to_str(component.get("LAST-MODIFIED")) or None,
                url=prop_to_str(component.get("URL")) or None,
                raw_etag=etag,
                rrule_text=rrule_to_text(component.get("RRULE")),
                recurrence_id=recurrence_id,
                exdates=vdatetime_tuple(component.get("EXDATE"), local_tz),
                rdates=vdatetime_tuple(component.get("RDATE"), local_tz),
            )
            tasks[key] = task
    return tasks


def status_icon(task: TaskItem, config: Config) -> str:
    icon, label = STATUS_DEFINITIONS.get(task.status, ("?", task.status or "Unknown"))
    if config.status_title_mode == "none":
        return ""
    if config.status_title_mode == "label":
        return f"[{label}]"
    return icon


def priority_bucket(priority: int) -> Tuple[str, str]:
    if priority <= 0:
        return "none", "Keine Priorität"
    if 1 <= priority <= 3:
        return "high", "Hoch"
    if 4 <= priority <= 6:
        return "medium", "Mittel"
    if 7 <= priority <= 9:
        return "low", "Niedrig"
    return "none", "Keine Priorität"


def priority_prefix(task: TaskItem, config: Config) -> str:
    bucket, label = priority_bucket(task.priority)
    if config.priority_title_mode == "none":
        return ""
    if config.priority_title_mode == "label":
        return {"high": "[P1]", "medium": "[P2]", "low": "[P3]", "none": ""}.get(bucket, "")
    return {
        "high": config.priority_high_prefix,
        "medium": config.priority_medium_prefix,
        "low": config.priority_low_prefix,
        "none": config.priority_none_prefix,
    }.get(bucket, "")


def build_summary(task: TaskItem, overdue: bool, config: Config) -> str:
    parts = []
    if overdue:
        parts.append(config.title_prefix_overdue)
    for token in (status_icon(task, config), priority_prefix(task, config)):
        if token:
            parts.append(token)
    parts.append(task.summary)
    return " ".join(parts)


def effective_due_datetime(task: TaskItem, fallback_date: date, fallback_time: dtime, local_tz) -> Tuple[Optional[datetime], bool]:
    if task.due_datetime is not None:
        return task.due_datetime, task.due_has_time
    if task.dtstart_datetime is not None:
        return task.dtstart_datetime, task.dtstart_has_time
    if fallback_date is not None:
        return datetime.combine(fallback_date, fallback_time, tzinfo=local_tz), False
    return None, False


def event_time_from_task(task: TaskItem, original_due_dt: datetime, fallback_time: dtime) -> dtime:
    if task.due_has_time or task.dtstart_has_time:
        return original_due_dt.timetz().replace(tzinfo=None)
    return fallback_time


def build_single_event_plan(
    task: TaskItem,
    config: Config,
    local_tz,
    source_key: str,
    original_due_dt: datetime,
    original_due_has_time: bool,
    recurrence_instance: Optional[str] = None,
) -> Optional[EventPlan]:
    today = datetime.now(local_tz).date()
    fallback_time = parse_default_time(config.default_event_time)
    event_time = original_due_dt.timetz().replace(tzinfo=None) if original_due_has_time else fallback_time
    original_due_date = original_due_dt.date()
    event_date = original_due_date
    overdue = (not task.completed) and (not task.cancelled) and original_due_date < today

    if overdue and config.overdue_mode == "move_to_today":
        event_date = today

    start = datetime.combine(event_date, event_time, tzinfo=local_tz)
    end = start + timedelta(minutes=config.event_duration_minutes)
    summary = build_summary(task, overdue, config)
    if overdue:
        summary = f"{summary} (fällig seit {original_due_date.isoformat()})"

    status_label = STATUS_DEFINITIONS.get(task.status, ("?", task.status))[1]
    priority_bucket_name, priority_label = priority_bucket(task.priority)
    lines = [
        "Source: Nextcloud Tasks",
        f"Sync application: {APP_SOURCE} {APP_VERSION}",
        f"Nextcloud UID: {task.uid}",
        f"Source key: {source_key}",
        f"Original due date: {original_due_date.isoformat()}",
        f"Original due time: {event_time.strftime('%H:%M') if original_due_has_time else 'not set; default used'}",
        f"Task status: {status_label} ({task.status})",
        f"Task priority: {priority_label} ({task.priority}; bucket={priority_bucket_name})",
    ]
    if recurrence_instance:
        lines.append(f"Recurrence instance: {recurrence_instance}")
    if task.rrule_text:
        lines.append(f"Recurrence rule: {task.rrule_text}")
    if task.last_modified:
        lines.append(f"Last modified: {task.last_modified}")
    if task.url:
        lines.append(f"Task URL: {task.url}")
    if task.description:
        lines.extend(["", "Description:", task.description])

    return EventPlan(
        source_key=source_key,
        task_uid=task.uid,
        summary=summary,
        description="\n".join(lines),
        start=start,
        end=end,
        task_completed=task.completed,
        task_cancelled=task.cancelled,
        recurrence_instance=recurrence_instance,
    )


def parse_rrule_set(task: TaskItem, base_dt: datetime, local_tz):
    ruleset = rrule.rruleset()
    if task.rrule_text:
        ruleset.rrule(rrule.rrulestr(task.rrule_text, dtstart=base_dt))
    for rdate in task.rdates:
        ruleset.rdate(rdate.astimezone(local_tz))
    for exdate in task.exdates:
        ruleset.exdate(exdate.astimezone(local_tz))
    return ruleset


def build_event_plans(task: TaskItem, config: Config, local_tz) -> List[EventPlan]:
    today = datetime.now(local_tz).date()
    fallback_time = parse_default_time(config.default_event_time)
    base_dt, base_has_time = effective_due_datetime(task, today, fallback_time, local_tz)
    if base_dt is None:
        return []
    if task.due_datetime is None and task.dtstart_datetime is None and config.ignore_undated_tasks:
        return []

    if not task.rrule_text:
        source_key = task.uid if task.recurrence_id is None else f"{task.uid}::{task.recurrence_id.isoformat()}"
        plan = build_single_event_plan(task, config, local_tz, source_key, base_dt, base_has_time, task.recurrence_id.isoformat() if task.recurrence_id else None)
        return [plan] if plan else []

    # Recurring VTODOs are expanded into a bounded occurrence window.
    plans: List[EventPlan] = []
    ruleset = parse_rrule_set(task, base_dt, local_tz)
    window_start = datetime.combine(today, dtime.min, tzinfo=local_tz)
    window_end = datetime.combine(today + timedelta(days=config.recurrence_expand_days), dtime.max, tzinfo=local_tz)

    occurrences: List[datetime] = list(ruleset.between(window_start, window_end, inc=True))

    if config.recurrence_include_latest_overdue:
        lookback_start = datetime.combine(today - timedelta(days=config.recurrence_lookback_days), dtime.min, tzinfo=local_tz)
        latest_past = list(ruleset.between(lookback_start, window_start - timedelta(seconds=1), inc=True))
        if latest_past:
            occurrences.insert(0, latest_past[-1])

    seen: Set[str] = set()
    for occ in occurrences:
        occ_local = occ.astimezone(local_tz) if occ.tzinfo else occ.replace(tzinfo=local_tz)
        occurrence_key = occ_local.isoformat()
        if occurrence_key in seen:
            continue
        seen.add(occurrence_key)
        source_key = f"{task.uid}::{occurrence_key}"
        plan = build_single_event_plan(
            task=task,
            config=config,
            local_tz=local_tz,
            source_key=source_key,
            original_due_dt=occ_local,
            original_due_has_time=base_has_time,
            recurrence_instance=occurrence_key,
        )
        if plan:
            plans.append(plan)
    return plans


def event_body_from_plan(plan: EventPlan, config: Config) -> dict:
    return {
        "summary": plan.summary,
        "description": plan.description,
        "start": {"dateTime": plan.start.isoformat()},
        "end": {"dateTime": plan.end.isoformat()},
        "extendedProperties": {
            "private": {
                "source": APP_SOURCE,
                "sourceKey": plan.source_key,
                "nextcloudTaskUid": plan.task_uid,
                "recurrenceInstance": plan.recurrence_instance or "",
                "managedBy": APP_NAME,
            }
        },
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": config.reminder_minutes},
            ],
        },
    }


def list_managed_google_events(service, config: Config) -> Dict[str, dict]:
    events_by_key: Dict[str, dict] = {}
    page_token = None
    while True:
        result = (
            service.events()
            .list(
                calendarId=config.google_calendar_id,
                privateExtendedProperty=f"source={APP_SOURCE}",
                showDeleted=False,
                maxResults=2500,
                pageToken=page_token,
            )
            .execute()
        )
        for event in result.get("items", []):
            private_props = event.get("extendedProperties", {}).get("private", {})
            key = private_props.get("sourceKey") or private_props.get("nextcloudTaskUid")
            if key:
                events_by_key[key] = event
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return events_by_key


def equivalent_event(existing: dict, desired: dict) -> bool:
    keys = ["summary", "description", "start", "end", "reminders"]
    for key in keys:
        if existing.get(key) != desired.get(key):
            return False
    existing_private = existing.get("extendedProperties", {}).get("private", {})
    desired_private = desired.get("extendedProperties", {}).get("private", {})
    for key, value in desired_private.items():
        if existing_private.get(key) != value:
            return False
    return True


def create_event(service, config: Config, body: dict) -> None:
    if config.dry_run:
        logging.info("DRY RUN: would create event: %s", body["summary"])
        return
    created = service.events().insert(calendarId=config.google_calendar_id, body=body).execute()
    logging.info("Created Google Calendar event: %s", created.get("id"))


def patch_event(service, config: Config, event_id: str, body: dict) -> None:
    if config.dry_run:
        logging.info("DRY RUN: would patch event %s: %s", event_id, body["summary"])
        return
    service.events().patch(calendarId=config.google_calendar_id, eventId=event_id, body=body).execute()
    logging.info("Updated Google Calendar event: %s", event_id)


def delete_event(service, config: Config, event_id: str) -> None:
    if config.dry_run:
        logging.info("DRY RUN: would delete event: %s", event_id)
        return
    try:
        service.events().delete(calendarId=config.google_calendar_id, eventId=event_id).execute()
        logging.info("Deleted Google Calendar event: %s", event_id)
    except HttpError as exc:
        if exc.resp.status == 410:
            logging.info("Event already deleted: %s", event_id)
            return
        raise


def should_delete_for_status(task: TaskItem, config: Config) -> bool:
    if task.completed and config.completed_mode == "delete_event":
        return True
    if task.cancelled and config.cancelled_mode == "delete_event":
        return True
    return False


def sync_once(config: Config, service) -> None:
    local_tz = tz.gettz(config.timezone_name)
    if local_tz is None:
        raise RuntimeError(f"Invalid timezone: {config.timezone_name}")

    logging.info("Fetching Nextcloud VTODOs from %s", config.nc_caldav_url)
    tasks = parse_tasks(fetch_nextcloud_vtodos(config), local_tz)
    logging.info("Fetched %d Nextcloud task component(s)", len(tasks))

    existing_events = list_managed_google_events(service, config)
    logging.info("Fetched %d managed Google Calendar event(s)", len(existing_events))

    planned_keys: Set[str] = set()

    for _, task in tasks.items():
        plans = build_event_plans(task, config, local_tz)
        if not plans:
            # Remove any old non-recurring event for the task if it became undated.
            old = existing_events.get(task.uid)
            if old:
                logging.info("Task has no plannable due date; deleting existing event for UID %s", task.uid)
                delete_event(service, config, old["id"])
            continue

        for plan in plans:
            existing = existing_events.get(plan.source_key)

            if should_delete_for_status(task, config):
                if existing:
                    delete_event(service, config, existing["id"])
                continue

            planned_keys.add(plan.source_key)
            body = event_body_from_plan(plan, config)

            if existing is None:
                create_event(service, config, body)
            elif not equivalent_event(existing, body):
                patch_event(service, config, existing["id"], body)
            else:
                logging.debug("Event already up to date for source key %s", plan.source_key)

    for key, event in existing_events.items():
        if key not in planned_keys:
            delete_event(service, config, event["id"])

    logging.info("Sync run completed")


def main() -> int:
    setup_logging()
    config = load_config()
    logging.info("Starting %s version %s", APP_SOURCE, APP_VERSION)

    if config.dry_run:
        logging.warning("DRY_RUN is enabled; no Google Calendar changes will be written")

    service = build_google_calendar_service(config)

    while True:
        try:
            sync_once(config, service)
        except Exception as exc:
            logging.exception("Sync run failed: %s", exc)

        if config.sync_interval_seconds <= 0:
            break
        time.sleep(config.sync_interval_seconds)

    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Project: Nextcloud Task to Google Calendar Sync
Version: 0.2.1
Synopsis:
    One-way synchronizes VTODO tasks from one shared and up to two optional
    personal Nextcloud CalDAV task lists into corresponding Google Calendars.
Description:
    Nextcloud remains the authoritative task backend. Google Calendar is used
    only as a shared visibility and reminder layer. The script creates,
    updates, or deletes Google Calendar events based on the current VTODO state.
    Mapping is stored in Google Calendar extendedProperties.private, so no
    separate database is required.

Supported behavior in version 0.2.1:
    - Reads one shared and up to two optional personal Nextcloud CalDAV task lists.
    - Uses an independent Nextcloud URL/login and Google Calendar per sync target.
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
import re
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
APP_VERSION = "0.2.1"

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
class SyncTarget:
    target_id: str
    display_name: str
    nc_caldav_url: str
    nc_username: str
    nc_password: str
    google_calendar_id: str


@dataclass(frozen=True)
class Config:
    targets: Tuple[SyncTarget, ...]
    google_service_account_file: str
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
    target_id: str
    target_name: str
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


def optional_value(name: str) -> Optional[str]:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def load_sync_target(
    target_id: str,
    display_name: str,
    prefix: str,
    *,
    enabled_default: bool = False,
    legacy_shared: bool = False,
) -> Optional[SyncTarget]:
    enabled_name = f"{prefix}_ENABLED"
    enabled = getenv_bool(enabled_name, enabled_default)

    if legacy_shared:
        caldav_url = optional_value(f"{prefix}_NC_CALDAV_URL") or optional_value("NC_CALDAV_URL")
        username = optional_value(f"{prefix}_NC_USERNAME") or optional_value("NC_USERNAME")
        password = optional_value(f"{prefix}_NC_PASSWORD") or optional_value("NC_PASSWORD")
        calendar_id = optional_value(f"{prefix}_GOOGLE_CALENDAR_ID") or optional_value("GOOGLE_CALENDAR_ID")
    else:
        caldav_url = optional_value(f"{prefix}_NC_CALDAV_URL")
        username = optional_value(f"{prefix}_NC_USERNAME")
        password = optional_value(f"{prefix}_NC_PASSWORD")
        calendar_id = optional_value(f"{prefix}_GOOGLE_CALENDAR_ID")

    configured_values = [caldav_url, username, password, calendar_id]
    if not enabled and not any(configured_values):
        return None
    if not enabled and any(configured_values):
        logging.warning("%s has values configured but %s=false; target is disabled", display_name, enabled_name)
        return None

    missing = []
    if not caldav_url:
        missing.append(f"{prefix}_NC_CALDAV_URL")
    if not username:
        missing.append(f"{prefix}_NC_USERNAME")
    if not password:
        missing.append(f"{prefix}_NC_PASSWORD")
    if not calendar_id:
        missing.append(f"{prefix}_GOOGLE_CALENDAR_ID")
    if missing:
        raise RuntimeError(f"Enabled sync target '{target_id}' is incomplete. Missing: {', '.join(missing)}")

    return SyncTarget(
        target_id=target_id,
        display_name=os.getenv(f"{prefix}_NAME", display_name),
        nc_caldav_url=caldav_url.rstrip("/") + "/",
        nc_username=username,
        nc_password=password,
        google_calendar_id=calendar_id,
    )


def load_config() -> Config:
    targets: List[SyncTarget] = []

    shared = load_sync_target(
        target_id="shared",
        display_name="Shared tasks",
        prefix="SHARED",
        enabled_default=True,
        legacy_shared=True,
    )
    if shared:
        targets.append(shared)

    for target_id, display_name, prefix in (
        ("personal1", "Personal tasks 1", "PERSONAL1"),
        ("personal2", "Personal tasks 2", "PERSONAL2"),
    ):
        target = load_sync_target(target_id, display_name, prefix, enabled_default=False)
        if target:
            targets.append(target)

    if not targets:
        raise RuntimeError("No sync target is enabled and fully configured")

    return Config(
        targets=tuple(targets),
        google_service_account_file=getenv_required("GOOGLE_SERVICE_ACCOUNT_FILE"),
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


def fetch_nextcloud_vtodos(target: SyncTarget) -> Iterable[Tuple[Optional[str], str]]:
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
        target.nc_caldav_url,
        data=report_body.encode("utf-8"),
        headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
        auth=(target.nc_username, target.nc_password),
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
    target: SyncTarget,
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
        f"Sync target: {target.display_name} ({target.target_id})",
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
        target_id=target.target_id,
        target_name=target.display_name,
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


def build_event_plans(task: TaskItem, config: Config, target: SyncTarget, local_tz) -> List[EventPlan]:
    today = datetime.now(local_tz).date()
    fallback_time = parse_default_time(config.default_event_time)
    base_dt, base_has_time = effective_due_datetime(task, today, fallback_time, local_tz)
    if base_dt is None:
        return []
    if task.due_datetime is None and task.dtstart_datetime is None and config.ignore_undated_tasks:
        return []

    if not task.rrule_text:
        source_key = task.uid if task.recurrence_id is None else f"{task.uid}::{task.recurrence_id.isoformat()}"
        plan = build_single_event_plan(task, config, target, local_tz, source_key, base_dt, base_has_time, task.recurrence_id.isoformat() if task.recurrence_id else None)
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
            target=target,
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
    private_properties = {
        "source": APP_SOURCE,
        "sourceKey": plan.source_key,
        "nextcloudTaskUid": plan.task_uid,
        "syncTarget": plan.target_id,
        "syncTargetName": plan.target_name,
        "managedBy": APP_NAME,
    }
    # Google Calendar may omit empty extended properties from API responses.
    # Do not send empty values because they would otherwise cause endless PATCH loops.
    if plan.recurrence_instance:
        private_properties["recurrenceInstance"] = plan.recurrence_instance

    return {
        "summary": plan.summary,
        "description": plan.description,
        "start": {"dateTime": plan.start.isoformat()},
        "end": {"dateTime": plan.end.isoformat()},
        "extendedProperties": {"private": private_properties},
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": config.reminder_minutes},
            ],
        },
    }


def list_managed_google_events(service, target: SyncTarget) -> Dict[str, dict]:
    events_by_key: Dict[str, dict] = {}
    page_token = None
    while True:
        result = (
            service.events()
            .list(
                calendarId=target.google_calendar_id,
                privateExtendedProperty=f"source={APP_SOURCE}",
                showDeleted=False,
                maxResults=2500,
                pageToken=page_token,
            )
            .execute()
        )
        for event in result.get("items", []):
            private_props = event.get("extendedProperties", {}).get("private", {})
            event_target = private_props.get("syncTarget")
            # Adopt legacy v0.1.x events without syncTarget only for the shared target.
            if event_target not in {target.target_id, None, ""}:
                continue
            if not event_target and target.target_id != "shared":
                continue
            key = private_props.get("sourceKey") or private_props.get("nextcloudTaskUid")
            if key:
                events_by_key[key] = event
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return events_by_key


def normalize_event_datetime(value: Optional[str]) -> Optional[str]:
    """Normalize RFC3339 timestamps to an absolute UTC instant for comparison."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.isoformat(timespec="seconds")
        return parsed.astimezone(tz.UTC).isoformat(timespec="seconds")
    except ValueError:
        # Preserve an unparseable value so a real mismatch still triggers an update.
        return value


def normalize_event_side(value: dict) -> dict:
    """Keep only semantic fields managed by this application."""
    if not value:
        return {}
    if value.get("dateTime"):
        return {"dateTime": normalize_event_datetime(value.get("dateTime"))}
    if value.get("date"):
        return {"date": value.get("date")}
    return {}


def normalize_managed_description(value: str) -> str:
    """Ignore the application version in the generated audit line during comparison.

    A software update alone must not rewrite every calendar event and trigger
    change notifications. The version is refreshed naturally when another
    semantic task field changes.
    """
    if not value:
        return ""
    return re.sub(
        rf"(?m)^Sync application: {re.escape(APP_SOURCE)}(?:\s+\S+)?$",
        f"Sync application: {APP_SOURCE}",
        value,
    )


def normalize_reminders(value: dict) -> dict:
    if not value:
        return {"useDefault": True, "overrides": []}
    overrides = []
    for item in value.get("overrides", []) or []:
        overrides.append(
            {
                "method": str(item.get("method", "")),
                "minutes": int(item.get("minutes", 0)),
            }
        )
    overrides.sort(key=lambda item: (item["method"], item["minutes"]))
    return {
        "useDefault": bool(value.get("useDefault", False)),
        "overrides": overrides,
    }


def comparable_event(value: dict, desired_private_keys: Optional[Set[str]] = None) -> dict:
    private = value.get("extendedProperties", {}).get("private", {}) or {}
    if desired_private_keys is not None:
        private = {key: private.get(key) for key in desired_private_keys}
    # Treat absent and empty private values as equivalent. Google may discard empty values.
    private = {key: str(val) for key, val in private.items() if val not in {None, ""}}

    return {
        "summary": value.get("summary", ""),
        "description": normalize_managed_description(value.get("description", "")),
        "start": normalize_event_side(value.get("start", {})),
        "end": normalize_event_side(value.get("end", {})),
        "reminders": normalize_reminders(value.get("reminders", {})),
        "private": private,
    }


def event_differences(existing: dict, desired: dict) -> List[str]:
    desired_private = desired.get("extendedProperties", {}).get("private", {}) or {}
    desired_private_keys = set(desired_private.keys())
    normalized_existing = comparable_event(existing, desired_private_keys)
    normalized_desired = comparable_event(desired, desired_private_keys)
    return [
        key
        for key in normalized_desired
        if normalized_existing.get(key) != normalized_desired.get(key)
    ]


def equivalent_event(existing: dict, desired: dict) -> bool:
    return not event_differences(existing, desired)


def create_event(service, config: Config, target: SyncTarget, body: dict) -> None:
    if config.dry_run:
        logging.info("DRY RUN: would create event: %s", body["summary"])
        return
    created = service.events().insert(calendarId=target.google_calendar_id, body=body).execute()
    logging.info("Created Google Calendar event: %s", created.get("id"))


def patch_event(service, config: Config, target: SyncTarget, event_id: str, body: dict) -> None:
    if config.dry_run:
        logging.info("DRY RUN: would patch event %s: %s", event_id, body["summary"])
        return
    service.events().patch(calendarId=target.google_calendar_id, eventId=event_id, body=body).execute()
    logging.info("Updated Google Calendar event: %s", event_id)


def delete_event(service, config: Config, target: SyncTarget, event_id: str) -> None:
    if config.dry_run:
        logging.info("DRY RUN: would delete event: %s", event_id)
        return
    try:
        service.events().delete(calendarId=target.google_calendar_id, eventId=event_id).execute()
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


def sync_target_once(config: Config, target: SyncTarget, service) -> None:
    local_tz = tz.gettz(config.timezone_name)
    if local_tz is None:
        raise RuntimeError(f"Invalid timezone: {config.timezone_name}")

    logging.info("[%s] Fetching Nextcloud VTODOs from %s", target.target_id, target.nc_caldav_url)
    tasks = parse_tasks(fetch_nextcloud_vtodos(target), local_tz)
    logging.info("[%s] Fetched %d Nextcloud task component(s)", target.target_id, len(tasks))

    existing_events = list_managed_google_events(service, target)
    logging.info("[%s] Fetched %d managed Google Calendar event(s)", target.target_id, len(existing_events))

    planned_keys: Set[str] = set()

    for _, task in tasks.items():
        plans = build_event_plans(task, config, target, local_tz)
        if not plans:
            old = existing_events.get(task.uid)
            if old:
                logging.info("[%s] Task has no plannable due date; deleting existing event for UID %s", target.target_id, task.uid)
                delete_event(service, config, target, old["id"])
            continue

        for plan in plans:
            existing = existing_events.get(plan.source_key)

            if should_delete_for_status(task, config):
                if existing:
                    delete_event(service, config, target, existing["id"])
                continue

            planned_keys.add(plan.source_key)
            body = event_body_from_plan(plan, config)

            if existing is None:
                create_event(service, config, target, body)
            else:
                differences = event_differences(existing, body)
                if differences:
                    logging.info(
                        "[%s] Event differs for source key %s; changed fields: %s",
                        target.target_id,
                        plan.source_key,
                        ", ".join(differences),
                    )
                    patch_event(service, config, target, existing["id"], body)
                else:
                    logging.debug(
                        "[%s] Event already up to date for source key %s",
                        target.target_id,
                        plan.source_key,
                    )

    for key, event in existing_events.items():
        if key not in planned_keys:
            delete_event(service, config, target, event["id"])

    logging.info("[%s] Sync run completed", target.target_id)


def sync_all_targets(config: Config, service) -> None:
    failures = 0
    for target in config.targets:
        try:
            sync_target_once(config, target, service)
        except Exception as exc:
            failures += 1
            logging.exception("[%s] Sync target failed: %s", target.target_id, exc)
    if failures:
        logging.error("Sync cycle completed with %d failed target(s) out of %d", failures, len(config.targets))
    else:
        logging.info("Sync cycle completed successfully for %d target(s)", len(config.targets))


def main() -> int:
    setup_logging()
    config = load_config()
    logging.info("Starting %s version %s", APP_SOURCE, APP_VERSION)

    logging.info("Configured sync targets: %s", ", ".join(f"{t.target_id}={t.display_name}" for t in config.targets))

    if config.dry_run:
        logging.warning("DRY_RUN is enabled; no Google Calendar changes will be written")

    service = build_google_calendar_service(config)

    while True:
        try:
            sync_all_targets(config, service)
        except Exception as exc:
            logging.exception("Sync run failed: %s", exc)

        if config.sync_interval_seconds <= 0:
            break
        time.sleep(config.sync_interval_seconds)

    return 0


if __name__ == "__main__":
    sys.exit(main())

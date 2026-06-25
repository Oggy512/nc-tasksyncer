# Nextcloud Task to Google Calendar Sync

## Version

`0.1.0`

## Synopsis

One-way synchronization from one Nextcloud CalDAV task list (`VTODO`) into one shared Google Calendar as normal calendar events.

## Short description

Nextcloud remains the authoritative task backend. Google Calendar is used only as a shared visibility and reminder layer. Open tasks with due dates are created or updated as events. Completed or deleted tasks remove the corresponding calendar event. Overdue open tasks can be moved to today to keep reminders visible on both phones.

## Architecture

```text
Nextcloud Tasks / CalDAV VTODO
        ↓
Python Docker container
        ↓
Google Calendar API
        ↓
Shared Google Calendar
```

## Setup

### 1. Google Service Account

1. Create a Google Cloud project.
2. Enable the Google Calendar API.
3. Create a service account.
4. Create a JSON key for the service account.
5. Save the JSON file as:

```text
./secrets/google-service-account.json
```

6. Share the target Google Calendar with the service account email address.
7. Grant at least: `Make changes to events`.

The target calendar ID usually looks like:

```text
xxxxxxxxxxxxxxxx@group.calendar.google.com
```

### 2. Nextcloud

Create an app password for the Nextcloud user that can read the shared task list.

Use the CalDAV URL of the concrete task list/calendar, for example:

```text
https://cloud.example.com/remote.php/dav/calendars/markus/family-tasks/
```

### 3. Configure environment

```bash
cp .env.example .env
nano .env
```

### 4. Start

```bash
docker compose up -d --build
```

### 5. Logs

```bash
docker compose logs -f
```

## Important behavior

- One-way sync only: Nextcloud -> Google Calendar.
- Manual changes in Google Calendar are overwritten.
- Tasks without due dates are ignored by default.
- Overdue open tasks are moved to today if `OVERDUE_MODE=move_to_today`.
- Completed tasks delete their mirrored event if `COMPLETED_MODE=delete_event`.

## Dry run

Set this in `.env`:

```text
DRY_RUN=true
```

The container then logs what it would do without changing Google Calendar.

## Limitations in version 0.1.0

- Recurring tasks are not expanded.
- Attachments are not synchronized.
- Categories and priorities are not mapped to event colors.
- No bidirectional synchronization.

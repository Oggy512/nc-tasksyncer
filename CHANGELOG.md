# Changelog

## 0.2.0

- Added one shared and up to two optional personal synchronization targets.
- Each target supports an independent Nextcloud CalDAV URL, Nextcloud account and Google Calendar ID.
- Added target isolation through Google Calendar private extended properties.
- Added backward-compatible adoption of shared events created by version 0.1.x.
- Added `requirements.in` and the Docker-based controlled update script.
- Replaced the short README with full GitHub project documentation.

## 0.1.2

- Added status and priority mapping.
- Added recurring-task expansion using RRULE, RDATE and EXDATE.

## 0.1.1

- Preserved VTODO due time in Google Calendar events.
- Preserved due time when overdue events are moved to today.

## 0.1.0

- Initial one-way synchronization from one Nextcloud task list to one Google Calendar.

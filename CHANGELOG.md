# Changelog

## 0.2.3

- Fixed a regression in 0.2.2 where the fingerprint-aware `equivalent_event()` function existed but was not used by the synchronization loop.
- Existing events with a matching `syncFingerprint` are now skipped without a Google Calendar PATCH request.
- Legacy events without `syncFingerprint` are patched once to add the fingerprint, then remain unchanged on later runs.
- Logging now distinguishes legacy migration from a real fingerprint change.

## 0.2.2

- Added stable SHA-256 event fingerprints for idempotent synchronization.
- Removed volatile Nextcloud `LAST-MODIFIED` data from generated event descriptions.

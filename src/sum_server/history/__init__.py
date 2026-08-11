"""Per-field change history for a host's observed and assigned data.

Facts and components are stored as current state: an ingest overwrites the
value in place, so ``first_seen`` / ``last_seen`` can say a value exists and was
seen recently but not what it used to be. This module keeps the before-and-after
of every change so any value on a host page can answer when it last moved.

Distinct from ``core/audit.py``: audit records that an actor performed an
action, append-only and fleet-wide. This records that a *value* changed, scoped
to one host and one field, and is read from the UI rather than by an admin.
"""

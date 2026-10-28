"""Mail census (private adapter, not part of the public release): an inert stub.

`bin/sync.py` runs `mail_reconcile_step` only on `REPAIR_MACHINE`; with none named, it never runs.
"""

REPAIR_MACHINE = None
MIN_INTERVAL_HOURS = 20
TRAILING_DAYS = 30
REPORT_NAME = "mail-reconcile.json"

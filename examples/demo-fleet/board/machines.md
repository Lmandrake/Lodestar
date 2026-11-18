# Machines (demo fleet)

Invented machines. Key is the machine's identity, Role the word used in stamps, Labels the
kinds that `applies:` headers and skill rows select on, Services the singleton duties at most
one machine may each claim (the always-on desk machine takes all four, as on a real fleet).

| Key | Role | Locks | Labels | Hosts | Storage | Services | Notes |
|-----|------|-------|--------|-------|---------|----------|-------|
| studio | studio | remove | mac, desk |  | ~/agent-storage | bearing, outbox, calendar, mailrepair | always-on desk machine; authors the rename |
| laptop | laptop | remove | mac, laptop |  | ~/agent-storage |  | travels; has a battery and a lid |
| tower | tower | remove | linux |  | ~/agent-storage |  | headless box; carries only some repos |

# Switchyard Sequencer

Switchyard Sequencer is a runnable Python backend baseline for rail yard
operations. It accepts inbound train manifests, classifies cars onto standing
tracks under destination and hazard rules, plans an outbound pull sequence that
respects LIFO stacks, executes buffer moves, and closes shifts with a
deterministic yard balance. It uses only the Python standard library and local
JSON files, so it runs without an external database or online service.

## Run the service

```bash
PYTHONPATH=src python3 -m switchyard.entry.server --port 8701 --data-dir data/dev
```

Or use the environment variables `SWITCHYARD_PORT` and `SWITCHYARD_DATA_DIR`.
The server exposes JSON endpoints under `/api`; `GET /api/health` returns a
simple liveness payload.

## Workflow checks

Deferred test mode is active in this baseline: no unit tests are shipped, and a
later engineering task stage adds red/green verification tests. Production
workflow checks exercise the real HTTP API:

```bash
PYTHONPATH=src python3 checks/wf_intake_classify.py
PYTHONPATH=src python3 checks/wf_outbound_sequence.py
PYTHONPATH=src python3 checks/wf_pull_depart.py
PYTHONPATH=src python3 checks/wf_close_shift.py
PYTHONPATH=src python3 checks/wf_shift_event_query.py
```

Each check starts an isolated server on a free port with a temporary data
directory and stops the server before exiting.

## Shift event queries

`GET /api/shifts/{code}` keeps its original shape (shift plus the 40 most
recent events) when called without parameters. Add any of the following query
parameters to switch to the filtered, paginated response:

- `kind`: event type to include; repeat the parameter or pass a comma-separated
  list (for example `kind=PULL_RUN_ADVANCED,TRAIN_DEPARTED`).
- `start_at`, `end_at`: inclusive UTC bounds (`YYYY-MM-DDTHH:MM:SSZ`).
- `object_code`: shift, intake, outbound, pull-run, track/bay, or car code;
  every event referencing the object is returned.
- `limit`: page size (1–200, default 40).
- `cursor`: opaque token from the previous page's `next_cursor`.

The first paged request anchors the walk to the current highest event sequence.
Events appended while paging carry larger sequences, so they can neither push
older events off a later page (skips) nor reappear (duplicates); `total` and
`anchor` stay constant for the whole walk, while `live_total` and
`new_event_count` report how many events were appended above the anchor.
Restart without a cursor to adopt the new high water mark. A cursor is bound to
its shift, filters, and time range and is rejected if those change.

Each returned event carries a `relation` block describing its link to the
current state of its subject: `CURRENT` (it is the latest state-bearing event),
`SUPERSEDED` (a later event moved the object on), `REJECTED` (the action never
took effect, for example a blocked closure), `MISSING`, or `INFORMATIONAL`,
plus `current_state`, `declared_state`, and `state_matches`. Events touching
cars also include `car_states` with each car's current state/location, so an
intermediate pull action is not mistaken for the final car state.

## Directory structure

```text
src/switchyard/domain/    entities, validation, transitions, allocation, sequencing
src/switchyard/storage/   workspace state, atomic JSON persistence, seed data
src/switchyard/service/   workflow commands and application context
src/switchyard/report/    metrics and closure summaries
src/switchyard/entry/     HTTP server, router, and request handling
checks/                   production workflow checks
data/                     runtime JSON data (created on first run)
```

The repository stores one versioned workspace file plus an append-only event
journal. Every write goes through a same-directory temporary file and an atomic
replace, so interrupted writes do not leave partial state.

## Environment variables

- `SWITCHYARD_PORT`: default service port, used when `--port` is absent.
- `SWITCHYARD_DATA_DIR`: default data directory, used when `--data-dir` is
  absent.

## Example API sequence

```text
POST /api/shifts
POST /api/intake-trains
POST /api/intake-trains/INT-01/classify
POST /api/outbound-trains
POST /api/outbound-trains/OB-01/sequencer
POST /api/pull-runs/RUN-01/advance
POST /api/outbound-trains/OB-01/depart
POST /api/shifts/SHIFT-01/close
GET  /api/yard
```

Request and response examples are embedded in the project specification and in
the workflow checks.

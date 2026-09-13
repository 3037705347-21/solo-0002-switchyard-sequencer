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
PYTHONPATH=src python3 checks/wf_transfer_capacity.py
PYTHONPATH=src python3 checks/wf_transfer_partial_hold.py
```

Each check starts an isolated server on a free port with a temporary data
directory and stops the server before exiting.

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

## Transfer line capacity scheduling

The yard may register more than one transfer line (the seed yard ships with
`X1`). Lines are registered with a car-slot capacity and expose a live
capacity view:

```text
GET  /api/transfer-lines
POST /api/transfer-lines                       {"code": "X2", "capacity_cars": 2}
POST /api/transfer-lines/X2/state              {"state": "MAINTENANCE"}
```

`POST /api/outbound-trains/{code}/sequencer` accepts either no
`transfer_code`, `"AUTO"`, or an explicit line code. With AUTO the scheduler
places the whole ticket on one line whose currently free slots cover its peak
concurrent demand (the deepest blocker stack). Feasible lines are ranked by a
stable best-fit key: least slack after placement, then highest committed load,
highest physical load, smallest capacity, stable registration order, and
finally code — never by name or call order.

Capacity is sold per active plan. Queued runs commit their peak slots; a
running run commits the full peak reached when its remaining steps are replayed
from the cars physically on the line right now (so a peak-2 ticket that has
buffered only one car still holds both slots until a RETURN frees one);
completed/cancelled runs release. Holds are derived from persisted runs and
bay positions on every plan, advance, cancellation, and restart, so partial
execution, blocker returns, cancellation, and process restart all recompute
occupancy consistent with the vehicles on the ground; released reservations
remain on file as an audit trail. A planning rejection (`TRANSFER_CAPACITY`,
HTTP 422) classifies each line as `existing_occupancy`, `single_line_limit`, or
`line_unavailable`. A queued run can be cancelled with
`POST /api/pull-runs/{code}/cancel`; running runs must be advanced to
completion because their cars are physically split between track and transfer
line. Single-transfer-line workspaces keep their old behavior (AUTO simply
resolves to the only registered line).

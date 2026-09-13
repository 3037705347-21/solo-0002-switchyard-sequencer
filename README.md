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
PYTHONPATH=src python3 checks/wf_audit_reconciliation.py
```

Each check starts an isolated server on a free port with a temporary data
directory and stops the server before exiting.

## Event audit and reconciliation

The state file and the append-only event journal can both be inspected by
humans, so the service ships a strictly read-only audit module that answers
whether the two describe the same yard work. It correlates events by sequence,
shift, timestamp, and object, and surfaces:

- duplicate events (same sequence appearing more than once),
- sequence gaps and a state/journal sequence counter mismatch,
- physical order inversions and timestamp inversions,
- events present on only one side (`journal_only_event` / `state_only_event`)
  and field mismatches between paired events,
- snapshot objects that no journal event accounts for,
- a trace from every event to the current state of its object and cars,
  marked `CONSISTENT`, `INCONSISTENT`, or `OBJECT_MISSING`.

Malformed journal lines, unknown event kinds, and events whose object cannot be
resolved are never silently dropped and never block normal service: they are
retained as manual review items. The auditor never writes and never appends
"fix-up" events, so problems cannot be hidden by rewriting the journal.

```bash
# HTTP: filter the event view by shift, kind, car, or pull run
GET /api/audit/reconciliation?shift=SHIFT-01&kind=TRAIN_DEPARTED
GET /api/audit/reconciliation?car=C-N4-31&pull_run=RUN-OB-01

# Stand-alone read-only CLI (exit 2 flags error-level divergence)
PYTHONPATH=src python3 -m switchyard.entry.audit_cli --data-dir data/run \
    --kind TRAIN_DEPARTED --issues-only --fail-on-discrepancy
```

## Directory structure

```text
src/switchyard/domain/    entities, validation, transitions, allocation, sequencing
src/switchyard/storage/   workspace state, atomic JSON persistence, seed data
src/switchyard/service/   workflow commands and application context
src/switchyard/report/    metrics, closure summaries, event audit/reconciliation
src/switchyard/entry/     HTTP server, router, audit CLI, and request handling
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

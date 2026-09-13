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
PYTHONPATH=src python3 checks/wf_snapshot_corrections.py
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
GET  /api/closure-snapshots
GET  /api/closure-snapshots/SNAP-SHIFT-01
GET  /api/closure-snapshots/SNAP-SHIFT-01/export
POST /api/closure-snapshots/SNAP-SHIFT-01/corrections
```

Request and response examples are embedded in the project specification and in
the workflow checks.

## Correcting an archived snapshot

Closure snapshots are immutable historical evidence. When a remark or the
responsible party turns out to be wrong after closure, corrections are handled
through a separate append-only record rather than by editing the snapshot:

```text
POST /api/closure-snapshots/{code}/corrections
{
  "changes": {"remark": "更正后的备注", "responsible": "REV-01"},
  "reason": "差异原因",
  "revised_by": "修订人",
  "idempotency_key": "optional stable key"
}
```

Only the explanatory fields `remark` and `responsible` are correctable; any
attempt to change `metrics`, `blockers`, `version`, or event data is rejected
with `VALIDATION_ERROR`. The original snapshot document is never modified.
List (`GET /api/closure-snapshots`), detail, and export responses keep the
archived values and the effective revised values side by side, and every
correction is appended to the event journal as `SNAPSHOT_CORRECTED`. Repeating
an identical submission (same content, or the same `idempotency_key` with the
same body) replays the existing revision stably without creating a new record;
reusing a key with different content, replaying a superseded revision, or
submitting a no-op returns `CONFLICT` so prior comparisons cannot be silently
overwritten.

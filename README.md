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
PYTHONPATH=src python3 checks/wf_shift_snapshot_archive.py
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
GET  /api/shift-snapshots
GET  /api/shift-snapshots/SNAP-SHIFT-01
POST /api/shift-snapshots/diff
```

### Shift snapshot archive

Every approved closure appends an immutable snapshot to the
`closure_snapshots` archive. Each document freezes the complete metrics, the
workspace/shift version, the closure timestamp, and the source event range
(first/last event sequence and count contributed by that shift):

- `GET /api/shift-snapshots` lists snapshots newest-first, optionally filtered
  by `shift_code`, `closed_from`, and `closed_to` (ISO-8601, inclusive).
- `GET /api/shift-snapshots/{code}` returns one full snapshot document.
- `POST /api/shift-snapshots/diff` with `{"base": ..., "target": ...}` produces
  a field-level comparison covering car state counts, track occupancy,
  open/completed outbounds, unfinished runs (plus run/outbound state counts),
  blockers, and source event ranges. Missing fields are reported explicitly
  with `missing_in_base` / `missing_in_target`, `null` values, and `null` deltas
  instead of being treated as zero.

Archive reads never commit and never mutate the live yard; later shifts can
only append new snapshots and can never rewrite older ones.

Request and response examples are embedded in the project specification and in
the workflow checks.

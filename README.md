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
PYTHONPATH=src python3 checks/wf_car_journey.py
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

## Car journey profiles

`GET /api/car-journeys/{carCode}` rebuilds a single car's timeline from the
persisted data directory. The profile aggregates, per car:

- `RECEIVED` (intake arrival), `CLASSIFIED` (track spotting), `RESERVED`
  (pull plan), `BUFFERED` / `RETURNED` (transfer-bay moves), `ASSEMBLED`
  (pulled onto the outbound consist), and `DEPARTED` entries with timestamps,
  locations, source locations, and the linked intake / pull run / outbound
  train and shift.
- `flags` marks places where the trajectory disagrees with the car's current
  state or location (or with stack/consist membership), and `evidence_gaps`
  reports missing journal evidence for older data. Surviving entity fragments
  are kept with `evidence: "entity"`; missing fragments are never fabricated.
- Run and departure journal events carry no run/train code, so events are
  attributed only when one owner is forced by the actual pull records
  (completed consist, step counts, sequence windows, timestamps). When later
  plans execute first or identical events cannot be tied to one run, the event
  is left unattributed and reported as an `AMBIGUOUS_*` evidence gap; it is
  never borrowed from another car's run.
- Only car-relevant structured journal events are used; shift, closure, and
  yard-view messages are ignored, and event message text is never parsed.
- Queries are read-only and deterministic: repeated calls return the same
  document and never change cars or events.

`GET /api/car-journeys` returns a compact sorted index of all car journeys.

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

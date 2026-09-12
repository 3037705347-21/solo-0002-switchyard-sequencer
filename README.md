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
PYTHONPATH=src python3 checks/wf_batch_intake.py
PYTHONPATH=src python3 checks/wf_outbound_sequence.py
PYTHONPATH=src python3 checks/wf_pull_depart.py
PYTHONPATH=src python3 checks/wf_close_shift.py
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

## Batch intake

For peak arrivals with several trains at once, prepare a local JSON file and
post its path. The whole file is validated (structure, per-train/per-car
rules, duplicates inside the batch and against the yard) before anything is
written; on failure the response contains per-train, per-car locators and no
partial state remains.

```json
{
  "code": "BATCH-20260912-01",
  "received_at": "2026-09-12T08:30:00Z",
  "note": "morning peak arrivals",
  "trains": [
    {
      "code": "INT-101",
      "route": "RAIL-11",
      "arrival_at": "2026-09-12T08:10:00Z",
      "cars": [
        {"code": "C-N4-101", "kind": "BOX", "destination": "N4",
         "loaded": true, "length_m": 18, "danger_class": "NONE"}
      ]
    }
  ]
}
```

```text
POST /api/intake-batches        {"source_path": "/abs/path/to/batch.json"}
GET  /api/intake-batches/BATCH-20260912-01
```

The same document may be sent inline as `{"batch": { ... }}`. A successful
import creates the same open intakes and `RECEIVED` cars as repeated calls to
`POST /api/intake-trains`, one `TRAIN_RECEIVED` event per train, a summary
`BATCH_IMPORTED` event with the source path and content hash, and a stored
batch record for provenance. Re-importing identical content is rejected with
`CONFLICT`, even with a different batch code.

Request and response examples are embedded in the project specification and in
the workflow checks.

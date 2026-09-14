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
PYTHONPATH=src python3 checks/wf_shift_statistics.py
```

Each check starts an isolated server on a free port with a temporary data
directory and stops the server before exiting.

## Per-shift operation statistics

Beyond the instant yard counters, each shift carries operation statistics that
answer backlog, repeat-work, turnover, and dwell questions:

- average and maximum duration for each stage — intake handling
  (receive → classify), arrival → classified waiting (intake and per car),
  pull planning delay (planned → started), pull execution (started →
  completed), car arrival → assembly, and car arrival → departure. Waiting and
  dwell stages start at the inbound train's manifest `arrival_at` (physical
  arrival); when that timestamp is missing or malformed the receive-event
  time is used and an entry is added to `issues`;
- buffer, return, and pull move counts, with every move of a failed attempt
  reported separately because it is rolled back;
- failed runs, replanned retry runs, and how many outbounds needed a retry;
- per-track placements, releases, turnovers, and cars still on the track;
- destination distribution (received / classified / assembled / departed);
- per-intake, per-run, per-outbound, and per-car detail rows plus an `issues`
  list that records missing or unparseable timestamps instead of treating them
  as zero.

All statistics are recomputed solely from the shift-filtered event trail, so
`GET /api/shifts/{code}/statistics/recompute` rebuilds them straight from the
raw `events.jsonl` journal. Open shifts recompute live on every read; when a
shift closes the numbers are frozen inside the closure snapshot and later reads
serve that frozen document.

When a pull action fails midway, the whole attempt is rolled back to its
starting state — including pull and buffer moves already committed by earlier
advances: buffered cars are returned to their source tracks, assembled cars go
back onto the stack as reserved-then-standing, the transfer bay is emptied, and
the outbound train returns to draft. The failure is persisted as
`PULL_RUN_FAILED` with the full list of rolled-back steps, and
`POST /api/outbound-trains/{code}/retry` plans a fresh numbered attempt run
(`RUN-OB-…-R2`, `…-R3`, …) without any manual repair.

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
POST /api/outbound-trains/OB-01/retry        # after a failed run attempt
POST /api/outbound-trains/OB-01/depart
GET  /api/shifts/SHIFT-01/statistics         # live while open, frozen once closed
GET  /api/shifts/SHIFT-01/statistics/recompute
POST /api/shifts/SHIFT-01/close
GET  /api/yard
```

Request and response examples are embedded in the project specification and in
the workflow checks.

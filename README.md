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

## Verify everything locally

One command runs the whole validation chain from any directory, with only the
Python standard library installed:

```bash
python3 checks/run_all.py
```

It performs the steps in a fixed order and stops at the first failure:

1. **source gate** — compile every Python file under `src/` and `checks/`, then
   import every `switchyard` module (catches syntax and import errors before
   any service starts);
2. `wf_intake_classify` — open shift, accept intake, classify cars;
3. `wf_outbound_sequence` — plan outbound consist and buffered pull run;
4. `wf_pull_depart` — execute the pull run and depart the train;
5. `wf_close_shift` — blocked closure, then a clean shift handoff.

Each check starts an isolated server on an ephemeral port with a fresh
temporary data directory; the command never writes into `data/` and does not
depend on any file from a previous run, so it is safe to rerun. Exit code is
non-zero on any failure, and the failing check name, offending HTTP request,
traceback, and captured service output (including its port and state-file
path) are printed before later steps are skipped.

The individual checks can still be run on their own:

```bash
PYTHONPATH=src python3 checks/wf_intake_classify.py
PYTHONPATH=src python3 checks/wf_outbound_sequence.py
PYTHONPATH=src python3 checks/wf_pull_depart.py
PYTHONPATH=src python3 checks/wf_close_shift.py
```

Each check stops its server before exiting.

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

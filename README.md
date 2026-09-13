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
PYTHONPATH=src python3 checks/wf_yard_inventory.py
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

Request and response examples are embedded in the project specification and in
the workflow checks.

## Yard inventory detail

`GET /api/yard` keeps its original `metrics`, `blockers`, `active_shift`, and
`shifts` fields and additionally returns an `inventory` object built directly
from the persisted workspace (never inferred from action history). It lists
every standing track stack, the X1 transfer bay, and each outbound consist in
**bottom-to-top** reading order (`from_bottom`/`from_top` positions included),
and partitions every car into mutually exclusive physical buckets:

- `on_tracks`: cars sitting in a standing track stack. Reserved cars that have
  not been pulled yet stay here and are flagged with `reserved` and
  `reserved_for` instead of being counted twice;
- `in_buffers`: cars parked in X1 mid pull-run (still `STANDING` by car state);
- `assembled`: cars already mounted on an outbound train consist
  (`outbound_trains[].assembled_cars`, with not-yet-pulled planned cars listed
  under `pending_cars`);
- `pending_intake`, `departed`, `removed`: received-but-unclassified cars and
  cars no longer physically in the yard.

`inventory.buckets` always sums to `inventory.total_cars`, and `anomalies`
reports any state/location disagreement found while reconciling the single-car
records against the containers, so detail, summary counts, and individual car
state can be cross-checked after a service restart.

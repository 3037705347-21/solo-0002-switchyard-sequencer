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
PYTHONPATH=src python3 checks/wf_car_deactivation.py
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

## Vehicle deactivation and recovery

When a car must stop working, the operator submits its code, a reason, and a
disposition (`HOLD` for a recoverable suspension or `RETIRE` for a permanent
withdrawal). The service first confirms the car's real attribution across
standing tracks, transfer bays, intakes, outbound plans/assemblies and pull
runs. If any active job still references it, deactivation is rejected without
touching state and the response carries an executable `conflicts` list whose
`action_method` / `action_path` / `action_payload` entries resolve each
reference (advance a running pull run, abandon an outbound plan, cancel an
open intake). A free standing car is pulled out of its track slot, moved to
state `REMOVED` at location `OUT_OF_SERVICE`, and can no longer be classified,
selected by a new outbound plan, or re-admitted on an intake.

Resolution endpoints backing the conflict list:

- `POST /api/outbound-trains/{code}/abandon` releases reservations and returns
  assembled cars to their source tracks (refused while a bay holds buffered
  cars; advance the run to completion instead).
- `POST /api/intake-trains/{code}/cancel` cancels an intake before
  classification and removes its unclassified cars.

Recovery endpoints:

- `POST /api/car-recoveries` clears a `HOLD`, restoring the car to its original
  track slot (pass `target_track` to place it elsewhere if the original track
  is full or in maintenance). `RETIRE` cannot be recovered.
- `GET /api/car-deactivations` lists every decision;
  `GET /api/car-deactivations/{code}` shows one record with the car's current
  attribution.

Every decision, blocked attempt, recovery and recovery block is written to the
event journal (`CAR_DEACTIVATED`, `CAR_DEACTIVATION_BLOCKED`,
`CAR_RECOVERED`, `CAR_RECOVERY_BLOCKED`) and survives service restarts.

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

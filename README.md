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
PYTHONPATH=src python3 checks/wf_pull_dispatch.py
PYTHONPATH=src python3 checks/wf_pull_reverse_order.py
PYTHONPATH=src python3 checks/wf_close_shift.py
```

Each check starts an isolated server on a free port with a temporary data
directory and stops the server before exiting. The dispatch-board check keeps
its data directory across a real server restart to prove the queue and claim
tokens survive.

## Pull dispatch board

Queued pull plans share limited standing tracks and the single X1 transfer
bay. Register a draft outbound at the board to receive a queue-ordered
dispatch ticket that declares its source tracks, transfer bay, blocker cars,
and target cars:

```text
POST /api/outbound-trains/OB-01/dispatch   {"transfer_code":"X1","client_id":"crew-A"}
GET  /api/dispatch                         queue order, blockers, release actions
POST /api/dispatch/TKT-0001/claim          {"client_id":"crew-A"} -> claim_token
POST /api/pull-runs/RUN-OB-01-1/advance    {"steps":3,"claim_token":"..."}
POST /api/dispatch/TKT-0001/cancel         {"client_id":"crew-A","claim_token":"..."}
```

A ticket blocked by an earlier ticket reports the blocking ticket, the
overlapping declared resources, and the step at which each resource is
released. Only the holding client can advance a claimed run; a repeat claim
with the same client id and token is idempotent, and a different client is
rejected with `RESOURCE_BUSY`. Tickets, tokens, and queue order persist in the
workspace file. Cancelling an unstarted ticket returns its outbound to DRAFT,
releases target-car reservations and declared track/X1 resources, and unblocks
later tickets; a run already in progress cannot be cancelled.

Steps are derived at registration and recomputed from the current stacks at
claim and at run start, so when an earlier ticket pulls a blocker a later
ticket buffered (reverse target order), the later ticket never executes stale
buffer steps; the refreshed resources and `plan_stale` flag are shown on the
board while the claim token, ticket and run codes stay stable
(`checks/wf_pull_reverse_order.py` covers this across a restart).

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

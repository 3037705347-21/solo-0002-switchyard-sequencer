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
PYTHONPATH=src python3 checks/wf_persistence_recovery.py
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
journal. Every state write goes through a same-directory temporary file, an
fsync, and an atomic replace (followed by a directory fsync), so interrupted
writes never leave a partial state file.

## Commit and crash-recovery protocol

Every successful business command is one **commit** with a strict ordering:

1. The command mutates the workspace and records its event(s) in memory.
2. A ledger entry (`commit_id`, `commit_number`, resulting `state_version`,
   event sequences and per-event SHA-256 content hashes) is appended to
   `workspace.commits`; the state file is atomically replaced and fsync'd.
3. The journal then receives, in order: one framed event line per event (with
   the same content hash) and a **commit marker** carrying the commit id and
   event hashes. The marker is the durable commit boundary.

On startup (`YardApplication.bootstrap`, before the HTTP server accepts
traffic) the state ledger and the journal are reconciled:

- state replaced but journal never written (crash between steps 2 and 3): the
  single missing tail commit is **rolled forward** from state — the business
  result is neither lost nor applied twice;
- event lines present but marker missing (torn journal write): the batch for
  the pending commit is completed; partial lines for a commit absent from
  state are discarded;
- duplicated event/marker lines (a retried append): de-duplicated by
  `(commit_id, sequence)` / commit id and the journal is rewritten;
- tampered event content, hash mismatch, a missing middle marker, or a state
  more than one commit ahead of the journal: **unrecoverable divergence** —
  the process refuses to start (exit code 2) instead of treating bad state as
  history;
- state files and journals written before commit framing are migrated once,
  automatically; no manual log edits are required.

The last startup verdict is visible at `GET /api/recovery`
commit ids and counts of removed duplicates/partial records).

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

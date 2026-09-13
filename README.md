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
```

Each check starts an isolated server on a free port with a temporary data
directory and stops the server before exiting.

## Yard backup and migration

The storage layer also ships an offline backup, validation and restore tool so
a damaged data directory can be recovered or migrated without copying the JSON
files by hand. A packet is a single zip archive containing `manifest.json`,
`yard-state.json`, and `events.jsonl`. The manifest records the packet format
version, export time, event range (count and first/last sequence), and a
SHA-256 digest plus byte size for every member.

```bash
PYTHONPATH=src python3 -m switchyard.entry.backup_cli backup   --data-dir data/dev --output yard.sypack
PYTHONPATH=src python3 -m switchyard.entry.backup_cli inspect  yard.sypack
PYTHONPATH=src python3 -m switchyard.entry.backup_cli migrate-preview yard-state-v0.json --journal events-v0.jsonl
PYTHONPATH=src python3 -m switchyard.entry.backup_cli restore  yard.sypack --data-dir data/new
```

Restore always validates inside an isolated staging directory first: packet
integrity (zip CRC, sizes, digests), field readability (every entity decodes
through the same codec as live loads), legacy migration preview, and basic
state/event consistency (gap-free event sequences, journal prefixes the state
events, and cross-entity references resolve). A target directory that already
holds running state is refused unless `--replace-existing` is given. Only a
fully valid packet is committed with a directory-level atomic rename; any
failure rolls the old directory back, so the target is either completely
restored or left exactly as it was. Schema v0 data is migrated to the current
schema with a preview of every rename; values with no safe mapping are reported
with an explicit non-migration reason instead of being guessed. Add `--json`
for machine-readable output.

```bash
PYTHONPATH=src python3 checks/wf_backup_restore.py
```

That check drives normal packets, truncated/corrupted packets, legacy v0
packets, and targets that already hold running state, then confirms shifts,
cars, the event journal, and closure snapshots remain readable after restore.


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

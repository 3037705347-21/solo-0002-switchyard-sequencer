# Switchyard Sequencer - Project Specification

## Goal

Switchyard Sequencer is a local backend service for a rail classification yard.
It helps yard dispatchers turn an inbound train manifest into stable car
spotting on standing tracks, build an outbound consist, derive a buffer-safe
pull plan, execute shunting actions, and close a shift with a deterministic
yard balance. The service runs entirely on the local machine and stores its
state as JSON files under a configurable data directory.

## Users

- Yard dispatchers open shifts, accept inbound trains, and release classified
  cars to standing tracks.
- Plan builders choose cars for an outbound consist and ask the sequencer for a
  verified pull plan.
- Shunt crew leads execute pull actions and mark outbound trains for departure.
- Shift leads request a closure balance and archive a shift snapshot.

## Core entities

- `YardShift`: a dispatcher shift with an open or closed state and an event
  trail.
- `FreightCar`: a rail car with a unique code, kind, destination route, load
  state, length, and hazard class.
- `StandingTrack`: a capacity-limited track with a purpose, operating state,
  destination affinity, hazard rating, and a LIFO car stack.
- `BufferBay`: a short transfer bay used to park non-target cars while a target
  car is pulled from deeper in a track stack.
- `IntakeTrain`: an inbound train with a consist of newly received cars and an
  open, partial, or classified state.
- `OutboundTrain`: a train destined for a route code with a planned car
  sequence and an assembled sequence.
- `PullRun`: a stateful sequence of buffer, pull, and return actions derived
  from a validated outbound plan.
- `DepartureManifest`: an immutable, versioned snapshot of one outbound train
  taken when its plan is confirmed. It freezes the planned sequence, the
  actual assembled sequence, basic car information, provenance (intake,
  spotted track, shift, pull run), and a SHA-256 content digest. Old versions
  are never rewritten.
- `ClosureSnapshot`: an immutable metric set and blocker list produced when a
  shift closes.

## Workflows

### 1. Open a shift and classify an intake train

Entry: `POST /api/shifts`, `POST /api/intake-trains`,
`POST /api/intake-trains/{code}/classify`

The dispatcher opens a shift, submits an inbound train with a car manifest, and
asks the classifier to place every car onto an active standing track. The
service validates car codes, dimensions, hazard classes, destination routes,
and duplicate codes, then applies destination affinity, hazard rating, and
capacity rules. A fully classified train is persisted and every placed car
becomes available for outbound planning.

### 2. Plan an outbound pull sequence

Entry: `POST /api/outbound-trains`, `POST /api/outbound-trains/{code}/sequencer`

The planner creates an outbound train for a destination with an explicit car
sequence. The sequencer checks that each car is standing, unreserved, and on a
compatible track, then simulates the LIFO constraint of every source stack. It
generates a `PullRun` with buffer, pull, and return actions, reserves the
planned cars, and moves the outbound train into a planned state.

### 3. Execute pull actions and depart the outbound train

Entry: `POST /api/pull-runs/{code}/advance`,
`POST /api/outbound-trains/{code}/depart`

The crew lead advances the pull run one action at a time. Each advance verifies
the current track top, the buffer state, and the car reservation before
changing locations. Buffer cars are parked and later returned, planned cars are
appended to the outbound assembled consist, and the run completes only when the
assembled sequence matches the planned sequence. The dispatcher can then mark
the train departed and move its cars into the departed state.

### 3a. Freeze and verify a departure manifest

Entry: `POST /api/outbound-trains/{code}/manifests`,
`GET /api/outbound-trains/{code}/manifests`,
`GET /api/outbound-trains/{code}/manifests/{version}`,
`GET /api/outbound-trains/{code}/manifests/{version}/verify`,
`GET /api/outbound-trains/{code}/manifests/{version}/export`,
`GET /api/outbound-trains/{code}/readiness`,
`POST /api/cars/remove`,
`POST /api/outbound-trains/{code}/replan`

Once an outbound plan is confirmed (state `PLANNED` or `READY`), the driver
publishes a departure manifest. The service freezes the planned sequence, the
assembled sequence captured at that moment, each car's basic information, and a
provenance trail (shift, intake train and route, spotted track, active pull run
and transfer bay) into an immutable document. Each publication gets a
per-train monotonically increasing version number and a SHA-256 digest over the
canonical document content. Publishing never changes cars, tracks, runs, or the
train state.

When preparing for departure the readiness view compares the plan against the
live assembly slot by slot and separates two kinds of difference:

- `PENDING`: the plan has simply not been executed yet; the slot is empty and
  the planned car is still reserved (possibly parked in the buffer). It will
  resolve by continuing the pull run.
- `CONFLICT`: the plan and the actual assembly can no longer converge as-is,
  for example a planned car has been removed from service or an unplanned car
  occupies a slot.

A reserved car can be removed from service (defect hold) through
`POST /api/cars/remove`; pending plans referencing it immediately show a
conflict. A confirmed but not yet executed plan can be corrected with
`POST /api/outbound-trains/{code}/replan`, which supersedes the queued run
(failed with `error = "superseded by replan"`), releases old reservations, and
derives a fresh run. Published manifests keep pointing at the run codes frozen
at publication time and are never rewritten; verification reports whether each
old version still matches the current yard.

### 4. Close a shift with a yard balance

Entry: `POST /api/shifts/{code}/close`, `GET /api/yard`

The shift lead requests closure. The service computes standing, reserved,
assembled, and departed car totals plus track occupancy and open-train counts.
It blocks closure when any intake is open, any run is queued or running, or any
standing track is in maintenance with cars present. When the checks pass, it
stores a `ClosureSnapshot`, records the closure event, and exposes the yard
view for verification.

## State and rules

- Shift state transitions from `open` to `closed` only through an approved
  closure check.
- Intake state moves from `open` through `partial` to `classified`; cancellation
  is allowed only before classification.
- Outbound state moves from `draft` to `planned` when a pull run is created,
  then to `ready` when assembly completes, then to `departed`.
- Pull runs move from `queued` to `running`, then `completed` or `failed`.
- Car state moves from `received` to `standing`, `reserved`, `assembled`, and
  `departed`.
- Destination-sorting tracks accept only cars whose destination matches the
  track affinity.
- Hazardous cars require a hazard-rated track.
- A track in maintenance cannot receive cars and cannot be used as a pull
  source.
- Track spotting cannot exceed car count or total length capacity.
- A pull plan is valid only when every buffer move targets a standing car that
  is not reserved elsewhere and the transfer bay has enough capacity.
- Departure manifests are append-only: a published version is frozen with a
  version number and content digest, and later state changes never modify it.
- Manifest publication is derived from the persisted workspace and never
  mutates car, track, run, or outbound train state.
- Replanning is allowed only while the confirmed plan has no executed pull
  step; the superseded queued run is failed and a new run is derived.
- Closure is derived from the persisted workspace and never mutates car or
  track state.

## Modules and dependency direction

- `entry`: HTTP server, router, request parsing, and JSON envelopes.
- `service`: application context and workflow commands that coordinate domain,
  storage, and report modules (including manifest publication and replanning).
- `domain`: enums, entities, validation, state transitions, allocation rules,
  pull sequencing, frozen departure manifest construction, and domain errors.
- `storage`: workspace model, atomic persistence, seed tracks, and event
  journaling.
- `report`: yard metrics, closure validation, and deterministic summaries.

Entry routes call service commands. Service commands load the persisted
workspace, apply domain operations, and commit only after the operation
succeeds. Domain modules never read files. Report modules compute from a
workspace snapshot and never mutate it.

## Public interfaces

- `POST /api/shifts`: open a shift.
- `POST /api/intake-trains`: create an inbound train.
- `POST /api/intake-trains/{code}/classify`: place cars on standing tracks.
- `POST /api/outbound-trains`: create an outbound train.
- `POST /api/outbound-trains/{code}/sequencer`: create a pull run.
- `POST /api/outbound-trains/{code}/replan`: replace the confirmed consist and
  derive a fresh pull run (only before any step executes).
- `POST /api/outbound-trains/{code}/manifests`: publish a frozen manifest version.
- `GET /api/outbound-trains/{code}/manifests`: list manifest versions.
- `GET /api/outbound-trains/{code}/manifests/{version}`: fetch one frozen version.
- `GET /api/outbound-trains/{code}/manifests/{version}/verify`: verify digest
  and compare the frozen version against the current yard.
- `GET /api/outbound-trains/{code}/manifests/{version}/export`: export the
  canonical frozen document with live verification.
- `GET /api/outbound-trains/{code}/readiness`: live plan-vs-actual differences
  split into pending moves and conflicts.
- `POST /api/cars/remove`: remove a car from service (defect/rejection hold).
- `POST /api/pull-runs/{code}/advance`: execute the next pull actions.
- `POST /api/outbound-trains/{code}/depart`: mark an assembled train departed.
- `POST /api/shifts/{code}/close`: create a closure snapshot.
- `GET /api/yard`: return the full yard view.
- `GET /api/shifts/{code}`: return shift details and recent events.

The service listens on a local port chosen through `--port` or the
`SWITCHYARD_PORT` environment variable. Data is stored under `--data-dir` or
`SWITCHYARD_DATA_DIR`.

## Validation plan

Tests are intentionally deferred in this initialization baseline. A later
engineering task stage adds red/green unit tests for validation, transition
tables, allocation, pull sequencing, and atomic persistence. This baseline
still exposes production workflow checks under `checks/`; each check starts the
HTTP service, drives the public API with real requests, and verifies the
visible success state.

## Intentionally omitted

- Multi-yard networking, remote train-control feeds, and cloud storage.
- Authentication and user accounts.
- Live train-control hardware and telemetry.
- A web or desktop interface.
- Automatic test files in this baseline.

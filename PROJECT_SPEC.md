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

### 4. Close a shift with a yard balance

Entry: `POST /api/shifts/{code}/close`, `GET /api/yard`

The shift lead requests closure. The service computes standing, reserved,
assembled, and departed car totals plus track occupancy and open-train counts.
It blocks closure when any intake is open, any run is queued or running, or any
standing track is in maintenance with cars present. When the checks pass, it
stores a `ClosureSnapshot`, records the closure event, and exposes the yard
view for verification.

### 5. Forecast arrival capacity before submitting a train

Entry: `POST /api/arrival-forecast`,
`POST /api/tracks/{code}/arrangement`

Given known arrival times, the dispatcher asks whether the next inbound trains
can still be classified inside the current shift before any manifest is
submitted. The request carries one or more prospective trains (code with an
`FCST-` or `INT-` prefix, arrival time, and car attributes) plus an optional
shift horizon. The forecast merges them onto one timeline with already
received but unclassified intakes, ordered by arrival time (train code breaks
ties), and replays the same destination/hazard/capacity allocation that real
classification uses on a deep copy of the yard. It never persists a car,
intake, or plan.

Each train reports the placeable car count, a per-car blocked list with a
stable reason code (`TRACK_MAINTENANCE`, `TRACK_RESTRICTED`,
`TRANSFER_PURPOSE`, `HAZARD_TRACK_UNAVAILABLE`, `HAZARD_CAPACITY_FULL`,
`CAR_CAPACITY_FULL`, `LENGTH_CAPACITY_FULL`, `AFTER_SHIFT_HORIZON`, ...), and
the projected track for every spot. Track rows strictly separate
`current_*` occupancy (cars physically on the stack when the query runs)
from `planned_*` occupancy (cars that only exist in persisted open intakes,
tagged `PLANNED_PERSISTED`, or in the request itself, tagged
`PLANNED_FORECAST`). The report also lists track types with remaining
capacity and exhausted track types.

A separate arrangement command persists maintenance/restriction state and
general-to-transfer duty changes for a track. Empty tracks can leave
receiving service; tracks with cars or an active pull reservation are
rejected. Because arrangements are stored in the versioned workspace and
journaled, the same forecast returns the same result after a service restart.

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
- Closure is derived from the persisted workspace and never mutates car or
  track state.
- Arrival forecasts are derived from a copied workspace: they never create
  cars or intakes, never reserve capacity, and keep current occupancy separate
  from planned occupancy (`PLANNED_PERSISTED` versus `PLANNED_FORECAST`).
- Track arrangement changes (maintenance/restriction/transfer duty) are
  journaled workspace updates; empty tracks only can leave receiving service,
  and forecasts observe them consistently across service restarts.

## Modules and dependency direction

- `entry`: HTTP server, router, request parsing, and JSON envelopes.
- `service`: application context and workflow commands that coordinate domain,
  storage, and report modules.
- `domain`: enums, entities, validation, state transitions, allocation rules,
  pull sequencing, and domain errors.
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
- `POST /api/pull-runs/{code}/advance`: execute the next pull actions.
- `POST /api/outbound-trains/{code}/depart`: mark an assembled train departed.
- `POST /api/shifts/{code}/close`: create a closure snapshot.
- `POST /api/arrival-forecast`: read-only capacity forecast for prospective
  inbound trains; reports placeable counts, block reasons, current versus
  planned occupancy, and track types still free in the shift.
- `POST /api/tracks/{code}/arrangement`: persist a maintenance/restriction or
  transfer-duty arrangement change for a track.
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

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
- `DispatchTicket`: a queue-ordered registration at the pull dispatch board.
  It declares the source standing tracks, transfer bays (X1), blocker cars,
  and target cars a queued pull plan occupies, tracks a QUEUED -> CLAIMED ->
  RUNNING -> COMPLETED (or CANCELLED) lifecycle, and persists the execution
  token handed to the claiming client.

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

### 3a. Register plans at the pull dispatch board

Entry: `POST /api/outbound-trains/{code}/dispatch`, `GET /api/dispatch`,
`GET /api/dispatch/{ticket}`, `POST /api/dispatch/{ticket}/claim`,
`POST /api/dispatch/{ticket}/cancel`

Multiple planned pulls can queue together even though the yard has limited
standing tracks and one transfer bay X1. Each plan is registered as a
`DispatchTicket` in queue order. The ticket declares the source standing
tracks, the transfer bay (only when blocker cars must be buffered), the
individual blocker cars, and the reserved target cars. A ticket is eligible to
run only when no earlier active ticket holds an overlapping declared resource;
otherwise the board view reports each blocking ticket, the shared resources,
and the reason (`ahead-in-queue` versus `resource-held`), along with the
release actions (the RETURN/PULL step that hands each resource back).

A client claims an eligible ticket and receives an opaque claim token. The
same ticket cannot be claimed by two clients: another client is rejected with
`RESOURCE_BUSY`, a repeated claim carrying the original client id and token is
idempotent, and advancing the pull run requires the matching token. The queue,
resource declarations, and issued tokens are persisted, so a service restart
keeps the queue order and every execution right. Unstarted tickets (queued or
claimed but with no car moved) can be cancelled; cancellation marks the run
cancelled, returns the outbound to DRAFT, releases the target-car reservations
and declared resources, and unblocks later tickets. A RUNNING ticket cannot be
cancelled.

Registered steps are derived from the track stack at registration time and can
go stale when an earlier ticket pulls a car the later plan buffered as a
blocker (for example a reverse target order where the first ticket takes the
top car and a later ticket targets the bottom car). When a ticket is claimed,
and again defensively when its run starts, the service recomputes its steps
from the current stacks and refreshes the declared resources (a ticket that
needed X1 at registration may no longer buffer anything, or vice versa). The
ticket code, run code, owner, and claim token are unchanged by recomputation;
the change is recorded as a `DISPATCH_REPLANNED` event and signalled by the
`replanned` claim response field and the `plan_stale` board flag.

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
- Pull runs move from `queued` to `running`, then `completed`, `failed`, or
  `cancelled`.
- Dispatch tickets move from `queued` to `claimed`, then `running` and
  `completed`, or to `cancelled` before any car moves; their claim tokens are
  persisted and required for every advance.
- Dispatch board arbitration is queue ordered: an earlier active ticket that
  declares an overlapping source track, transfer bay, or blocker car blocks a
  later ticket from claiming, even when each plan is valid on its own.
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
- `POST /api/outbound-trains/{code}/dispatch`: register a draft at the dispatch
  board and derive its declared resources.
- `GET /api/dispatch`: return the queue order, tickets, blockers, release
  actions, and the per-resource holder view.
- `GET /api/dispatch/{ticket}`: return one ticket with eligibility and blockers.
- `POST /api/dispatch/{ticket}/claim`: acquire the exclusive execution token.
- `POST /api/dispatch/{ticket}/cancel`: cancel an unstarted ticket and release
  its dependencies.
- `POST /api/pull-runs/{code}/advance`: execute the next pull actions (a
  dispatch claim token is required for board-registered runs).
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
visible success state. `checks/wf_pull_dispatch.py` additionally restarts the
service against the same data directory to prove queue order, blocker state,
and claim tokens are recovered.

## Intentionally omitted

- Multi-yard networking, remote train-control feeds, and cloud storage.
- Authentication and user accounts.
- Live train-control hardware and telemetry.
- A web or desktop interface.
- Automatic test files in this baseline.

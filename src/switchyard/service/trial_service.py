"""Read-only pull plan trial commands.

A trial never commits: the live workspace is only loaded, deeply cloned, and
evaluated on the detached copy. No pull run codes, car reservations, outbound
states, or events are ever produced, so repeated trials cannot influence each
other or the formal plans.
"""

from __future__ import annotations

from typing import Any

from ..domain.errors import NotFoundError, ValidationError
from ..domain.trial import BASELINE_PLAN_STATES, choose_baseline, evaluate_pull_trial
from ..domain.validators import build_trial_payload
from ..storage.codec import clone_workspace
from .context import YardApplication


def run_pull_trial(app: YardApplication, payload: Any) -> dict[str, Any]:
    request = build_trial_payload(payload)
    workspace = app.load()
    baseline = None
    mode = request["baseline_mode"]
    if mode == "code":
        baseline = workspace.outbounds.get(request["baseline_code"])
        if baseline is None:
            raise NotFoundError("baseline outbound train", request["baseline_code"])
        if baseline.state not in BASELINE_PLAN_STATES:
            raise ValidationError(
                f"baseline outbound train {baseline.code} is {baseline.state.value}, not an active plan",
                **{"baseline_code": [f"current state is {baseline.state.value}"]},
            )
        if baseline.destination != request["destination"]:
            raise ValidationError(
                f"baseline outbound train {baseline.code} targets {baseline.destination}, "
                f"not {request['destination']}",
                **{"baseline_code": ["destination mismatch"]},
            )
    elif mode == "auto":
        baseline = choose_baseline(workspace, request["destination"])
    snapshot_version = workspace.version
    snapshot = clone_workspace(workspace)
    snapshot_baseline = None
    if baseline is not None:
        snapshot_baseline = snapshot.outbounds[baseline.code]
    result = evaluate_pull_trial(
        snapshot,
        candidate_code=request["candidate_code"],
        destination=request["destination"],
        car_codes=request["car_codes"],
        transfer_code=request["transfer_code"],
        baseline=snapshot_baseline,
        snapshot_version=snapshot_version,
    )
    return {"trial": result.to_dict()}


__all__ = ["run_pull_trial"]

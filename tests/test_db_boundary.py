from copy import deepcopy

import pytest

from sol_alerts.db_boundary import FakeDatabaseBoundary


def signal(key: str, direction: str = "long", trigger: int = 1_000, score: float = 90, shadow_only: bool = False) -> dict[str, object]:
    return {"state_key": key, "direction": direction, "trigger_time": trigger, "score": score, "shadow_only": shadow_only, "signal_idempotency": f"{key}:{trigger}"}


def test_boundary_rolls_back_all_mutations_on_durable_failure() -> None:
    boundary = FakeDatabaseBoundary()
    boundary.commit_scan(now=1_000, observed_state_keys={"BTC:long"}, signals=[signal("BTC:long")])
    before = deepcopy(boundary.state)
    with pytest.raises(RuntimeError):
        boundary.commit_scan(now=2_000, observed_state_keys=set(), signals=[signal("ETH:long", trigger=2_000)], fail=True)
    assert boundary.state == before


def test_boundary_invalidates_vanished_state_and_rearms_after_one_hour() -> None:
    boundary = FakeDatabaseBoundary()
    key = "BTC:long"
    boundary.commit_scan(now=1_000, observed_state_keys={key}, signals=[signal(key)])
    boundary.commit_scan(now=2_000, observed_state_keys=set(), signals=[])
    assert boundary.state.signal_states[key]["active"] is False
    assert boundary.rearm_allowed(key, 2_000 + 60 * 60_000) is True
    boundary.commit_scan(now=2_000 + 60 * 60_000, observed_state_keys={key}, signals=[signal(key, trigger=2_000 + 60 * 60_000)])
    assert boundary.state.signal_states[key]["active"] is True


def test_boundary_capacity_is_ranked_and_shadow_does_not_consume_formal_slots() -> None:
    boundary = FakeDatabaseBoundary()
    signals = [signal(f"S{index}:{direction}", direction, score=100 - index) for index, direction in enumerate(("long", "short", "long", "short", "long", "short", "long"))]
    signals.append(signal("SHADOW:long", "long", score=1, shadow_only=True))
    boundary.commit_scan(now=1_000, observed_state_keys={str(item["state_key"]) for item in signals}, signals=signals)
    formal = [position for position in boundary.state.positions if not position["shadow_only"]]
    assert len(formal) == 6
    assert sum(position["direction"] == "long" for position in formal) == 3
    assert sum(position["direction"] == "short" for position in formal) == 3
    assert any(position["shadow_only"] for position in boundary.state.positions)


def test_boundary_shadow_activation_is_single_and_can_rearm_as_formal() -> None:
    boundary = FakeDatabaseBoundary()
    key = "BTC:long"
    boundary.commit_scan(now=1_000, observed_state_keys={key}, signals=[signal(key, trigger=1_000, shadow_only=True)])
    boundary.commit_scan(now=2_000, observed_state_keys={key}, signals=[signal(key, trigger=2_000, shadow_only=True)])
    assert len(boundary.state.positions) == 1
    boundary.commit_scan(now=3_000, observed_state_keys=set(), signals=[])
    rearm_time = 3_000 + 60 * 60_000
    boundary.commit_scan(now=rearm_time, observed_state_keys={key}, signals=[signal(key, trigger=rearm_time, shadow_only=False)])
    assert len(boundary.state.positions) == 2
    assert boundary.state.positions[-1]["shadow_only"] is False


def test_boundary_outbox_is_idempotent() -> None:
    boundary = FakeDatabaseBoundary()
    payload = {"idempotency_key": "scan-email:1", "subject": "one"}
    boundary.commit_scan(now=1_000, observed_state_keys=set(), signals=[], email=payload)
    boundary.commit_scan(now=2_000, observed_state_keys=set(), signals=[], email=payload)
    assert list(boundary.state.outbox) == ["scan-email:1"]

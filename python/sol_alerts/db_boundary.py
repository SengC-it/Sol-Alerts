from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass


@dataclass
class BoundaryState:
    signal_states: dict[str, dict[str, object]]
    signals: dict[str, dict[str, object]]
    positions: list[dict[str, object]]
    outbox: dict[str, dict[str, object]]


class FakeDatabaseBoundary:
    """Small deterministic transaction harness for the Supabase RPC contract.

    It models rollback, state invalidation/cooldown, idempotent signal keys,
    formal capacity reservations, and one outbox row per idempotency key. It is
    intentionally not a Postgres replacement; real migration execution remains
    an operator check requiring external Supabase credentials.
    """

    def __init__(self) -> None:
        self.state = BoundaryState({}, {}, [], {})

    def commit_scan(self, *, now: int, observed_state_keys: set[str], signals: list[dict[str, object]], email: dict[str, object] | None = None, fail: bool = False) -> None:
        snapshot = deepcopy(self.state)
        try:
            for key, state in self.state.signal_states.items():
                if state.get("active") and key not in observed_state_keys:
                    state.update({"active": False, "last_invalidated_at": now, "cooldown_until": now + 60 * 60_000})
            total = sum(1 for position in self.state.positions if position["status"] in {"pending", "open"} and not position.get("shadow_only", False))
            long_count = sum(1 for position in self.state.positions if position["status"] in {"pending", "open"} and not position.get("shadow_only", False) and position["direction"] == "long")
            short_count = sum(1 for position in self.state.positions if position["status"] in {"pending", "open"} and not position.get("shadow_only", False) and position["direction"] == "short")
            for signal in sorted(signals, key=lambda item: (-float(item.get("score", 0)), str(item["state_key"]))):
                key = str(signal["state_key"])
                signal_idempotency = f"{key}:{signal['trigger_time']}"
                state = self.state.signal_states.setdefault(key, {"active": False, "shadow_only": False, "cooldown_until": None})
                if state.get("active"):
                    # Formal and shadow setups share one activation state. A
                    # repeated setup is observed by the scan, but it does not
                    # create another position/sample until it disappears and
                    # the persisted cooldown has elapsed.
                    continue
                shadow = bool(signal.get("shadow_only", True))
                cooldown = state.get("cooldown_until")
                if cooldown is not None and int(cooldown) > now:
                    continue
                self.state.signals.setdefault(signal_idempotency, dict(signal))
                state.update({"active": True, "shadow_only": shadow, "last_triggered_at": signal["trigger_time"]})
                if signal_idempotency in {str(position.get("signal_idempotency")) for position in self.state.positions}:
                    continue
                if shadow:
                    self.state.positions.append({"signal_idempotency": signal_idempotency, "direction": signal["direction"], "status": "shadow", "shadow_only": True})
                elif total < 6 and (signal["direction"] == "long" and long_count < 3 or signal["direction"] == "short" and short_count < 3):
                    self.state.positions.append({"signal_idempotency": signal_idempotency, "direction": signal["direction"], "status": "pending", "shadow_only": False})
                    total += 1
                    if signal["direction"] == "long":
                        long_count += 1
                    else:
                        short_count += 1
            if email is not None:
                key = str(email["idempotency_key"])
                self.state.outbox.setdefault(key, dict(email))
            if fail:
                raise RuntimeError("injected transaction failure")
        except Exception:
            self.state = snapshot
            raise

    def rearm_allowed(self, key: str, now: int) -> bool:
        state = self.state.signal_states.get(key)
        return bool(state and not state.get("active") and int(state.get("cooldown_until") or 0) <= now)

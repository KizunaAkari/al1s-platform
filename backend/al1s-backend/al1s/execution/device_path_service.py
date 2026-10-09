"""Path observations are facts; ownership changes require physical release."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from al1s.execution.device_authority import DevicePath, choose_path, handoff_allowed
from al1s.execution.errors import ConflictError, NotFoundError


@dataclass(frozen=True)
class PathObservation:
    device_id: UUID
    instance_id: UUID
    control_ready: bool
    release_generation: int | None = None
    released_previous_instance_id: UUID | None = None


@dataclass
class PathState:
    device_id: UUID
    holder: UUID | None
    holder_instance: UUID | None
    requested: UUID | None
    generation: int
    state: str
    paths: list[DevicePath]
    pending_control: bool
    editor_open: bool = False
    require_ocr: bool = False


class PathTransaction(Protocol):
    def __enter__(self) -> Self: ...
    def __exit__(
        self,
        kind: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...
    def observe(
        self, terminal: UUID, items: list[PathObservation], now: datetime
    ) -> list[PathState]: ...
    def save(self, states: list[PathState], now: datetime) -> None: ...
    def view(self, device: UUID, now: datetime) -> dict[str, object]: ...
    def load(self, devices: list[UUID], now: datetime) -> list[PathState]: ...


class DevicePathService:
    def __init__(self, transactions: Callable[[], PathTransaction]) -> None:
        self._transactions = transactions

    def view(self, device: UUID) -> dict[str, object]:
        with self._transactions() as tx:
            return tx.view(device, datetime.now(UTC))

    def request_handoff(
        self, device: UUID, target: UUID, expected_generation: int
    ) -> dict[str, object]:
        """Request the policy-qualified path; retain the old holder until physical release."""
        now = datetime.now(UTC)
        with self._transactions() as tx:
            states = tx.load([device], now)
            if not states:
                raise NotFoundError("target_device")
            state = states[0]
            if state.generation != expected_generation:
                raise ConflictError(
                    "stale_path_generation", "Refresh the current path before handoff"
                )
            candidate = choose_path(state.paths, {"ocr"} if state.require_ocr else set(), now)
            if candidate is None or candidate.terminal_id != target:
                raise ConflictError(
                    "path_not_preferred", "Target must be the fresh policy-qualified path"
                )
            if state.state == "active" and state.holder == target:
                return tx.view(device, now)
            if state.state == "releasing" and state.requested == target:
                return tx.view(device, now)
            if state.pending_control:
                raise ConflictError(
                    "outstanding_control", "Finish existing work and offline permits first"
                )
            old = next((p for p in state.paths if p.terminal_id == state.holder), None)
            if state.state != "active" or old is None or old.authority_protocol != 1:
                raise ConflictError(
                    "physical_release_required", "Current holder must support physical release"
                )
            state.state, state.requested = "releasing", target
            tx.save(states, now)
            return tx.view(device, now)

    def qualify(self, requirements: dict[UUID, set[str]]) -> None:
        if not requirements:
            return
        if len(requirements) > 100:
            raise ValueError("path qualification batch too large")
        now = datetime.now(UTC)
        with self._transactions() as tx:
            states = tx.load(sorted(requirements, key=str), now)
            for state in states:
                state.require_ocr = "ocr" in requirements[state.device_id]
                candidate = choose_path(state.paths, requirements[state.device_id], now)
                old = next((p for p in state.paths if p.terminal_id == state.holder), None)
                if (
                    candidate
                    and candidate.terminal_id != state.holder
                    and not state.pending_control
                    and old
                    and old.authority_protocol == 1
                ):
                    state.state = "releasing"
                    state.requested = candidate.terminal_id
            tx.save(states, now)

    def observe(self, terminal: UUID, items: list[PathObservation]) -> list[dict[str, object]]:
        if not 1 <= len(items) <= 100 or len({i.device_id for i in items}) != len(items):
            raise ConflictError("invalid_path_batch", "Unique device observations required")
        now = datetime.now(UTC)
        by_device = {i.device_id: i for i in items}
        with self._transactions() as tx:
            states = tx.observe(terminal, items, now)
            for state in states:
                self._advance(state, by_device[state.device_id], terminal, now)
            tx.save(states, now)
        return [
            {
                "device_id": str(s.device_id),
                "generation": s.generation,
                "state": s.state,
                "holder_terminal_id": str(s.holder) if s.holder else None,
                "holder_instance_id": str(s.holder_instance) if s.holder_instance else None,
                "input_granted": s.state == "active"
                and s.holder == terminal
                and s.holder_instance == by_device[s.device_id].instance_id
                and choose_path([p for p in s.paths if p.terminal_id == terminal], set(), now)
                is not None,
                "release_requested": s.state == "releasing" and s.holder == terminal,
                "blocked_reason": "outstanding_control"
                if s.pending_control
                else ("physical_release_required" if s.state == "releasing" else None),
            }
            for s in states
        ]

    @staticmethod
    def _advance(
        state: PathState, observation: PathObservation, terminal: UUID, now: datetime
    ) -> None:
        item = observation
        if state.holder == terminal and state.holder_instance is None:
            state.holder_instance = item.instance_id
        replaced = (
            state.holder == terminal
            and state.holder_instance is not None
            and state.holder_instance != item.instance_id
            and item.released_previous_instance_id == state.holder_instance
            and item.release_generation == state.generation
        )
        if (
            replaced
            and state.state == "active"
            and not state.pending_control
            and not state.editor_open
        ):
            state.holder_instance = item.instance_id
            state.generation += 1
        acknowledged = (
            state.holder == terminal
            and state.state == "releasing"
            and (state.holder_instance == item.instance_id or replaced)
            and item.release_generation == state.generation
        )
        if acknowledged and handoff_allowed(
            physical_release=True,
            unsettled_work=state.pending_control,
            editor_open=state.editor_open,
        ):
            state.state = "released"
        candidate = choose_path(state.paths, {"ocr"} if state.require_ocr else set(), now)
        if candidate is None:
            return
        if state.state == "released" or state.holder is None:
            state.holder = candidate.terminal_id
            state.holder_instance = candidate.instance_id
            state.generation += 1
            state.state = "active"
            state.requested = None
        elif candidate.terminal_id != state.holder and not state.pending_control:
            # The old holder must support release. Legacy endpoints remain owners.
            old = next((p for p in state.paths if p.terminal_id == state.holder), None)
            if old is not None and old.authority_protocol == 1:
                state.state = "releasing"
                state.requested = candidate.terminal_id

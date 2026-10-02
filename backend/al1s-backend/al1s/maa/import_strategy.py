"""Create the archive's strategy in the same transaction as its script assets."""

from datetime import datetime
from uuid import UUID

from al1s.maa.archive import ParsedScriptArchive
from al1s.maa.ports import MaaUnitOfWork
from al1s.maa.strategy_service import MaaStrategyService
from al1s.maa.types import ProcessModuleInput


def activate_strategy(
    uow: MaaUnitOfWork, service: MaaStrategyService, parsed: ParsedScriptArchive,
    identities: dict[int, UUID], previous_id: UUID | None,
    correlation_id: UUID, now: datetime,
) -> UUID | None:
    if parsed.strategy is None:
        return None
    if previous_id is not None and uow.strategies.get_active(previous_id) is not None:
        return previous_id
    sources = {s.source_script_id: identities[s.ordinal] for s in parsed.scripts}
    modules = parsed.strategy["modules"]
    start = sources[modules[0]["source_script_id"]]
    script = uow.scripts.get_active(start)
    assert script is not None
    result = service.create_strategy_in_uow(
        uow, application_id=script.application_id, name=parsed.strategy["name"],
        start_script_id=start, end_script_id=sources[modules[-1]["source_script_id"]],
        start_wait_after_ms=modules[0]["wait_after_ms"],
        process_modules=[ProcessModuleInput(sources[m["source_script_id"]], m["wait_after_ms"])
                         for m in modules[1:-1]],
        default_parameters=parsed.strategy["default_parameters"],
        correlation_id=correlation_id, now=now,
    )
    return result.strategy.strategy_id

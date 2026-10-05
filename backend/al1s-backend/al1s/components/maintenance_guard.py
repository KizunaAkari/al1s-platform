from uuid import UUID


def admit_new_work(uow: object, terminal: UUID | None = None, device: UUID | None = None) -> None:
    # Legacy in-memory test adapters have no SQL guard; all production SQL UoWs install it.
    guard = getattr(uow, "component_admission", None)
    if guard is not None:
        guard.admit(terminal, device)


def platform_accepts_work(uow: object) -> bool:
    guard = getattr(uow, "component_admission", None)
    return guard.platform_open() if guard is not None else True

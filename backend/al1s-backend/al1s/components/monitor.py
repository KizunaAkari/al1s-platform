from threading import Event, Thread

import structlog

from al1s.components.operations import ComponentUpgradeService


def start_monitor(service: ComponentUpgradeService, stop: Event) -> Thread:
    def observe() -> None:
        while not stop.is_set():
            try:
                for operation in service.store.active_operations():
                    if stop.is_set():
                        return
                    try:
                        service.observe(operation.operation_id)
                    except Exception as error:
                        structlog.get_logger().warning(
                            "component_observation_unavailable", error=type(error).__name__
                        )
            except Exception as error:
                structlog.get_logger().warning(
                    "component_monitor_unavailable", error=type(error).__name__
                )
            stop.wait(2)

    thread = Thread(target=observe, name="component-observation", daemon=True)
    thread.start()
    return thread

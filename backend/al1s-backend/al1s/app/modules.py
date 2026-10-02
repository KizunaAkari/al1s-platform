from dataclasses import dataclass


@dataclass(frozen=True)
class ModuleDescriptor:
    id: str
    title: str
    routes: tuple[str, ...]
    data_owner: str
    ui_complete: bool


class ModuleRegistry:
    def __init__(self) -> None:
        self._modules: dict[str, ModuleDescriptor] = {}

    def register(self, module: ModuleDescriptor) -> None:
        if module.id in self._modules:
            raise ValueError(f'Duplicate module: {module.id}')
        self._modules[module.id] = module

    def entries(self) -> tuple[ModuleDescriptor, ...]:
        return tuple(self._modules.values())


def platform_modules() -> ModuleRegistry:
    registry = ModuleRegistry()
    registry.register(ModuleDescriptor(
        'platform', '平台基础', ('/', '/maintenance'), 'platform', False,
    ))
    registry.register(ModuleDescriptor(
        'maa', 'Maa自动化', ('/terminals', '/tasks', '/scripts', '/editor'), 'maa', False,
    ))
    registry.register(ModuleDescriptor(
        'information', '信息管理', ('/notifications', '/bots'), 'information', False,
    ))
    registry.register(ModuleDescriptor(
        'lineup', '阵容识别', ('/lineup',), 'lineup', True,
    ))
    return registry

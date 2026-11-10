import logging
import re

from .cache import OwnedCache
from .guard import JobContext, install_executor_guard, install_memory_guard
from .inspection import AssetHashCache, inspect_assets, inspect_dependencies
from .nodes import build_nodes
from .registry import ResourceRegistry
from .routes import build_handlers, install_routes
from .safety import check_files
from .scheduler import ReleaseScheduler, install_queue_hook

_REQUIRED = {"CheckpointLoaderSimple", "LoraLoader", "CLIPSetLastLayer", "UpscaleModelLoader",
             "ImageReceiver", "SaveImage", "WD14Tagger|pysssss", "AILab_QwenVL_Advanced", "ShowText|pysssss"}
_WRAPPERS = {"AL1SCheckpointLoader", "AL1SLoraLoader", "AL1SCLIPSetLastLayer", "AL1SUpscaleModelLoader",
             "AL1SImageReceiver", "AL1SSaveImage", "AL1SWD14Tagger", "AL1SQwenVLAdvanced", "AL1SInvalidRequest"}


def prepare_extension(server, nodes, execution, memory, folder_paths, root, token, *, web, preflight=check_files):
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
        raise ValueError("resource_token_invalid")
    registry, cache, context = ResourceRegistry(), OwnedCache(), JobContext()
    asset_cache = AssetHashCache()
    queue = server.prompt_queue
    scheduler = ReleaseScheduler(registry, queue, cache=cache)
    state = {"ready": False}

    def inspect_callback(names, assets):
        return inspect_dependencies(root, nodes.NODE_CLASS_MAPPINGS, folder_paths, names, assets, cache=asset_cache)

    def verify_asset(kind, name):
        return inspect_assets(folder_paths, [{"kind": kind, "name": name}], cache=asset_cache)[0]["sha256"]

    def metrics():
        try:
            device = memory.get_torch_device()
            if device.type == "cuda":
                return {"peak_allocated_bytes": int(memory.torch.cuda.max_memory_allocated(device))}
        except (AttributeError, RuntimeError, ValueError):
            pass
        return {"peak_allocated_bytes": None}

    handlers = build_handlers(registry, queue, scheduler, token=token, web=web,
        output_root=folder_paths.get_output_directory(), inspect_callback=inspect_callback,
        ready=lambda: state["ready"], metrics=metrics, cache=cache)
    install_routes(server.routes, handlers)

    async def startup(_app):
        old_free, old_load = memory.free_memory, memory.load_models_gpu
        old_execute = execution.PromptExecutor.execute
        old_flags = getattr(queue, "get_flags", None)
        installed = set()
        try:
            preflight(root)
            if not _REQUIRED <= set(nodes.NODE_CLASS_MAPPINGS):
                raise ValueError("resource_required_node_missing")
            inspect_callback(sorted(_REQUIRED), [])
            wrappers = build_nodes(registry, nodes.NODE_CLASS_MAPPINGS,
                                   current_job=context.current, folder_paths=folder_paths, verify_asset=verify_asset)
            if not _WRAPPERS <= set(wrappers) or set(wrappers) & set(nodes.NODE_CLASS_MAPPINGS):
                raise ValueError("resource_node_conflict")
            with queue.mutex:
                if queue.queue or queue.currently_running:
                    raise ValueError("resource_queue_not_empty")
                install_memory_guard(memory, registry, current_job=context.current)
                install_executor_guard(execution, registry, cache, context)
                install_queue_hook(queue, registry, cache, scheduler)
                nodes.NODE_CLASS_MAPPINGS.update(wrappers)
                installed = set(wrappers)
                state["ready"] = True
        except Exception as error:
            memory.free_memory, memory.load_models_gpu = old_free, old_load
            execution.PromptExecutor.execute = old_execute
            if old_flags is not None:
                queue.get_flags = old_flags
            for name in installed:
                nodes.NODE_CLASS_MAPPINGS.pop(name, None)
            logging.warning("AL1S Comfy resource extension unavailable: %s", str(error)[:100])

    async def cleanup(_app):
        state["ready"] = False
        scheduler.close()

    server.app.on_startup.append(startup)
    server.app.on_cleanup.append(cleanup)
    return state

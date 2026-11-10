import hashlib
import json
import threading


class JobContext:
    def __init__(self):
        self._local = threading.local()

    def current(self):
        return getattr(self._local, "value", None)

    def set(self, value):
        self._local.value = value


def install_executor_guard(execution, registry, cache, context):
    original = execution.PromptExecutor.execute
    if getattr(original, "_al1s_guard", False):
        raise ValueError("resource_guard_already_installed")

    def guarded(executor, prompt, prompt_id, extra_data=None, execute_outputs=None):
        try:
            record = registry.get(prompt_id)
        except ValueError:
            record = None
        valid = False
        if record is not None:
            try:
                encoded = json.dumps(prompt, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()
                digest = hashlib.sha256(encoded).hexdigest()
                valid = (len(encoded) <= 12 * 1024 * 1024 + 256 * 1024
                         and record["state"] == "started" and record["execution_hash"] == digest
                         and isinstance(extra_data, dict) and extra_data.get("client_id") == record["owner"])
            except (TypeError, ValueError, UnicodeError):
                valid = False
            if not valid:
                prompt = {"0": {"class_type": "AL1SInvalidRequest", "inputs": {}}}
                execute_outputs = ["0"]
            else:
                context.set((prompt_id, digest))
        try:
            return original(executor, prompt, prompt_id, extra_data or {}, execute_outputs or [])
        finally:
            if valid:
                try:
                    cache.capture(prompt_id, executor.caches.outputs)
                except Exception:
                    cache.mark_unsafe(prompt_id)
            context.set(None)

    guarded._al1s_guard = True
    execution.PromptExecutor.execute = guarded


def install_memory_guard(memory, registry, *, current_job):
    original_free = memory.free_memory
    original_load = memory.load_models_gpu
    if getattr(original_free, "_al1s_guard", False) or getattr(original_load, "_al1s_guard", False):
        raise ValueError("resource_guard_already_installed")

    def check():
        current = current_job()
        if current is None:
            return False
        if not isinstance(current, tuple) or len(current) != 2:
            raise ValueError("resource_job_conflict")
        registry.require_started(current[0], current[1])
        return True

    def guarded_free(memory_required, device, keep_loaded=None, for_dynamic=False, pins_required=0, ram_required=0):
        keep = list(keep_loaded or [])
        managed = check()
        protect_owned = registry.protect_owned()
        for loaded in memory.current_loaded_models:
            if ((managed or protect_owned and registry.owns_value(loaded.model))
                    and all(loaded is not item for item in keep)):
                keep.append(loaded)
        return original_free(memory_required, device, keep_loaded=keep, for_dynamic=for_dynamic,
                             pins_required=pins_required, ram_required=ram_required)

    def guarded_load(models, *args, **kwargs):
        managed = check()
        protect_owned = registry.protect_owned()
        for requested in models:
            dependencies = getattr(requested, "model_patches_models", lambda: [])()
            for candidate in (requested, *dependencies):
                for loaded in memory.current_loaded_models:
                    other = loaded.model
                    if other is None or not candidate.is_clone(other):
                        continue
                    owned = registry.owns_value(other)
                    if managed and not owned:
                        raise ValueError("resource_external_clone_conflict")
                    if not managed and protect_owned and owned:
                        raise ValueError("resource_managed_clone_conflict")
        return original_load(models, *args, **kwargs)

    guarded_free._al1s_guard = True
    guarded_load._al1s_guard = True
    memory.free_memory, memory.load_models_gpu = guarded_free, guarded_load

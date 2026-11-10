import gc
import threading


class ReleaseScheduler:
    def __init__(self, registry, queue, *, cache=None, timer_factory=threading.Timer):
        self.registry, self.queue, self.timer_factory = registry, queue, timer_factory
        self.cache = cache
        self._lock, self._timer, self._generation = threading.Lock(), None, 0
        self.last_status = "idle"

    def _schedule(self, delay):
        with self._lock:
            self._generation += 1
            generation = self._generation
            if self._timer is not None:
                self._timer.cancel()
            if delay is None:
                self._timer = None
                return
            timer = self.timer_factory(delay, lambda: self._wake(generation))
            timer.daemon = True
            self._timer = timer
            timer.start()

    def refresh(self):
        if self.cache is not None and any(self.cache.pending(prompt) for prompt in self.registry.released_prompts()):
            self.queue.set_flag("al1s_cache_release", True)
        vision = self.registry.vision_due()
        if vision is not None:
            self.queue.set_flag("al1s_vision_release", vision)
        self._schedule(self.registry.next_delay())

    def retry(self):
        self._schedule(5.0)

    def _wake(self, generation):
        with self._lock:
            if generation != self._generation:
                return
            self._timer = None
        token = self.registry.due()
        if token is not None:
            self.queue.set_flag("al1s_idle_release", token)
        else:
            self.refresh()

    def close(self):
        self._schedule(None)


def install_queue_hook(queue, registry, cache, scheduler):
    original = queue.get_flags
    if getattr(original, "_al1s_guard", False):
        raise ValueError("resource_guard_already_installed")

    def get_flags(reset=True):
        flags = original(reset=reset)
        if not reset:
            return flags
        if flags.pop("al1s_cache_release", False):
            with queue.mutex:
                busy = bool(queue.queue or queue.currently_running)
                if busy or not all(cache.release(prompt) for prompt in registry.released_prompts()
                                   if cache.pending(prompt)):
                    scheduler.retry()
        switch_token = flags.pop("al1s_switch", None)
        if switch_token is not None:
            with queue.mutex:
                busy = bool(queue.queue or queue.currently_running)
                safe = not busy and all(cache.release(prompt) for prompt in registry.released_prompts())
                scheduler.last_status = registry.reclaim_switch(switch_token, external_busy=not safe,
                                                                 release=lambda _value: True)
                if scheduler.last_status != "deferred":
                    gc.collect()
        vision_token = flags.pop("al1s_vision_release", None)
        if vision_token is not None:
            with queue.mutex:
                busy = bool(queue.queue or queue.currently_running)
                safe = not busy and all(cache.release(prompt) for prompt in registry.released_prompts("vision"))
                status = registry.reclaim_vision(vision_token, external_busy=not safe, release=lambda _value: True)
                if status == "deferred" and registry.vision_due() is not None:
                    scheduler.retry()
                elif status == "released":
                    gc.collect()
        if "al1s_idle_release" not in flags:
            return flags
        token = flags.pop("al1s_idle_release")
        with queue.mutex:
            busy = bool(queue.queue or queue.currently_running)
            if not registry.may_release(token, external_busy=busy):
                scheduler.last_status = "deferred"
                if registry.due() is not None:
                    scheduler.retry()
                else:
                    scheduler.refresh()
                return flags
            for prompt in registry.released_prompts():
                if not cache.release(prompt):
                    scheduler.last_status = "deferred"
                    scheduler.retry()
                    return flags
            scheduler.last_status = registry.reclaim(token, external_busy=False, release=lambda _value: True)
            if scheduler.last_status == "deferred":
                scheduler.retry()
            else:
                gc.collect()
        return flags

    get_flags._al1s_guard = True
    queue.get_flags = get_flags

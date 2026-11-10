import weakref


def _contains_marker(value, prompt, depth=0):
    if depth > 120:
        return False
    if isinstance(value, tuple) and value == ("al1s_prompt_id", prompt):
        return True
    if isinstance(value, frozenset) and {(0, "al1s_prompt_id"), (1, prompt)} <= value:
        return True
    if isinstance(value, (tuple, frozenset, list)):
        return any(_contains_marker(item, prompt, depth + 1) for item in value)
    return False


class OwnedCache:
    def __init__(self):
        self._entries = {}
        self._unsafe = set()

    def mark_unsafe(self, prompt):
        self._unsafe.add(prompt)

    def pending(self, prompt):
        return prompt in self._entries or prompt in self._unsafe

    def capture(self, prompt, cache):
        if not isinstance(getattr(cache, "cache", None), dict) or getattr(cache, "subcaches", None):
            self._unsafe.add(prompt)
            return
        keys = getattr(getattr(cache, "cache_key_set", None), "keys", None)
        if not isinstance(keys, dict):
            self._unsafe.add(prompt)
            return
        captured = []
        for key in keys.values():
            if _contains_marker(key, prompt) and key in cache.cache:
                captured.append((weakref.ref(cache), key, cache.cache[key]))
        if not captured:
            self._unsafe.add(prompt)
        else:
            self._entries[prompt] = captured

    def release(self, prompt):
        if prompt in self._unsafe:
            return False
        entries = self._entries.get(prompt, [])
        for reference, key, value in entries:
            cache = reference()
            if cache is not None and key in cache.cache and cache.cache[key] is not value:
                return False
        for reference, key, value in entries:
            cache = reference()
            if cache is not None and cache.cache.get(key) is value:
                del cache.cache[key]
        self._entries.pop(prompt, None)
        return True

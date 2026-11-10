import re
import threading
import time
from uuid import UUID, uuid4


def _uuid(value):
    try:
        result = str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("resource_identity_invalid") from None
    if result != value:
        raise ValueError("resource_identity_invalid")
    return result


class ResourceRegistry:
    def __init__(self, *, clock=time.monotonic, boot_id=None):
        self.clock = clock
        self.boot_id = boot_id or str(uuid4())
        self._lock = threading.RLock()
        self._jobs, self._retention, self._pins, self._automatic = {}, {}, {}, {}
        self._resources, self._pin_resources = {}, {}
        self._activity_seq = 0
        self._idle_since = None
        self._last_reclaimed = None
        self._last_vision_reclaimed = None
        self._manual_release = None
        self._draw_profile = None

    def _set_policy(self, table, service, revision, values):
        _uuid(service)
        if type(revision) is not int or revision < 1 or any(type(value) is not bool for value in values):
            raise ValueError("resource_policy_invalid")
        previous = table.get(service)
        if previous and (revision < previous[0] or revision == previous[0] and values != previous[1]):
            raise ValueError("resource_revision_conflict")
        if previous != (revision, values):
            table[service] = (revision, values)
            self._activity_seq += 1

    def set_retention(self, service, *, revision, enabled, keep_warm):
        with self._lock:
            self._set_policy(self._retention, service, revision, (enabled, keep_warm))

    def set_pin(self, service, *, revision, enabled):
        with self._lock:
            self._set_policy(self._pins, service, revision, (enabled,))

    def set_automatic(self, service, *, revision, enabled):
        with self._lock:
            retention = self._retention.get(service)
            if retention is None or retention[0] != revision:
                raise ValueError("resource_revision_conflict")
            self._set_policy(self._automatic, service, revision, (enabled,))

    def _automatic_for(self, service, revision):
        return self._automatic.get(service) == (revision, (True,))

    def _resident(self):
        return any(enabled and warm and not self._automatic_for(service, revision)
                   for service, (revision, (enabled, warm)) in self._retention.items())

    def _idle_timeout(self):
        active = [(service, revision) for service, (revision, (enabled, _warm)) in self._retention.items()
                  if enabled]
        if active:
            return 60 if all(self._automatic_for(service, revision) for service, revision in active) else 600
        return 60 if any(values == (True,) for _revision, values in self._automatic.values()) else 600

    def register(self, prompt, service, owner, operation, execution_hash, boot_id, assets=()):
        _uuid(prompt)
        _uuid(service)
        if (not isinstance(owner, str) or not owner or len(owner) > 128 or operation not in {"draw", "tag", "vision"}
                or not isinstance(execution_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", execution_hash)):
            raise ValueError("resource_job_invalid")
        with self._lock:
            if boot_id != self.boot_id:
                raise ValueError("resource_boot_changed")
            if not isinstance(assets, (tuple, list)) or len(assets) > 100:
                raise ValueError("resource_job_invalid")
            frozen_assets = []
            for asset in assets:
                if (not isinstance(asset, dict) or set(asset) != {"kind", "name", "size", "sha256"}
                        or not isinstance(asset["kind"], str) or not isinstance(asset["name"], str)
                        or type(asset["size"]) is not int or asset["size"] < 0
                        or not isinstance(asset["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", asset["sha256"])):
                    raise ValueError("resource_job_invalid")
                frozen_assets.append((asset["kind"], asset["name"], asset["size"], asset["sha256"]))
            identity = (service, owner, operation, execution_hash)
            previous = self._jobs.get(prompt)
            if previous is not None:
                if previous["identity"] != identity or previous["assets"] != tuple(frozen_assets):
                    raise ValueError("resource_job_conflict")
                return self._view(prompt, previous)
            self._jobs[prompt] = {"identity": identity, "assets": tuple(frozen_assets), "state": "registered"}
            self._activity_seq += 1
            if operation == "draw":
                self._idle_since = None
            return self._view(prompt, self._jobs[prompt])

    def _view(self, prompt, row):
        service, owner, operation, execution_hash = row["identity"]
        return {"prompt_id": prompt, "service_id": service, "owner": owner,
                "operation": operation, "execution_hash": execution_hash, "state": row["state"],
                "boot_id": self.boot_id}

    def get(self, prompt):
        _uuid(prompt)
        with self._lock:
            row = self._jobs.get(prompt)
            return self._view(prompt, row) if row else None

    def begin(self, prompt, service, owner, operation, execution_hash):
        with self._lock:
            row = self._jobs.get(prompt)
            if row is None or row["identity"] != (service, owner, operation, execution_hash):
                raise ValueError("resource_job_conflict")
            if row["state"] != "registered":
                return False
            row["state"] = "started"
            return True

    def mark_dequeued(self, prompt):
        with self._lock:
            row = self._jobs.get(prompt)
            if row is not None and row["state"] in {"dequeued", "released"}:
                return
            if row is None or row["state"] != "started":
                raise ValueError("resource_job_conflict")
            row["state"] = "dequeued"

    def require_started(self, prompt, execution_hash=None):
        with self._lock:
            row = self._jobs.get(prompt)
            if (row is None or row["state"] != "started"
                    or execution_hash is not None and row["identity"][3] != execution_hash):
                raise ValueError("resource_job_not_started")
            return self._view(prompt, row)

    def asset_hash(self, prompt, kind, name):
        with self._lock:
            self.require_started(prompt)
            matches = [item for item in self._jobs[prompt]["assets"] if item[:2] == (kind, name)]
            if len(matches) != 1:
                raise ValueError("resource_asset_unregistered")
            return matches[0][3]

    def key_for_value(self, value):
        with self._lock:
            for key, entry in self._resources.items():
                values = entry["value"] if isinstance(entry["value"], tuple) else (entry["value"],)
                if any(item is value for item in values):
                    return key
            raise ValueError("resource_parent_unknown")

    def owns_value(self, value):
        if value is None:
            return False
        with self._lock:
            for entry in self._resources.values():
                values = entry["value"] if isinstance(entry["value"], tuple) else (entry["value"],)
                if any(item is value or getattr(item, "patcher", None) is value for item in values):
                    return True
            return False

    def protect_owned(self):
        with self._lock:
            return bool(self._resources and (self._resident() or self._idle_since is not None
                        or any(row["state"] != "released" for row in self._jobs.values())))

    def release(self, prompt, *, native_terminal):
        with self._lock:
            row = self._jobs.get(prompt)
            if row is None:
                raise ValueError("resource_job_missing")
            if row["state"] == "released":
                return "retained"
            if row["state"] == "started" and native_terminal is not True:
                return "deferred"
            row["state"] = "released"
            if row["identity"][2] == "draw" and not any(
                    item["state"] != "released" and item["identity"][2] == "draw"
                    for item in self._jobs.values()):
                self._idle_since = self.clock()
            return "retained"

    def due(self):
        with self._lock:
            if (self._resident() or self._idle_since is None or self.clock() - self._idle_since < self._idle_timeout()
                    or any(row["state"] != "released" and row["identity"][2] == "draw"
                           for row in self._jobs.values())):
                return None
            token = (self.boot_id, self._activity_seq)
            return None if token == self._last_reclaimed else token

    def next_delay(self):
        with self._lock:
            if (self._resident() or self._idle_since is None
                    or any(row["state"] != "released" and row["identity"][2] == "draw"
                           for row in self._jobs.values())
                    or (self.boot_id, self._activity_seq) == self._last_reclaimed):
                return None
            return max(0.0, self._idle_timeout() - (self.clock() - self._idle_since))

    def released_prompts(self, operation=None):
        with self._lock:
            return tuple(prompt for prompt, row in self._jobs.items() if row["state"] == "released"
                         and (operation is None or row["identity"][2] == operation))

    def may_release(self, token, *, external_busy):
        with self._lock:
            manual = (token == self._manual_release and token is not None
                      and token[:2] == (self.boot_id, self._activity_seq))
            return bool(not external_busy and (manual or token == self.due() and token is not None)
                        and not any(row["state"] != "released" for row in self._jobs.values()))

    def request_manual_release(self, service):
        _uuid(service)
        with self._lock:
            if service not in self._retention:
                raise ValueError('resource_policy_missing')
            if any(row['state'] != 'released' for row in self._jobs.values()):
                return None
            self._activity_seq += 1
            self._manual_release = (self.boot_id, self._activity_seq, service)
            return self._manual_release

    def _switch_candidates(self, operation, profile):
        kinds = {"vision"} if operation == "draw" else {"draw"}
        if operation == "draw" and self._draw_profile not in {None, profile}:
            kinds.add("draw")
        candidates = {key for key, row in self._resources.items()
                      if row["kinds"] <= kinds and not row["external"]}
        protected = set(self._resources) - candidates
        stack = list(protected)
        while stack:
            key = stack.pop()
            for parent in self._resources[key]["parents"]:
                if parent not in protected:
                    protected.add(parent)
                    stack.append(parent)
        return candidates - protected

    def request_switch(self, operation, profile):
        if (operation not in {"draw", "vision"} or operation == "vision" and profile is not None
                or operation == "draw" and (not isinstance(profile, str) or not 1 <= len(profile) <= 128)):
            raise ValueError("resource_switch_invalid")
        with self._lock:
            if self._resident() or any(row["state"] == "started" for row in self._jobs.values()):
                return "deferred"
            if not self._switch_candidates(operation, profile):
                if operation == "draw" and self._draw_profile != profile:
                    self._draw_profile = profile
                    self._activity_seq += 1
                return None
            return (self.boot_id, self._activity_seq, operation, profile)

    def reclaim_switch(self, token, *, external_busy, release):
        with self._lock:
            if (external_busy or not isinstance(token, tuple) or len(token) != 4
                    or token[:2] != (self.boot_id, self._activity_seq) or self._resident()
                    or any(row["state"] == "started" for row in self._jobs.values())):
                return "deferred"
            operation, profile = token[2:]
            candidates = self._switch_candidates(operation, profile)

            def depth(key):
                parents = self._resources[key]["parents"]
                return 1 + max((depth(parent) for parent in parents), default=0)

            for key in sorted(candidates, key=depth, reverse=True):
                try:
                    if release(self._resources[key]["value"]) is not True:
                        return "deferred"
                except Exception:
                    return "deferred"
                del self._resources[key]
            self._draw_profile = profile if operation == "draw" else None
            self._activity_seq += 1
            return "retained" if self._resources else "released"

    def cached(self, key):
        with self._lock:
            row = self._resources.get(key)
            return row["value"] if row else None

    def remember(self, prompt, key, value, *, parents=(), external=False):
        with self._lock:
            job = self._jobs.get(prompt)
            if job is None or job["state"] != "started":
                raise ValueError("resource_job_not_started")
            if (not isinstance(key, str) or not key or len(key) > 256 or not isinstance(parents, tuple)
                    or any(parent not in self._resources for parent in parents) or type(external) is not bool):
                raise ValueError("resource_identity_invalid")
            previous = self._resources.get(key)
            if previous is not None:
                if previous["value"] is not value or previous["parents"] != parents:
                    raise ValueError("resource_identity_conflict")
                previous["kinds"].add(job["identity"][2])
                previous["services"].add(job["identity"][0])
                return value
            self._resources[key] = {"value": value, "parents": parents, "external": external,
                                    "kinds": {job["identity"][2]}, "services": {job["identity"][0]}}
            return value

    def pin_resource(self, service, key):
        with self._lock:
            if service not in self._pins or not self._pins[service][1][0] or key not in self._resources:
                raise ValueError("resource_pin_invalid")
            pins = self._pin_resources.setdefault(service, set())
            if key not in pins:
                pins.add(key)
                self._activity_seq += 1

    def _protected(self, *, vision_only=False, manual_service=None):
        protected = {key for key, row in self._resources.items()
                     if (row["external"] or vision_only and row["kinds"] != {"vision"}
                         or manual_service is not None and ("vision" in row["kinds"]
                             or row["services"] != {manual_service} or "draw" not in row["kinds"]))}
        for service, keys in self._pin_resources.items():
            if self._pins.get(service, (0, (False,)))[1][0]:
                protected.update(key for key in keys if key in self._resources)
        stack = list(protected)
        while stack:
            key = stack.pop()
            for parent in self._resources[key]["parents"]:
                if parent not in protected:
                    protected.add(parent)
                    stack.append(parent)
        return protected

    def vision_due(self):
        with self._lock:
            token = (self.boot_id, self._activity_seq)
            if token == self._last_vision_reclaimed:
                return None
            protected = self._protected(vision_only=True)
            return token if any(key not in protected and "vision" in row["kinds"]
                                for key, row in self._resources.items()) else None

    def reclaim_vision(self, token, *, external_busy, release):
        with self._lock:
            if (external_busy or token != self.vision_due()
                    or any(row["state"] != "released" for row in self._jobs.values())):
                return "deferred"
            protected = self._protected(vision_only=True)
            candidates = [key for key, row in self._resources.items()
                          if "vision" in row["kinds"] and key not in protected]
            for key in candidates:
                try:
                    if release(self._resources[key]["value"]) is not True:
                        return "deferred"
                except Exception:
                    return "deferred"
                del self._resources[key]
            self._last_vision_reclaimed = token
            return "released"

    def reclaim(self, token, *, external_busy, release):
        with self._lock:
            if not self.may_release(token, external_busy=external_busy):
                return "deferred"
            manual_service = token[2] if len(token) == 3 else None
            protected = self._protected(manual_service=manual_service)

            def depth(key):
                parents = self._resources[key]["parents"]
                return 1 + max((depth(parent) for parent in parents), default=0)

            candidates = sorted((key for key in self._resources if key not in protected),
                                key=depth, reverse=True)
            for key in candidates:
                try:
                    if release(self._resources[key]["value"]) is not True:
                        return "deferred"
                except Exception:
                    return "deferred"
                del self._resources[key]
            self._last_reclaimed = (self.boot_id, self._activity_seq)
            if manual_service is not None:
                self._manual_release = None
            return "retained" if self._resources else "released"

    def state(self):
        with self._lock:
            remaining = (None if self._idle_since is None
                         else max(0, self._idle_timeout() - (self.clock() - self._idle_since)))
            pins = sum(1 for _, (enabled,) in self._pins.values() if enabled)
            vision_active = any(row["state"] == "started" and row["identity"][2] == "vision"
                                for row in self._jobs.values())
            vision_loaded = any(getattr(item, "model", None) is not None and getattr(item, "al1s_ready", False)
                for row in self._resources.values() if "vision" in row["kinds"]
                for item in (row["value"] if isinstance(row["value"], tuple) else (row["value"],)))
            drawing = sum(1 for row in self._resources.values() if "draw" in row["kinds"])
            vision_state = ("disabled" if not pins else "loading" if vision_active else "warm" if vision_loaded
                            else "suspended" if drawing else "cold")
            return {"protocol": 1, "boot_id": self.boot_id, "effective_resident": self._resident(),
                "vision_pins": pins, "vision_state": vision_state,
                "active": sum(1 for row in self._jobs.values() if row["state"] != "released"),
                "drawing_resources": drawing,
                "vision_resources": sum(1 for row in self._resources.values() if "vision" in row["kinds"]),
                "activity_seq": self._activity_seq, "idle_timeout_seconds": self._idle_timeout(),
                "idle_seconds_remaining": remaining}

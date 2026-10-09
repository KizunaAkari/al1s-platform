from al1s.components.host.contracts import JsonObject

SERVICES = {
    "platform": ("backend",),
    "postgresql": ("postgres",),
    "s3": ("seaweed-master", "seaweed-volume", "seaweed-filer", "seaweed-s3"),
    "mqtt": ("mosquitto",),
}
S3_LAYOUT_LABEL = "org.al1s.storage-layout"
S3_BUNDLE_LAYOUT = "seaweed-four-process-v1"


def component_services(config: JsonObject, component: str) -> tuple[str, ...]:
    if component != "s3":
        return SERVICES[component]
    configured = config.get("services", {})
    bundled = "storage" in configured and not configured["storage"].get("profiles")
    legacy = tuple(
        name
        for name in SERVICES["s3"]
        if name in configured and not configured[name].get("profiles")
    )
    if bundled and not legacy:
        return ("storage",)
    if not bundled and legacy == SERVICES["s3"]:
        return legacy
    raise ValueError("component_storage_layout_invalid")


def service_map(config: JsonObject) -> dict[str, tuple[str, ...]]:
    return {component: component_services(config, component) for component in SERVICES}


def storage_layout(config: JsonObject) -> str:
    return "bundle" if component_services(config, "s3") == ("storage",) else "legacy"


def validate_s3_image(layout: str, image: JsonObject) -> None:
    label = (image.get("Config", {}).get("Labels") or {}).get(S3_LAYOUT_LABEL)
    if (layout == "bundle" and label != S3_BUNDLE_LAYOUT) or (
        layout == "legacy" and label is not None
    ):
        raise ValueError("component_storage_layout_mismatch")

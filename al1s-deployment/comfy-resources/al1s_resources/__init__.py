NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}


def _install():
    import logging
    import sys
    from pathlib import Path

    required = ("server", "nodes", "execution", "folder_paths", "comfy.model_management")
    if not all(name in sys.modules for name in required):
        return
    try:
        from aiohttp import web
        from .bootstrap import prepare_extension
        from .safety import _not_link

        token_path = Path(__file__).resolve().parent / "resources.token"
        _not_link(token_path)
        token = token_path.read_text(encoding="ascii").strip()
        server, nodes, execution, folder_paths, memory = (sys.modules[name] for name in required)
        root = Path(nodes.__file__).resolve().parent
        prepare_extension(server.PromptServer.instance, nodes, execution, memory, folder_paths, root, token, web=web)
    except Exception as error:
        logging.warning("AL1S Comfy resource extension not initialized: %s", str(error)[:100])


_install()

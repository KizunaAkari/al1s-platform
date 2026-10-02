from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles


class FrontendFiles(StaticFiles):
    async def get_response(self, path: str, scope: MutableMapping[str, Any]) -> Response:
        path = path.replace(chr(92), '/').lstrip('/')
        if path == 'api' or path.startswith('api/'):
            raise HTTPException(404)
        try:
            response = await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404 or path.startswith('assets/') or Path(path).suffix:
                raise
            response = await super().get_response('index.html', scope)
        if response.media_type == 'text/html':
            response.headers['Cache-Control'] = 'no-store'
        return response


def mount_frontend(app: FastAPI, directory: str | None) -> None:
    if directory is None:
        return
    root = Path(directory)
    if not (root / 'index.html').is_file():
        raise RuntimeError('Configured frontend directory has no index.html')
    app.mount('/', FrontendFiles(directory=root, html=True), name='frontend')

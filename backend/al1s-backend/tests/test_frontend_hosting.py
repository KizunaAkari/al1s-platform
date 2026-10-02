from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from al1s.app.frontend import mount_frontend


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    (tmp_path / "index.html").write_text("<html>AL1S</html>", encoding="utf-8")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets/main.js").write_text("export {}", encoding="utf-8")
    app = FastAPI()

    @app.get("/api/v1/probe")
    def probe() -> dict[str, bool]:
        return {"ok": True}

    mount_frontend(app, str(tmp_path))
    return TestClient(app)


@pytest.mark.parametrize("path", ["/", "/tasks", "/maa/editor"])
def test_spa(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "AL1S" in response.text


@pytest.mark.parametrize("path", ["/api/missing", "/api", "/assets/missing.js", "/lost.png"])
def test_missing(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 404


def test_api_and_assets(client: TestClient) -> None:
    assert client.get("/api/v1/probe").json() == {"ok": True}
    assert client.get("/assets/main.js").text == "export {}"
    assert client.post("/tasks").status_code == 405


def test_missing_build(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="Configured frontend directory"):
        mount_frontend(FastAPI(), str(tmp_path))


def test_api_only() -> None:
    app = FastAPI()
    mount_frontend(app, None)
    assert TestClient(app).get("/").status_code == 404

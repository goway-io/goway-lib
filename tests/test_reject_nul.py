from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from goway_lib.handlers import install_handlers


class Note(BaseModel):
    text: str


def client() -> TestClient:
    app = FastAPI()
    install_handlers(app)

    @app.post("/notes")
    def create(note: Note) -> dict:
        return {"text": note.text}

    @app.get("/notes")
    def search(q: str = "") -> dict:
        return {"q": q}

    return TestClient(app)


def test_a_nul_in_a_json_body_is_a_422_problem():
    response = client().post("/notes", content=b'{"text": "a\\u0000b"}', headers={"content-type": "application/json"})
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["errors"] == [{"field": "body", "message": "Text must not contain the NUL character."}]


def test_a_nul_in_the_query_is_a_422_problem():
    assert client().get("/notes", params={"q": "a\x00b"}).status_code == 422


def test_clean_requests_pass_through_with_their_body():
    c = client()
    assert c.post("/notes", json={"text": "Toshkent → Samarqand"}).json() == {"text": "Toshkent → Samarqand"}
    assert c.get("/notes", params={"q": "100%"}).json() == {"q": "100%"}

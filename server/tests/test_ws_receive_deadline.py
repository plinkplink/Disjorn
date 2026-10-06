import pytest
from fastapi import FastAPI, WebSocket
from starlette.testclient import TestClient, WebSocketTestSession


def test_a_websocket_receive_that_never_gets_a_frame_fails_instead_of_hanging(monkeypatch):
    app = FastAPI()

    @app.websocket("/silent")
    async def silent(ws: WebSocket):
        await ws.accept()
        await ws.receive_text()

    assert WebSocketTestSession.receive.__name__ == "_receive_with_deadline"
    monkeypatch.setitem(WebSocketTestSession.receive.__globals__, "WS_RECEIVE_TIMEOUT", 0.5)
    with TestClient(app) as client, client.websocket_connect("/silent") as ws:
        with pytest.raises(TimeoutError):
            ws.receive_json()
        ws.send_text("bye")

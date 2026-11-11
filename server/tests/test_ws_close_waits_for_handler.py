"""Starlette's session exit cancels the handler as soon as close() returns, so close
must outlast whatever the handler still does about the disconnect."""

import threading

import anyio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.testclient import TestClient


def test_closing_a_websocket_returns_only_after_its_handler_has():
    may_finish, finished = threading.Event(), threading.Event()
    app = FastAPI()

    @app.websocket("/lingering")
    async def lingering(ws: WebSocket):
        await ws.accept()
        try:
            await ws.receive_text()
        except WebSocketDisconnect:
            await anyio.to_thread.run_sync(may_finish.wait)
            finished.set()

    with TestClient(app) as client, client.websocket_connect("/lingering") as ws:
        closing = threading.Thread(target=ws.close)
        closing.start()
        closing.join(0.2)
        still_closing = closing.is_alive()
        may_finish.set()
        closing.join()
        assert still_closing and finished.is_set()


def test_leaving_the_with_block_lets_the_handler_finish_its_disconnect_work():
    finished = threading.Event()
    app = FastAPI()

    @app.websocket("/lingering")
    async def lingering(ws: WebSocket):
        await ws.accept()
        try:
            await ws.receive_text()
        except WebSocketDisconnect:
            await anyio.sleep(0.05)
            finished.set()

    with TestClient(app) as client:
        with client.websocket_connect("/lingering"):
            pass
        assert finished.is_set()

"""Channel files for bots, slice B: the per-seat export and its sweep, and the
route a resident seat's file is posted through."""

import asyncio
import base64
import hashlib
import json
import logging
import os
import stat

import httpx
import pytest

from app import db
from app.config import reset_settings_cache
from app.routers import bot_files as bot_files_router
from app.services import bot_files

from .test_bot_files import (  # noqa: F401 — fixtures
    BOT_KEY, CANARY, House, house, png, reset_rate_limit, row, set_env, txt,
)

RELAY_KEY = "relay-key"
SEAT = "res-claw"
QUOTA = 1000


def seats(monkeypatch, *names: str) -> None:
    monkeypatch.setenv("BOT_FILES_EXPORT_SEATS", json.dumps(list(names)))
    reset_settings_cache()


@pytest.fixture
def export(house, tmp_path, monkeypatch):
    """The directory the house bot's seat is exported to."""
    seats(monkeypatch, SEAT)
    seat_dir = tmp_path / "files-ro" / SEAT
    seat_dir.mkdir(parents=True)
    return seat_dir


def exported(seat_dir) -> set[str]:
    return {
        str(p.relative_to(seat_dir)) for p in seat_dir.rglob("*")
        if not p.is_dir() and p.name != bot_files.MANIFEST
    }


def manifest(seat_dir, channel_id: int) -> list[dict]:
    return json.loads((seat_dir / str(channel_id) / bot_files.MANIFEST).read_text())


class Probe:
    """Records what the export holds at the moment each response starts to be sent."""

    def __init__(self, app, seat_dir) -> None:
        self.app, self.seat_dir, self.seen = app, seat_dir, []

    async def __call__(self, scope, receive, send) -> None:
        async def watched(message):
            if message["type"] == "http.response.start":
                self.seen.append(exported(self.seat_dir))
            await send(message)

        await self.app(scope, receive, watched)


@pytest.fixture
async def probed(app, house, export):
    """The admin's own session through a transport that looks at the disk before it answers."""
    probe = Probe(app, export)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=probe), base_url="https://test",
        cookies=house.client.cookies, headers={"Origin": "https://test"},
    ) as client:
        yield House(client, house.bot, house.wire, house.bot_id, house.room), probe


async def shown(house, export, name: str = "notes.txt", body: bytes = b"plain notes"):
    """One file posted in the open room and exported; its message, id and relative path."""
    message, (att,) = await house.say("x", txt(name, body))
    await bot_files.export_sweep()
    path = f"{house.room}/{bot_files.export_name(await row(att))}"
    assert path in exported(export)
    return message, att, path


# ------------------------------------------------------------------- export

async def test_a_visible_file_is_exported_with_its_manifest_row(house, export):
    await house.flip("on")
    message, (att,) = await house.say("x", txt("Field Notes.TXT", b"plain notes"))
    assert exported(export) == set()

    await bot_files.export_sweep()

    path = export / str(house.room) / f"{att}_Field-Notes.txt"
    assert exported(export) == {f"{house.room}/{att}_Field-Notes.txt"}
    assert path.read_bytes() == b"plain notes"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert manifest(export, house.room) == [{
        "attachment_id": att,
        "message_id": message["id"],
        "author": "Ada",
        "created_at": (await row(att))["created_at"],
        "mime_type": "text/plain",
        "file": f"{att}_Field-Notes.txt",
        "sha256": hashlib.sha256(b"plain notes").hexdigest(),
    }]


async def test_a_sweep_that_changes_nothing_rewrites_nothing(house, export):
    await house.flip("on")
    _, _, path = await shown(house, export)
    before = {p: (export / p).stat().st_mtime_ns for p in (path, f"{house.room}/manifest.json")}
    opened = []
    real = bot_files._copy
    bot_files._copy = lambda *a: opened.append(a) or real(*a)
    try:
        await bot_files.export_sweep()
    finally:
        bot_files._copy = real

    assert opened == []
    assert before == {p: (export / p).stat().st_mtime_ns for p in before}


@pytest.mark.parametrize("hide", ["secret", "off_the_record", "before_the_flag", "not_a_type"])
async def test_a_file_bots_may_not_read_is_never_exported(house, export, hide):
    if hide == "before_the_flag":
        await house.say("x", txt())
        await house.flip("on")
    else:
        await house.flip("on")
        if hide == "not_a_type":
            await house.say("x", ("a.zip", b"PK\x03\x04", "application/zip"))
        else:
            await house.say("x", txt(), flags={hide: True})

    await bot_files.export_sweep()

    assert exported(export) == set()
    assert not (export / str(house.room)).exists()


# --------------------------------------------------------- sweep, immediate

async def test_closing_the_channel_removes_its_files_before_the_response(probed, export):
    house, probe = probed
    await house.flip("on")
    _, _, path = await shown(house, export)
    probe.seen.clear()

    await house.say("/files-safe #files off")

    assert probe.seen == [set()]
    assert not (export / str(house.room)).exists()


@pytest.mark.parametrize("words", ["keep this secret", "this is off the record"])
async def test_flagging_a_message_removes_its_file_before_the_response(probed, export, words):
    house, probe = probed
    await house.flip("on")
    message, att, path = await shown(house, export)
    _, _, other = await shown(house, export, "other.txt", b"other")
    probe.seen.clear()

    r = await house.client.patch(f"/messages/{message['id']}", json={"content": words})

    assert r.status_code == 200 and bot_files.privacy.hidden_from_bots(r.json()["privacy_flags"])
    assert probe.seen == [{other}]
    assert [e["file"] for e in manifest(export, house.room)] == [other.split("/")[1]]


async def test_deleting_a_message_removes_its_file_before_the_response(probed, export):
    house, probe = probed
    await house.flip("on")
    message, att, path = await shown(house, export)
    probe.seen.clear()

    assert (await house.client.delete(f"/messages/{message['id']}")).status_code == 200

    assert probe.seen == [set()]
    assert not (export / str(house.room)).exists()


async def test_deleting_the_channel_removes_its_files_before_the_response(probed, export):
    house, probe = probed
    await house.flip("on")
    await shown(house, export)
    probe.seen.clear()

    assert (await house.client.delete(f"/channels/{house.room}")).status_code == 200

    assert probe.seen == [set()]
    assert list(export.iterdir()) == []


async def test_removing_the_bot_from_the_channel_removes_its_files_before_the_response(
    probed, export
):
    house, probe = probed
    await house.flip("on")
    await shown(house, export)
    probe.seen.clear()

    r = await house.client.delete(f"/channels/{house.room}/bots/{house.bot_id}")

    assert r.status_code == 200 and r.json()["removed"] is True
    assert probe.seen == [set()]


async def test_an_edit_that_hides_nothing_runs_no_sweep(house, export, monkeypatch):
    await house.flip("on")
    message, _, path = await shown(house, export)
    ran = []

    async def counted():
        ran.append(1)

    monkeypatch.setattr(bot_files, "export_sweep", counted)
    r = await house.client.patch(f"/messages/{message['id']}", json={"content": "reworded"})

    assert r.status_code == 200 and ran == []


# ------------------------------------------------------------- sweep, timer

@pytest.fixture
def timer(tmp_db_path, monkeypatch):
    monkeypatch.setenv("BOT_FILES_SWEEP_SECONDS", "0.02")
    reset_settings_cache()


async def until(condition) -> None:
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the timer never got there")


async def test_the_timer_alone_adds_and_removes(timer, house, export):
    await house.flip("on")
    message, (att,) = await house.say("x", txt())
    path = f"{house.room}/{att}_notes.txt"
    await until(lambda: exported(export) == {path})

    await db.execute(
        "UPDATE messages SET privacy_flags = ? WHERE id = ?",
        (json.dumps({"secret": True}), message["id"]),
    )
    await until(lambda: exported(export) == set())

    await db.execute("UPDATE messages SET privacy_flags = '{}' WHERE id = ?", (message["id"],))
    await until(lambda: exported(export) == {path})
    await db.execute("UPDATE channels SET files_safe = 0 WHERE id = ?", (house.room,))
    await until(lambda: exported(export) == set())


async def test_no_timer_runs_unless_one_is_asked_for(house, export):
    await house.flip("on")
    await house.say("x", txt())
    await asyncio.sleep(0.1)
    assert exported(export) == set()


async def test_a_file_left_by_an_earlier_process_is_swept_at_startup(
    tmp_db_path, tmp_path, monkeypatch
):
    seats(monkeypatch, SEAT)
    stale = tmp_path / "files-ro" / SEAT / "7" / "1_stale.txt"
    stale.parent.mkdir(parents=True)
    stale.write_text(CANARY)
    await db.close()
    from app.main import create_app

    application = create_app()
    async with application.router.lifespan_context(application):
        assert not stale.parent.exists()
    await db.close()


# --------------------------------------------------------------- membership

async def test_only_channels_the_seats_bot_is_in_are_exported(house, export):
    den = (await house.client.post(
        "/channels", json={"name": "den", "visibility": "private"})).json()["id"]
    main = (await db.fetch_one("SELECT id FROM channels WHERE type = 'main_feed'"))["id"]
    await house.flip("on")
    await house.flip("on", channel="den")
    await house.flip("on", channel="main")
    _, (mine,) = await house.say("x", txt("mine.txt"))
    _, (private,) = await house.say("x", txt("private.txt"), channel=den)
    _, (public,) = await house.say("x", txt("public.txt"), channel=main)

    await bot_files.export_sweep()
    assert exported(export) == {f"{house.room}/{mine}_mine.txt"}

    await house.join(house.bot_id, den)
    await bot_files.export_sweep()
    assert exported(export) == {
        f"{house.room}/{mine}_mine.txt", f"{den}/{private}_private.txt"}


async def test_each_seat_gets_its_own_bots_channels_under_its_own_name(
    house, export, tmp_path, monkeypatch
):
    den = (await house.client.post("/channels", json={"name": "den"})).json()["id"]
    await house.add_bot("Otto", "otto-key", den)
    otto_dir = tmp_path / "files-ro" / "res-otto"
    otto_dir.mkdir()
    seats(monkeypatch, SEAT, "res-otto", "res-nobody")
    await house.flip("on")
    await house.flip("on", channel="den")
    _, (mine,) = await house.say("x", txt("mine.txt"))
    _, (his,) = await house.say("x", txt("his.txt"), channel=den)

    await bot_files.export_sweep()

    assert exported(export) == {f"{house.room}/{mine}_mine.txt"}
    assert exported(otto_dir) == {f"{den}/{his}_his.txt"}
    assert sorted(p.name for p in (tmp_path / "files-ro").iterdir()) == ["res-claw", "res-otto"]


@pytest.mark.parametrize("change", ["renamed", "doubled", "unlisted"])
async def test_a_seat_with_no_one_bot_exports_nothing(house, export, monkeypatch, change):
    await house.flip("on")
    await shown(house, export)

    if change == "renamed":
        await db.execute("UPDATE bots SET name = 'talon' WHERE id = ?", (house.bot_id,))
    elif change == "doubled":
        await house.add_bot("Claw", "k2", house.room)
    else:
        seats(monkeypatch, "res-otto")
    await bot_files.export_sweep()

    assert (exported(export) == set()) is (change != "unlisted")


@pytest.mark.parametrize("label", ["../escape", "res-claw/../../escape", "claw", "/abs", ""])
async def test_a_seat_label_that_is_not_a_seat_touches_no_directory(
    house, export, tmp_path, monkeypatch, label
):
    victim = tmp_path / "escape"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep")
    (tmp_path / "files-ro" / "claw").mkdir()
    (tmp_path / "files-ro" / "claw" / "keep.txt").write_text("keep")
    seats(monkeypatch, label)
    await house.flip("on")
    await house.say("x", txt())

    await bot_files.export_sweep()

    assert (victim / "keep.txt").exists()
    assert (tmp_path / "files-ro" / "claw" / "keep.txt").exists()


async def test_a_seat_directory_nobody_made_is_not_made(house, tmp_path, monkeypatch):
    seats(monkeypatch, SEAT)
    await house.flip("on")
    await house.say("x", txt())

    await bot_files.export_sweep()

    assert not (tmp_path / "files-ro").exists()


async def test_a_seat_directory_that_is_a_symlink_is_left_alone(house, tmp_path, monkeypatch):
    seats(monkeypatch, SEAT)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.txt").write_text("keep")
    (tmp_path / "files-ro").mkdir()
    (tmp_path / "files-ro" / SEAT).symlink_to(elsewhere)
    await house.flip("on")
    await house.say("x", txt())

    await bot_files.export_sweep()

    assert sorted(p.name for p in elsewhere.iterdir()) == ["keep.txt"]


# ---------------------------------------------------------------- filenames

@pytest.mark.parametrize("name, expected", [
    ("notes.txt", "7_notes.txt"),
    ("../../etc/passwd", "7_etc-passwd"),
    ("..", "7_file"),
    ("", "7_file"),
    (None, "7_file"),
    (".bashrc", "7_bashrc"),
    ("a/b\\c.PNG", "7_a-b-c.png"),
    ("manifest.json", "7_manifest.json"),
    ("x" * 300 + ".txt", "7_" + "x" * 40 + ".txt"),
    ("report.tar.gz", "7_report-tar.gz"),
    ("weird.ext!ension", "7_weird-ext-ension"),
    ("nul\x00byte\n.txt", "7_nul-byte.txt"),
    ("ünïcode✓.md", "7_n-code.md"),
    ("a.verylongextension", "7_a-verylongextension"),
])
def test_an_exported_name_is_one_safe_path_component(name, expected):
    got = bot_files.export_name({"id": 7, "original_filename": name})
    assert got == expected
    assert os.path.basename(got) == got and len(got) <= 60


async def test_two_uploads_with_one_name_are_two_files(house, export):
    await house.flip("on")
    _, (a, b) = await house.say("x", txt("same.txt", b"one"), txt("same.txt", b"two"))

    await bot_files.export_sweep()

    assert exported(export) == {f"{house.room}/{a}_same.txt", f"{house.room}/{b}_same.txt"}
    assert (export / str(house.room) / f"{b}_same.txt").read_bytes() == b"two"


# -------------------------------------------------------------- fail closed

async def test_anything_the_sweep_did_not_put_there_is_removed(house, export, tmp_path):
    await house.flip("on")
    _, _, path = await shown(house, export)
    room = export / str(house.room)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    (room / "999_planted.txt").write_text(CANARY)
    (room / ".tmp-5_half.txt").write_text(CANARY)
    (room / "link.txt").symlink_to(outside)
    (room / "sub").mkdir()
    (export / "stray.txt").write_text(CANARY)
    (export / "999").mkdir()
    (export / "999" / "1_gone.txt").write_text(CANARY)
    (export / "٣").mkdir()

    await bot_files.export_sweep()

    assert exported(export) == {path}
    assert sorted(p.name for p in export.iterdir()) == [str(house.room)]
    assert sorted(p.name for p in room.iterdir()) == sorted([path.split("/")[1], "manifest.json"])
    assert outside.read_text() == "outside"


async def test_a_file_whose_name_is_right_but_is_a_symlink_is_replaced(house, export, tmp_path):
    await house.flip("on")
    _, att, path = await shown(house, export)
    secret = tmp_path / "secret.txt"
    secret.write_text(CANARY)
    (export / path).unlink()
    (export / path).symlink_to(secret)

    await bot_files.export_sweep()

    assert not (export / path).is_symlink()
    assert (export / path).read_bytes() == b"plain notes"
    assert secret.read_text() == CANARY


@pytest.mark.parametrize("broken", ["visible_to_bots", "seat_bot", "_reconcile"])
async def test_a_sweep_that_cannot_finish_empties_the_export_and_does_not_raise(
    house, export, monkeypatch, caplog, broken
):
    await house.flip("on")
    await shown(house, export)

    def boom(*_args, **_kwargs):
        raise RuntimeError("database is gone")

    monkeypatch.setattr(bot_files, broken, boom)
    await bot_files.export_sweep()

    assert list(export.iterdir()) == []
    assert "sweep failed for res-claw" in caplog.text


async def test_a_handler_whose_sweep_fails_still_answers_and_leaves_nothing_exported(
    house, export, monkeypatch
):
    await house.flip("on")
    message, _, _ = await shown(house, export)
    monkeypatch.setattr(bot_files, "_reconcile", lambda *a: 1 / 0)

    assert (await house.client.delete(f"/messages/{message['id']}")).status_code == 200

    assert list(export.iterdir()) == []
    assert (await db.fetch_one(
        "SELECT deleted_at FROM messages WHERE id = ?", (message["id"],)))["deleted_at"]


async def test_removals_and_the_pruned_manifest_come_before_any_copy(
    house, export, monkeypatch
):
    await house.flip("on")
    message, gone, gone_path = await shown(house, export)
    await house.say("x", txt("new.txt", b"new"))
    await db.execute("UPDATE messages SET deleted_at = 'x' WHERE id = ?", (message["id"],))
    at_first_copy = []
    real = bot_files._copy

    def watching(src, dest):
        at_first_copy.append((exported(export), manifest(export, house.room)))
        return real(src, dest)

    monkeypatch.setattr(bot_files, "_copy", watching)
    await bot_files.export_sweep()

    assert at_first_copy == [(set(), [])]


async def test_a_copy_that_fails_is_logged_and_made_by_the_next_sweep(
    house, export, monkeypatch, caplog
):
    await house.flip("on")
    _, (bad, good) = await house.say("x", txt("bad.txt", b"bad"), txt("good.txt", b"good"))
    real = os.replace

    def failing(src, dest):
        if str(dest).endswith("bad.txt"):
            raise OSError("disk full")
        return real(src, dest)

    monkeypatch.setattr(os, "replace", failing)
    await bot_files.export_sweep()

    assert exported(export) == {f"{house.room}/{good}_good.txt"}
    assert [e["attachment_id"] for e in manifest(export, house.room)] == [good]
    assert "could not write" in caplog.text

    monkeypatch.setattr(os, "replace", real)
    await bot_files.export_sweep()
    assert exported(export) == {f"{house.room}/{bad}_bad.txt", f"{house.room}/{good}_good.txt"}
    assert [e["attachment_id"] for e in manifest(export, house.room)] == [bad, good]


async def test_a_half_written_copy_is_never_under_the_files_name(house, export, monkeypatch):
    await house.flip("on")
    _, (att,) = await house.say("x", txt("big.txt", b"x" * 10))
    seen = []
    real = os.replace

    def watching(src, dest):
        seen.append((os.path.basename(src), os.path.exists(dest)))
        return real(src, dest)

    monkeypatch.setattr(os, "replace", watching)
    await bot_files.export_sweep()

    assert (f".tmp-{att}_big.txt", False) in seen


async def test_a_manifest_that_cannot_be_written_is_removed(house, export, monkeypatch):
    await house.flip("on")
    message, att, path = await shown(house, export)
    _, _, other = await shown(house, export, "other.txt", b"other")
    await db.execute("UPDATE messages SET deleted_at = 'x' WHERE id = ?", (message["id"],))
    real = bot_files._write_atomic

    def failing(dest, chunks):
        if dest.name == bot_files.MANIFEST:
            raise OSError("disk full")
        return real(dest, chunks)

    monkeypatch.setattr(bot_files, "_write_atomic", failing)
    await bot_files.export_sweep()

    assert exported(export) == {other}
    assert not (export / str(house.room) / bot_files.MANIFEST).exists()


async def test_a_file_whose_original_is_gone_is_not_exported(house, export):
    await house.flip("on")
    _, att, path = await shown(house, export)
    (bot_files.get_settings().data_dir / (await row(att))["file_path"]).unlink()

    await bot_files.export_sweep()

    assert exported(export) == set()


# ------------------------------------------------------- a removal that fails

def refuse_unlink(monkeypatch, name: str) -> None:
    real = os.unlink

    def unlink(path, *args, **kwargs):
        if os.path.basename(os.fspath(path)) == name:
            raise PermissionError(1, "Operation not permitted")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", unlink)


def refuse_writing(monkeypatch, name: str) -> None:
    real = os.open

    def opening(path, flags, *args, **kwargs):
        if os.path.basename(os.fspath(path)) == name and flags & os.O_WRONLY:
            raise PermissionError(1, "Operation not permitted")
        return real(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", opening)


def content(path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""


async def test_a_file_that_cannot_be_unlinked_loses_its_content(house, export, monkeypatch):
    await house.flip("on")
    message, _, path = await shown(house, export, "plan.txt", CANARY.encode())
    _, _, other = await shown(house, export, "other.txt", b"other")
    refuse_unlink(monkeypatch, path.split("/")[1])

    r = await house.client.patch(f"/messages/{message['id']}", json={"content": "keep this secret"})

    assert r.status_code == 200
    assert (export / path).exists() and content(export / path) == b""
    assert stat.S_IMODE((export / path).lstat().st_mode) == 0
    assert (export / other).read_bytes() == b"other"
    assert [e["file"] for e in manifest(export, house.room)] == [other.split("/")[1]]

    monkeypatch.undo()
    await bot_files.export_sweep()
    assert exported(export) == {other}


async def test_a_directory_that_cannot_be_removed_loses_every_files_content(
    house, export, monkeypatch
):
    await house.flip("on")
    _, _, path = await shown(house, export, "plan.txt", CANARY.encode())
    _, _, other = await shown(house, export, "other.txt", CANARY.encode())
    refuse_unlink(monkeypatch, path.split("/")[1])

    await house.say("/files-safe #files off")

    assert content(export / path) == b"" and content(export / other) == b""


async def test_content_that_cannot_be_removed_empties_the_seat_and_is_reported(
    house, export, monkeypatch, caplog
):
    await house.flip("on")
    message, _, path = await shown(house, export, "plan.txt", CANARY.encode())
    _, _, other = await shown(house, export, "other.txt", b"other")
    name = path.split("/")[1]
    refuse_unlink(monkeypatch, name)
    refuse_writing(monkeypatch, name)

    assert (await house.client.delete(f"/messages/{message['id']}")).status_code == 200

    assert exported(export) == {path}
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert errors[-1] == (
        "bot file export: 1 entries that must go are still readable in the export of"
        " res-claw; the next sweep retries")
    assert name not in caplog.text and "plan" not in caplog.text

    monkeypatch.undo()
    await bot_files.export_sweep()
    assert exported(export) == {other}


async def test_a_link_that_cannot_be_unlinked_is_never_written_through(
    house, export, tmp_path, monkeypatch
):
    await house.flip("on")
    await shown(house, export)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    (export / str(house.room) / "link.txt").symlink_to(outside)
    refuse_unlink(monkeypatch, "link.txt")

    await bot_files.export_sweep()

    assert outside.read_text() == "outside" and stat.S_IMODE(outside.stat().st_mode) != 0
    assert exported(export) == {f"{house.room}/link.txt"}


# ------------------------------------------------------------ the sweep's cost

async def test_a_sweep_asks_the_database_no_more_for_more_files(house, export, monkeypatch):
    asked = []
    for name in ("fetch_one", "fetch_all"):
        real = getattr(db, name)

        async def counted(*args, _real=real, **kwargs):
            asked.append(1)
            return await _real(*args, **kwargs)

        monkeypatch.setattr(db, name, counted)
    await house.flip("on")
    await house.say("x", txt("a.txt", b"a"))
    asked.clear()
    await bot_files.export_sweep()
    for_one = len(asked)
    await house.say("x", *(txt(f"{n}.txt", b"b") for n in range(5)))
    await house.say("x", txt("s.txt", b"s"), flags={"secret": True})
    asked.clear()

    await bot_files.export_sweep()

    assert len(asked) == for_one
    assert len(exported(export)) == 6


async def test_the_one_query_decides_every_row_as_visible_to_bots_does(house, monkeypatch):
    den = (await house.client.post("/channels", json={"name": "den"})).json()["id"]
    yard = (await house.client.post("/channels", json={"name": "yard"})).json()["id"]
    _, (early, kept) = await house.say("x", txt("early.txt"), txt("kept.txt"))
    await house.flip("on")
    await house.flip("on", channel="yard")
    await db.execute("UPDATE attachments SET bot_history_ok = 1 WHERE id = ?", (kept,))
    _, (plain, typed) = await house.say(
        "x", txt(), ("typed.txt", b"t", "text/plain; charset=utf-8"))
    _, (elsewhere,) = await house.say("x", txt(), channel=yard)
    await house.say("x", txt(), flags={"secret": True})
    await house.say("x", txt(), flags={"off_the_record": True})
    await house.say("x", txt(), channel=den)
    await house.say("x", ("a.zip", b"PK", "application/zip"))
    gone, _ = await house.say("x", txt())
    await house.client.delete(f"/messages/{gone['id']}")
    for flags in ("not json", "[]", '{"secret": 1}'):
        message, _ = await house.say("x", txt())
        await db.execute(
            "UPDATE messages SET privacy_flags = ? WHERE id = ?", (flags, message["id"]))
    for size in (-1, 10 ** 12):
        _, (att,) = await house.say("x", txt())
        await db.execute("UPDATE attachments SET size_bytes = ? WHERE id = ?", (size, att))
    await house.client.post("/upload", files=[("files", ("staged.txt", b"s", "text/plain"))])

    every = await db.fetch_all("SELECT * FROM attachments")
    one_by_one = {a["id"] for a in every if await bot_files.visible_to_bots(a)}
    at_once = {a["id"] for a in await bot_files.visible_rows()}

    assert at_once == one_by_one == {kept, plain, typed, elsewhere}
    assert len(every) == 16


# ------------------------------------------------------------------ the ceiling

async def test_an_export_over_its_ceiling_keeps_the_newest_files_that_fit(
    house, export, monkeypatch, caplog
):
    await house.flip("on")
    _, (old, big, new) = await house.say(
        "x", txt("old.txt", b"o" * 100), txt("big.txt", b"b" * 800), txt("new.txt", b"n" * 300))
    set_env(monkeypatch, "BOT_FILES_EXPORT_MAX_BYTES", 500)

    await bot_files.export_sweep()
    await bot_files.export_sweep()

    assert exported(export) == {f"{house.room}/{old}_old.txt", f"{house.room}/{new}_new.txt"}
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [
        "bot file export: res-claw is over its ceiling of 500 bytes; 1 files (800 bytes)"
        " are left out and 400 bytes are exported"]


async def test_a_lowered_ceiling_removes_what_no_longer_fits(house, export, monkeypatch):
    await house.flip("on")
    _, (old, new) = await house.say("x", txt("old.txt", b"o" * 300), txt("new.txt", b"n" * 300))
    await bot_files.export_sweep()
    assert len(exported(export)) == 2

    set_env(monkeypatch, "BOT_FILES_EXPORT_MAX_BYTES", 500)
    await bot_files.export_sweep()

    assert exported(export) == {f"{house.room}/{new}_new.txt"}
    assert [e["attachment_id"] for e in manifest(export, house.room)] == [new]


# --------------------------------------------------------------------- post

class Poster:
    def __init__(self, house: House, relay: int) -> None:
        self.house, self.relay = house, relay

    async def post(self, name="report.md", body=b"# report", *, channel=None, key=RELAY_KEY,
                   seat=SEAT, quota=QUOTA, text="here it is", raw=None,
                   **more) -> httpx.Response:
        return await self.house.bot.post(
            "/bot/attachments",
            headers={"X-Api-Key": key},
            json={
                "on_behalf_of": seat,
                "channel_id": channel or self.house.room,
                "filename": name,
                "data": raw if raw is not None else base64.b64encode(body).decode(),
                "text": text,
                "daily_quota_bytes": quota,
                **more,
            },
        )

    async def stored(self) -> tuple[int, int]:
        rows = await db.fetch_one(
            "SELECT (SELECT COUNT(*) FROM attachments) AS a, (SELECT COUNT(*) FROM messages) AS m")
        return rows["a"], rows["m"]


@pytest.fixture
async def poster(house):
    relay = await house.add_bot("broker", RELAY_KEY)
    return Poster(house, relay)


async def test_a_posted_file_is_an_attachment_on_the_seats_bots_own_message(house, poster):
    await house.flip("on")

    r = await poster.post()

    assert r.status_code == 200, r.text
    out = r.json()
    message = await db.fetch_one("SELECT * FROM messages WHERE id = ?", (out["message_id"],))
    assert (message["author_type"], message["author_id"]) == ("bot", house.bot_id)
    assert (message["channel_id"], message["content"]) == (house.room, "here it is")
    assert out == {"attachment_id": out["attachment_id"], "message_id": message["id"],
                   "channel_id": house.room, "seq": message["seq"], "size_bytes": 8}
    att = await row(out["attachment_id"])
    assert (att["message_id"], att["mime_type"], att["original_filename"]) == (
        message["id"], "text/markdown", "report.md")
    assert (att["uploader_type"], att["uploader_id"]) == ("bot", house.bot_id)
    assert (bot_files.get_settings().data_dir / att["file_path"]).read_bytes() == b"# report"
    assert await bot_files.visible_to_bots(att) is True
    seen = await house.seen_by_user(message["id"])
    assert [a["id"] for a in seen["attachments"]] == [att["id"]]
    assert seen["author"]["name"] == "claw"


async def test_a_posted_image_gets_its_previews(house, poster):
    await house.flip("on")

    r = await poster.post("chart.png", png(), quota=10 ** 6)

    att = await row(r.json()["attachment_id"])
    assert att["mime_type"] == "image/png" and att["width"] == 64


@pytest.mark.parametrize("where", ["closed", "not_a_member", "unknown", "closed_after"])
async def test_a_channel_the_seat_cannot_read_files_in_takes_no_post(house, poster, where):
    target = house.room
    if where == "not_a_member":
        target = (await house.client.post("/channels", json={"name": "den"})).json()["id"]
        await house.flip("on", channel="den")
    elif where == "unknown":
        target = 999_999
    elif where == "closed_after":
        await house.flip("on")
        await house.flip("off")
    before = await poster.stored()

    r = await poster.post(channel=target)

    assert r.status_code == 403
    assert r.json()["detail"] == (
        f"channel {target} is not a files-safe channel this seat is a member of")
    assert await poster.stored() == before


async def test_a_post_over_the_daily_quota_is_refused_with_the_figures(house, poster):
    await house.flip("on")
    assert (await poster.post(body=b"a" * 600)).status_code == 200
    before = await poster.stored()

    r = await poster.post(body=b"b" * 401)

    assert r.status_code == 403
    assert r.json()["detail"] == (
        "daily upload quota is 1000 bytes; 600 are used today and this file is 401")
    assert await poster.stored() == before
    assert (await poster.post(body=b"c" * 400)).status_code == 200


async def test_the_quota_counts_only_the_posting_bot_and_only_today(house, poster):
    await house.flip("on")
    await house.add_bot("otto", "otto-key", house.room)
    assert (await poster.post(body=b"a" * 900, seat="res-otto")).status_code == 200
    assert (await poster.post(body=b"b" * 900)).status_code == 200
    assert (await poster.post(body=b"c" * 200)).status_code == 403

    await db.execute(
        "UPDATE attachments SET created_at = '2020-01-01T23:59:59.999Z' WHERE uploader_id = ?",
        (house.bot_id,))
    assert (await poster.post(body=b"c" * 200)).status_code == 200


@pytest.mark.parametrize("name", [
    "a.zip", "a.exe", "a", "a.html", "a.htm", "a.svg", "a.xml", "a.unknownext", "a.mp4",
])
async def test_a_type_no_bot_may_post_is_refused(house, poster, name):
    await house.flip("on")
    before = await poster.stored()

    r = await poster.post(name)

    assert (r.status_code, r.json()["detail"]) == (403, "a bot may not post a file of this type")
    assert await poster.stored() == before


@pytest.mark.parametrize("name", ["a.txt", "a.md", "a.py", "a.json", "a.csv", "a.pdf", "a.png"])
def test_text_code_images_and_pdf_are_postable(name):
    assert bot_files.postable_mime(name) is not None


async def test_an_oversize_or_empty_or_garbled_file_is_refused(house, poster, monkeypatch):
    await house.flip("on")
    set_env(monkeypatch, "MAX_UPLOAD_BYTES", 10)
    before = await poster.stored()

    assert (await poster.post(body=b"x" * 11)).json()["detail"] == (
        "a posted file must be 1 to 10 bytes")
    assert (await poster.post(body=b"")).status_code == 403
    assert (await poster.post(raw="not base64!")).json()["detail"] == "file data is not base64"
    assert await poster.stored() == before
    assert (await poster.post(body=b"x" * 10)).status_code == 200


async def _swept(house, body):
    message, (att,) = await house.say("x", txt("s.txt", body))
    await house.client.patch(f"/messages/{message['id']}", json={"content": "keep this secret"})


async def _deleted(house, body):
    message, _ = await house.say("x", txt("s.txt", body))
    await house.client.delete(f"/messages/{message['id']}")


async def _always_secret(house, body):
    await house.say("x", txt("s.txt", body), flags={"secret": True})


async def _before_the_flag(house, body):
    await house.flip("off")
    await house.say("x", txt("s.txt", body))
    await house.flip("on")


async def _in_a_closed_channel(house, body):
    den = (await house.client.post("/channels", json={"name": "den"})).json()["id"]
    await house.say("x", txt("s.txt", body), channel=den)


async def _never_attached(house, body):
    r = await house.client.post("/upload", files=[("files", ("s.txt", body, "text/plain"))])
    assert r.status_code == 200


async def _not_a_bot_readable_type(house, body):
    await house.say("x", ("s.zip", body, "application/zip"))


LAUNDERED = [_swept, _deleted, _always_secret, _before_the_flag, _in_a_closed_channel,
             _never_attached, _not_a_bot_readable_type]


@pytest.mark.parametrize("hide", LAUNDERED, ids=lambda f: f.__name__)
async def test_bytes_of_an_upload_bots_may_not_read_cannot_be_posted_back(house, poster, hide):
    await house.flip("on")
    body = CANARY.encode() * 3
    await hide(house, body)
    before = await poster.stored()

    r = await poster.post("innocent.txt", body)

    assert (r.status_code, r.json()["detail"]) == (
        403, "this file matches an upload that bots may not read")
    assert await poster.stored() == before
    assert (await poster.post("innocent.txt", body + b"!")).status_code == 200


async def test_bytes_hidden_anywhere_are_refused_even_if_also_visible_elsewhere(house, poster):
    await house.flip("on")
    await house.say("x", txt("open.txt", b"shared bytes"))
    assert (await poster.post("copy.txt", b"shared bytes")).status_code == 200

    await house.say("x", txt("closed.txt", b"shared bytes"), flags={"secret": True})
    assert (await poster.post("copy.txt", b"shared bytes")).status_code == 403


async def test_a_same_size_hidden_upload_that_cannot_be_hashed_refuses_the_post(house, poster):
    await house.flip("on")
    _, (att,) = await house.say("x", txt("s.txt", b"12345678"), flags={"secret": True})
    (bot_files.get_settings().data_dir / (await row(att))["file_path"]).unlink()

    assert (await poster.post(body=b"abcdefgh")).status_code == 403
    assert (await poster.post(body=b"abcdefghi")).status_code == 200


@pytest.mark.parametrize("who", ["the_bot_itself", "another_bot", "no_seat", "no_such_seat",
                                 "the_relay_as_itself", "a_person"])
async def test_only_the_relay_naming_a_seat_with_one_bot_posts(house, poster, who):
    await house.flip("on")
    await house.join(poster.relay, house.room)
    await house.add_bot("otto", "otto-key", house.room)
    before = await poster.stored()
    post = {
        "the_bot_itself": lambda: poster.post(key=BOT_KEY),
        "another_bot": lambda: poster.post(key="otto-key"),
        "no_seat": lambda: poster.post(seat=""),
        "no_such_seat": lambda: poster.post(seat="res-nobody"),
        "the_relay_as_itself": lambda: poster.post(seat="res-broker-x"),
        "a_person": lambda: house.client.post("/bot/attachments", json={
            "on_behalf_of": SEAT, "channel_id": house.room, "filename": "a.txt",
            "data": "eA==", "daily_quota_bytes": QUOTA}),
    }[who]

    r = await post()

    assert (r.status_code, r.json()["detail"]) == (403, "no seat to post as")
    assert await poster.stored() == before


async def test_a_post_with_no_credentials_is_unauthenticated(house, app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test") as anon:
        r = await anon.post("/bot/attachments", json={
            "on_behalf_of": SEAT, "channel_id": house.room, "filename": "a.txt",
            "data": "eA==", "daily_quota_bytes": QUOTA})
    assert r.status_code == 401


async def test_a_post_that_fails_after_the_upload_leaves_no_staged_row(
    house, poster, monkeypatch
):
    await house.flip("on")
    before = await poster.stored()

    async def broken(*_args, **_kwargs):
        raise RuntimeError("database is gone")

    monkeypatch.setattr(bot_files_router, "deliver_message", broken)
    with pytest.raises(RuntimeError):
        await poster.post(body=b"retry me")

    assert await poster.stored() == before
    originals = bot_files.get_settings().data_dir / "uploads" / "originals"
    assert [p.name for p in originals.iterdir() if "report" in p.name] == []
    monkeypatch.undo()
    assert (await poster.post(body=b"retry me")).status_code == 200


async def test_every_post_attempt_is_one_log_line_without_the_file(house, poster, caplog):
    await house.flip("on")
    caplog.set_level(logging.INFO, logger="app.routers.bot_files")

    ok = (await poster.post(body=CANARY.encode())).json()["attachment_id"]
    await poster.post("a.zip", CANARY.encode())
    await poster.post(key=BOT_KEY)

    lines = [r.getMessage() for r in caplog.records if r.name == "app.routers.bot_files"]
    assert lines == [
        f"bot attachment post: caller=bot:{poster.relay} poster_bot={house.bot_id}"
        f" channel={house.room} attachment={ok} allowed",
        f"bot attachment post: caller=bot:{poster.relay} poster_bot={house.bot_id}"
        f" channel={house.room} attachment=None refused (a bot may not post a file of this type)",
        f"bot attachment post: caller=bot:{house.bot_id} poster_bot=None"
        f" channel={house.room} attachment=None refused (no seat to post as)",
    ]
    assert CANARY not in caplog.text


async def test_a_posted_file_reaches_the_seats_own_export_on_the_next_sweep(
    house, poster, export
):
    await house.flip("on")
    att = (await poster.post()).json()["attachment_id"]

    await bot_files.export_sweep()

    assert exported(export) == {f"{house.room}/{att}_report.md"}


# ---------------------------------------------------------------- attribution

async def test_a_posted_file_carries_what_the_seat_said_produced_it_unverified(house, poster):
    await house.flip("on")

    r = await poster.post(attribution={"model": "claude-x", "summoner": "Ada", "verified": True})

    stated = {"model": "claude-x", "verified": False, "summoner": "Ada"}
    seen = await house.seen_by_user(r.json()["message_id"])
    assert seen["attribution"] == stated
    (frame,) = [f for f in house.wire.frames if f.get("type") == "message_create"
                and f["message"]["id"] == r.json()["message_id"]]
    assert frame["message"]["attribution"] == stated


@pytest.mark.parametrize("given, stored", [
    (None, {}),
    ({}, {}),
    ({"model": None, "summoner": None}, {}),
    ({"summoner": "Ada"}, {"model": None, "verified": False, "summoner": "Ada"}),
    ({"model": "claude-x"}, {"model": "claude-x", "verified": False, "summoner": None}),
])
async def test_a_post_that_names_no_model_or_summoner_is_unattributed(
    house, poster, given, stored
):
    await house.flip("on")
    more = {} if given is None else {"attribution": given}

    r = await poster.post(**more)

    assert (await house.seen_by_user(r.json()["message_id"]))["attribution"] == stored


@pytest.mark.parametrize("bad", [
    {"model": "m" * 101}, {"summoner": "s" * 101}, {"model": 7}, {"verified": "yes"},
    {"model": "claude-x", "note": "a side channel"}, "claude-x",
])
async def test_an_attribution_the_message_route_would_refuse_posts_nothing(house, poster, bad):
    await house.flip("on")
    before = await poster.stored()

    r = await poster.post(attribution=bad)

    assert r.status_code == 422
    assert await poster.stored() == before


# ---------------------------------------------------------------- idempotency

KEY = "ab" * 32


async def test_migration_026_adds_the_empty_post_record(tmp_path, tmp_db_path, monkeypatch):
    earlier = tmp_path / "migrations"
    earlier.mkdir()
    for path in db.MIGRATIONS_DIR.glob("*.sql"):
        if path.name != "026_bot_file_posts.sql":
            (earlier / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    real = db.MIGRATIONS_DIR
    await db.close()
    await db.connect()
    try:
        monkeypatch.setattr(db, "MIGRATIONS_DIR", earlier)
        assert "024_message_mentions.sql" in await db.run_migrations()
        monkeypatch.setattr(db, "MIGRATIONS_DIR", real)
        assert await db.run_migrations() == ["026_bot_file_posts.sql"]
        assert await db.fetch_all("SELECT * FROM bot_file_posts") == []
        assert await bot_files.posted_before(1, KEY) is None
    finally:
        await db.close()


async def test_the_same_key_twice_is_one_post_and_one_answer(house, poster):
    await house.flip("on")
    before = await poster.stored()

    first = await poster.post(body=b"a" * 600, idempotency_key=KEY)
    again = await poster.post(body=b"a" * 600, idempotency_key=KEY)

    assert first.status_code == again.status_code == 200
    assert again.json() == first.json()
    assert await poster.stored() == (before[0] + 1, before[1] + 1)


async def test_two_posts_of_one_key_at_once_are_one_post(house, poster):
    await house.flip("on")
    before = await poster.stored()

    first, again = await asyncio.gather(
        poster.post(idempotency_key=KEY), poster.post(idempotency_key=KEY))

    assert again.json() == first.json() and first.status_code == 200
    assert await poster.stored() == (before[0] + 1, before[1] + 1)


async def test_another_key_or_no_key_is_a_new_post(house, poster):
    await house.flip("on")
    first = (await poster.post(idempotency_key=KEY)).json()

    other = (await poster.post(text="said differently", idempotency_key="cd" * 32)).json()
    bare = (await poster.post()).json()
    bare_again = (await poster.post()).json()

    assert len({first["message_id"], other["message_id"], bare["message_id"],
                bare_again["message_id"]}) == 4


async def test_a_key_is_one_bots_own(house, poster):
    await house.flip("on")
    await house.add_bot("otto", "otto-key", house.room)

    mine = (await poster.post(idempotency_key=KEY)).json()
    his = (await poster.post(seat="res-otto", idempotency_key=KEY)).json()

    assert his["message_id"] != mine["message_id"]
    author = await db.fetch_one(
        "SELECT author_id FROM messages WHERE id = ?", (his["message_id"],))
    assert author["author_id"] != house.bot_id


@pytest.mark.parametrize("change", ["a_day_old", "message_deleted"])
async def test_a_key_stops_answering_after_a_day_or_once_its_message_is_gone(
    house, poster, change
):
    await house.flip("on")
    first = (await poster.post(idempotency_key=KEY)).json()
    if change == "a_day_old":
        await db.execute("UPDATE bot_file_posts SET created_at = '2020-01-01T00:00:00.000Z'")
    else:
        r = await house.bot.delete(f"/messages/{first['message_id']}")
        assert r.status_code == 200

    again = (await poster.post(body=b"# report 2", idempotency_key=KEY)).json()

    assert again["message_id"] != first["message_id"]
    rows = await db.fetch_all("SELECT message_id FROM bot_file_posts")
    assert [r["message_id"] for r in rows] == [again["message_id"]]


async def test_a_post_whose_key_cannot_be_kept_is_still_answered(house, poster, monkeypatch):
    await house.flip("on")

    async def broken(*_args, **_kwargs):
        raise RuntimeError("database is gone")

    monkeypatch.setattr(bot_files, "remember_post", broken)
    r = await poster.post(idempotency_key=KEY)

    assert r.status_code == 200
    assert (await row(r.json()["attachment_id"]))["message_id"] == r.json()["message_id"]


async def test_a_refused_post_leaves_its_key_free(house, poster):
    await house.flip("on")
    assert (await poster.post("a.zip", idempotency_key=KEY)).status_code == 403

    assert (await poster.post(idempotency_key=KEY)).status_code == 200


@pytest.mark.parametrize("key", ["", "short", "AB" * 32, "ab" * 33, 7])
async def test_a_key_that_is_not_a_sha256_posts_nothing(house, poster, key):
    await house.flip("on")
    before = await poster.stored()

    assert (await poster.post(idempotency_key=key)).status_code == 422
    assert await poster.stored() == before


async def test_only_the_seat_itself_is_answered_from_its_key(house, poster):
    await house.flip("on")
    assert (await poster.post(idempotency_key=KEY)).status_code == 200

    r = await poster.post(key=BOT_KEY, idempotency_key=KEY)

    assert (r.status_code, r.json()["detail"]) == (403, "no seat to post as")

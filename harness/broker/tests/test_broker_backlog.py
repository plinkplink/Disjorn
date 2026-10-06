"""The resident backlog verbs, `backlog-list` and `backlog-file`.

Both go to the server's /backlog as the broker's own bot. The filing's author
is the calling seat from SO_PEERCRED, and a seat files at most ten rows per
UTC day, counted from the audit log so a restart does not reset it."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import gen_verb_surface as gen
from brokerd import BACKLOG_DAILY_FILE_CAP, Broker, load_config


def file_rows(harness, n: int) -> None:
    for i in range(n):
        resp = harness.call("backlog-file", {"text": f"request {i}"})
        assert resp["ok"] is True, resp


def backlog_posts(harness) -> list[dict]:
    return [c for c in harness.planroom_calls
            if c["method"] == "POST" and c["path"] == "/backlog"]


def seed(harness, *statuses: str, text: str = "a request") -> None:
    rows = harness.planroom_state.setdefault("backlog", [])
    for status in statuses:
        rows.append({"id": len(rows) + 1, "text": text, "author": "plink",
                     "created_at": "2026-10-05T00:00:00Z", "status": status,
                     "spec_ref": None})


def restarted(harness, tmp_path: Path) -> Broker:
    return Broker(load_config(str(tmp_path / "broker.toml")),
                  str(harness.verbs_path),
                  transport=harness.broker.transport,
                  planroom_api=harness.broker.planroom_api)


def test_both_verbs_ship_off(harness):
    for verb, args in (("backlog-list", {}), ("backlog-file", {"text": "x"})):
        assert harness.call(verb, args)["error"]["code"] == "verb-disabled"
    assert harness.planroom_calls == []
    assert [ln["allowed"] for ln in harness.audit_lines()] == [False, False]


def test_the_author_is_the_calling_seat(harness):
    harness.set_verbs(**{"backlog-file": True})
    resp = harness.call("backlog-file", {"text": "let seats read the backlog"})
    assert resp["ok"] is True
    (call,) = backlog_posts(harness)
    assert call["payload"] == {"text": "let seats read the backlog",
                               "on_behalf_of": "res-test"}
    assert resp["result"]["row"]["author"] == "res-test"
    line = harness.audit_lines()[-1]
    assert line["verb"] == "backlog-file" and line["backlog_filed"] is True


def test_an_args_author_is_refused_not_honoured(harness):
    harness.set_verbs(**{"backlog-file": True})
    for key in ("on_behalf_of", "author"):
        resp = harness.call("backlog-file", {"text": "x", key: "res-other"})
        assert resp["error"]["code"] == "bad-args", key
    assert backlog_posts(harness) == []


def test_the_list_is_open_rows_newest_first_by_default(harness):
    harness.set_verbs(**{"backlog-list": True})
    seed(harness, "open", "rejected", "open", "built")
    resp = harness.call("backlog-list", {})
    result = resp["result"]
    assert result["status"] == "open"
    assert [r["id"] for r in result["rows"]] == [3, 1]
    assert set(result["rows"][0]) == {"id", "text", "author", "created_at",
                                      "status", "spec_ref"}


def test_rejected_and_duplicate_rows_are_listable_by_name(harness):
    harness.set_verbs(**{"backlog-list": True})
    seed(harness, "open", "rejected", "duplicate", "spec'd", "rejected")
    for status, ids in (("rejected", [5, 2]), ("duplicate", [3]),
                        ("spec'd", [4]), ("built", [])):
        rows = harness.call("backlog-list", {"status": status})["result"]["rows"]
        assert [r["id"] for r in rows] == ids, status


def test_an_unknown_status_is_refused(harness):
    harness.set_verbs(**{"backlog-list": True})
    resp = harness.call("backlog-list", {"status": "closed"})
    assert resp["error"]["code"] == "bad-args"
    assert "rejected" in resp["error"]["message"]
    assert harness.planroom_calls == []


def test_the_limit_is_capped_at_fifty(harness):
    harness.set_verbs(**{"backlog-list": True})
    seed(harness, *["open"] * 60)
    assert harness.call("backlog-list", {"limit": 51})["error"]["code"] == "bad-args"
    result = harness.call("backlog-list", {"limit": 50})["result"]
    assert result["count"] == 50 and result["truncated"] is True
    assert result["rows"][0]["id"] == 60


def test_the_list_reads_past_one_server_page(harness):
    harness.set_verbs(**{"backlog-list": True})
    seed(harness, *["built"] * 450, "open")
    rows = harness.call("backlog-list", {})["result"]["rows"]
    assert [r["id"] for r in rows] == [451]
    froms = [c["path"] for c in harness.planroom_calls if c["method"] == "GET"]
    assert len(froms) == 3


def test_long_text_is_clipped_to_three_hundred(harness):
    harness.set_verbs(**{"backlog-list": True})
    seed(harness, "open", text="x" * 1000)
    (row,) = harness.call("backlog-list", {})["result"]["rows"]
    assert len(row["text"]) == 300 and row["text"].endswith("…")


def test_the_eleventh_filing_of_the_day_is_refused_and_audited(harness):
    harness.set_verbs(**{"backlog-file": True})
    file_rows(harness, BACKLOG_DAILY_FILE_CAP)
    resp = harness.call("backlog-file", {"text": "one too many"})
    assert resp["error"]["code"] == "over-budget"
    assert "daily cap of 10" in resp["error"]["message"]
    assert len(backlog_posts(harness)) == BACKLOG_DAILY_FILE_CAP
    line = harness.audit_lines()[-1]
    assert line["verb"] == "backlog-file" and line["allowed"] is False
    assert "daily cap" in line["result_summary"]


def test_the_daily_cap_survives_a_broker_restart(harness, tmp_path):
    harness.set_verbs(**{"backlog-file": True})
    file_rows(harness, BACKLOG_DAILY_FILE_CAP)
    resp = restarted(harness, tmp_path).dispatch(
        os.getuid(), "backlog-file", {"text": "after the restart"})
    assert resp["error"]["code"] == "over-budget"
    assert len(backlog_posts(harness)) == BACKLOG_DAILY_FILE_CAP


def test_yesterdays_filings_do_not_count(harness, tmp_path):
    harness.set_verbs(**{"backlog-file": True})
    yesterday = (dt.datetime.now(dt.timezone.utc)
                 - dt.timedelta(days=1)).isoformat()
    with open(harness.broker.audit_path, "a", encoding="utf-8") as fh:
        for _ in range(BACKLOG_DAILY_FILE_CAP):
            fh.write(json.dumps({"ts": yesterday, "resident": "res-test",
                                 "verb": "backlog-file", "allowed": True,
                                 "result_summary": "filed",
                                 "backlog_filed": True}) + "\n")
    resp = restarted(harness, tmp_path).dispatch(
        os.getuid(), "backlog-file", {"text": "a new day"})
    assert resp["ok"] is True


def test_a_server_refusal_arrives_verbatim_and_spends_nothing(harness):
    harness.set_verbs(**{"backlog-file": True})
    harness.planroom_state["backlog_refusal"] = (
        "Can't file that: backlog items are capped at 2000 characters "
        "(that one was 2001).")
    resp = harness.call("backlog-file", {"text": "x" * 2001})
    assert resp["error"]["message"] == harness.planroom_state["backlog_refusal"]
    harness.planroom_state["backlog_refusal"] = None
    file_rows(harness, BACKLOG_DAILY_FILE_CAP)


def test_the_generated_surface_carries_both_verbs_to_both_seats():
    gen.check()
    for seat in ("res-claudette", "res-gable"):
        names = {t["name"] for t in gen.tool_schemas(
            gen.seat_surface(gen.VERBS_TOML, seat))}
        assert {"backlog_list", "backlog_file"} <= names, seat
    text_arg = gen.load_surface()["backlog-file"]["args"]["text"]
    assert text_arg["max_len"] == 2000

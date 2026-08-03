"""Tests for the house error log.

The properties that matter here are all "does it fail safe": collect must be
idempotent (a timer runs it every 10 minutes forever), rotation must not lose
events, and a redacted source must never leak a line body into a file other
people read.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import errorlog  # noqa: E402


GABLE_LINES = [
    "2026-07-21 18:41:13,602 disjorn.residency ERROR model drift: pinned "
    "claude-fable-5 but session ran claude-opus-4-8 (summon by plink in channel 4)",
    "2026-07-22 16:29:33,481 disjorn.residency WARNING summon session failed: "
    "session timed out after 300.0s",
    "2026-07-23 08:23:50,294 disjorn.residency WARNING summon session failed: "
    "session exit 1: run-resident: auth: CLAUDE_CODE_OAUTH_TOKEN",
    "2026-07-26 00:49:46,101 httpx INFO HTTP Request: GET http://x 200 OK",
]

CLAUDETTE_LINES = [
    "INFO:claudette:[Claudette] connected to Disjorn as bot 1",
    "DEBUG:claudette:[Claudette] stop_reason=max_tokens",
    "DEBUG:claudette:[Claudette] Final answer: No response generated.",
    "DEBUG:claudette:[Claudette] Processing question: SECRET CONVERSATION TEXT "
    "that must never reach the house log stop_reason=max_tokens",
]


@pytest.fixture
def gable_src(tmp_path):
    p = tmp_path / "gable.log"
    p.write_text("\n".join(GABLE_LINES) + "\n", encoding="utf-8")
    src = dict(errorlog.SOURCES[0])
    src["path"] = str(p)
    return src, p


@pytest.fixture
def claudette_src(tmp_path):
    p = tmp_path / "disjorn_bot.log"
    p.write_text("\n".join(CLAUDETTE_LINES) + "\n", encoding="utf-8")
    src = dict(errorlog.SOURCES[1])
    src["path"] = str(p)
    return src, p


# --------------------------------------------------------------------------
# Matching.
# --------------------------------------------------------------------------

def test_scan_extracts_known_kinds(gable_src):
    src, _ = gable_src
    events, offset = errorlog.scan_source(src)
    kinds = [e["kind"] for e in events]
    assert kinds == ["model_drift", "timeout", "session_failed"]
    assert offset > 0


def test_scan_parses_timestamp_from_line(gable_src):
    src, _ = gable_src
    events, _ = errorlog.scan_source(src)
    assert events[0]["ts"] == "2026-07-21T18:41:13Z"


def test_uninteresting_lines_are_ignored(gable_src):
    src, _ = gable_src
    events, _ = errorlog.scan_source(src)
    assert not any("httpx" in e["detail"] for e in events)


def test_canonical_truncation_and_null_turn(claudette_src):
    """The 2026-07-28 lost reply: max_tokens then 'No response generated.'"""
    src, _ = claudette_src
    events, _ = errorlog.scan_source(src)
    kinds = [e["kind"] for e in events]
    assert "truncation" in kinds
    assert "null_turn" in kinds


# --------------------------------------------------------------------------
# The privacy rule.
# --------------------------------------------------------------------------

def test_redacted_source_records_signature_only(claudette_src):
    src, _ = claudette_src
    events, _ = errorlog.scan_source(src)
    for e in events:
        assert "SECRET CONVERSATION TEXT" not in e["detail"]
        assert e["evidence"]["redacted"] is True
    # and the signature itself did survive
    assert any(e["detail"] == "stop_reason=max_tokens" for e in events)


def test_unredacted_source_keeps_the_line(gable_src):
    src, _ = gable_src
    events, _ = errorlog.scan_source(src)
    assert "claude-opus-4-8" in events[0]["detail"]


def test_detail_is_capped(gable_src, tmp_path):
    src, p = gable_src
    p.write_text(
        "2026-07-21 18:41:13,602 disjorn.residency ERROR model drift: pinned a "
        "but session ran b " + ("x" * 5000) + "\n",
        encoding="utf-8",
    )
    events, _ = errorlog.scan_source(src)
    assert len(events[0]["detail"]) <= errorlog.DETAIL_MAX


# --------------------------------------------------------------------------
# Idempotence — a timer runs collect forever.
# --------------------------------------------------------------------------

def test_collect_is_idempotent(tmp_path, gable_src):
    src, _ = gable_src
    log = tmp_path / "errors.jsonl"
    state = tmp_path / "state.json"

    first = errorlog.collect(log, state, [src])
    assert first["written"] == 3

    second = errorlog.collect(log, state, [src])
    assert second["written"] == 0
    assert len(list(errorlog.iter_events(log))) == 3


def test_collect_picks_up_appended_lines(tmp_path, gable_src):
    src, p = gable_src
    log = tmp_path / "errors.jsonl"
    state = tmp_path / "state.json"
    errorlog.collect(log, state, [src])

    with open(p, "a", encoding="utf-8") as fh:
        fh.write(
            "2026-07-29 10:00:00,000 disjorn.residency WARNING summon session "
            "failed: session timed out after 600.0s\n"
        )
    res = errorlog.collect(log, state, [src])
    assert res["written"] == 1


def test_fingerprint_dedupes_when_state_is_lost(tmp_path, gable_src):
    """A wiped state file must not re-append the whole log."""
    src, _ = gable_src
    log = tmp_path / "errors.jsonl"
    state = tmp_path / "state.json"
    errorlog.collect(log, state, [src])
    state.unlink()
    res = errorlog.collect(log, state, [src])
    assert res["written"] == 0


def test_same_signature_at_different_times_is_two_events(tmp_path, gable_src):
    """Two timeouts are two events, not one deduped away."""
    src, p = gable_src
    log = tmp_path / "errors.jsonl"
    state = tmp_path / "state.json"
    errorlog.collect(log, state, [src])
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(
            "2026-07-30 11:11:11,000 disjorn.residency WARNING summon session "
            "failed: session timed out after 300.0s\n"
        )
    res = errorlog.collect(log, state, [src])
    assert res["written"] == 1


# --------------------------------------------------------------------------
# Rotation.
# --------------------------------------------------------------------------

def test_rotation_resets_the_watermark(tmp_path, gable_src):
    """A rotated log has a new inode; events after rotation must be seen."""
    src, p = gable_src
    log = tmp_path / "errors.jsonl"
    state = tmp_path / "state.json"
    errorlog.collect(log, state, [src])

    p.unlink()
    p.write_text(
        "2026-08-01 09:00:00,000 disjorn.residency WARNING summon session "
        "failed: session timed out after 42.0s\n",
        encoding="utf-8",
    )
    res = errorlog.collect(log, state, [src])
    assert res["written"] == 1


def test_truncation_in_place_resets_the_watermark(tmp_path, gable_src):
    src, p = gable_src
    log = tmp_path / "errors.jsonl"
    state = tmp_path / "state.json"
    errorlog.collect(log, state, [src])
    p.write_text(
        "2026-08-02 09:00:00,000 disjorn.residency WARNING summon session "
        "failed: session exit 7: boom\n",
        encoding="utf-8",
    )
    res = errorlog.collect(log, state, [src])
    assert res["written"] == 1


def test_partial_last_line_is_not_consumed(tmp_path, gable_src):
    """A line still being written must be read next pass, not half-parsed."""
    src, p = gable_src
    log = tmp_path / "errors.jsonl"
    state = tmp_path / "state.json"
    errorlog.collect(log, state, [src])

    with open(p, "a", encoding="utf-8") as fh:
        fh.write("2026-08-03 09:00:00,000 disjorn.residency WARNING summon ses")
    assert errorlog.collect(log, state, [src])["written"] == 0

    with open(p, "a", encoding="utf-8") as fh:
        fh.write("sion failed: session timed out after 5.0s\n")
    assert errorlog.collect(log, state, [src])["written"] == 1


# --------------------------------------------------------------------------
# Missing things must not be fatal.
# --------------------------------------------------------------------------

def test_missing_source_is_not_fatal(tmp_path):
    src = dict(errorlog.SOURCES[0])
    src["path"] = str(tmp_path / "nope.log")
    res = errorlog.collect(tmp_path / "e.jsonl", tmp_path / "s.json", [src])
    assert res["written"] == 0


def test_malformed_log_lines_are_skipped(tmp_path):
    log = tmp_path / "errors.jsonl"
    log.write_text('{"ok": 1}\nnot json\n[]\n', encoding="utf-8")
    assert [e for e in errorlog.iter_events(log)] == [{"ok": 1}]


# --------------------------------------------------------------------------
# record + tail.
# --------------------------------------------------------------------------

def test_record_appends_and_tail_reads(tmp_path):
    log = tmp_path / "errors.jsonl"
    rc = errorlog.main(
        ["--log", str(log), "record", "--source", "keyboard",
         "--kind", "other", "--detail", "hello", "--subject", "plink"]
    )
    assert rc == 0
    rows = errorlog.tail(log)
    assert len(rows) == 1
    assert rows[0]["detail"] == "hello"
    assert rows[0]["subject"] == "plink"


def test_unknown_kind_falls_back_to_other():
    ev = errorlog.make_event(source="s", kind="not-a-kind", detail="d")
    assert ev["kind"] == "other"


# --------------------------------------------------------------------------
# Timestamp honesty. A source with no clock must not be stamped "now" — thirty
# events sharing a fabricated time read as an incident that never happened.
# --------------------------------------------------------------------------

def test_timestampless_source_leaves_ts_null(claudette_src):
    src, _ = claudette_src
    events, _ = errorlog.scan_source(src)
    assert events, "expected matches"
    for e in events:
        assert e["ts"] is None
        assert e["ts_known"] is False
        assert e["logged_at"]  # we still record when we saw it


def test_timestamped_source_keeps_ts_known(gable_src):
    src, _ = gable_src
    events, _ = errorlog.scan_source(src)
    assert all(e["ts_known"] for e in events)


def test_timestampless_fingerprints_are_stable_across_runs(claudette_src):
    """The bug this guards: a fingerprint keyed on 'now' changes every pass,
    silently disabling the de-dupe backstop."""
    src, _ = claudette_src
    first, _ = errorlog.scan_source(src)
    second, _ = errorlog.scan_source(src)
    assert [e["fingerprint"] for e in first] == [e["fingerprint"] for e in second]


def test_timestampless_collect_dedupes_after_state_loss(tmp_path, claudette_src):
    src, _ = claudette_src
    log = tmp_path / "errors.jsonl"
    state = tmp_path / "state.json"
    n = errorlog.collect(log, state, [src])["written"]
    assert n > 0
    state.unlink()
    assert errorlog.collect(log, state, [src])["written"] == 0


def test_rotation_defeats_the_position_fingerprint(tmp_path, claudette_src):
    """After rotation a new file's line 3 is a DIFFERENT event from the old
    file's line 3, so the inode in the anchor must keep them apart."""
    src, p = claudette_src
    log = tmp_path / "errors.jsonl"
    state = tmp_path / "state.json"
    errorlog.collect(log, state, [src])
    p.unlink()
    p.write_text("DEBUG:claudette:[Claudette] stop_reason=max_tokens\n",
                 encoding="utf-8")
    assert errorlog.collect(log, state, [src])["written"] == 1


def test_tail_days_does_not_drop_timestampless_events(tmp_path, claudette_src):
    src, _ = claudette_src
    log = tmp_path / "errors.jsonl"
    state = tmp_path / "state.json"
    errorlog.collect(log, state, [src])
    assert errorlog.tail(log, days=7)


def test_format_event_marks_unknown_time(tmp_path):
    ev = errorlog.make_event(source="s", kind="other", detail="d",
                             anchor="x", stamp_now=False)
    assert ev["ts"] is None
    assert errorlog.format_event(ev).startswith("seen ")


def test_tail_filters(tmp_path):
    log = tmp_path / "errors.jsonl"
    errorlog.append_events(log, [
        errorlog.make_event(source="a", kind="timeout", detail="x",
                            subject="res-gable"),
        errorlog.make_event(source="b", kind="crash", detail="y",
                            subject="res-claudette"),
    ])
    assert len(errorlog.tail(log, kind="timeout")) == 1
    assert len(errorlog.tail(log, subject="res-claudette")) == 1


def test_new_log_file_is_group_readable_not_world(tmp_path):
    log = tmp_path / "errors.jsonl"
    errorlog.append_events(log, [errorlog.make_event(
        source="s", kind="other", detail="d")])
    assert oct(log.stat().st_mode)[-3:] == "640"

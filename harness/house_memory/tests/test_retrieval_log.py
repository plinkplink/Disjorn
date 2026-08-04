"""RetrievalLog: write/parse, unified schema fields, legacy tolerance,
reference_counts windowing."""

import json
from datetime import datetime, timedelta, timezone

from house_memory import (
    CALLER_CONSOLIDATION,
    CALLER_SELF_QUERY,
    CALLER_SERVICE,
    RetrievalLog,
    UnknownCaller,
    read_records,
)

import pytest


def test_write_and_parse(tmp_path):
    log = RetrievalLog(tmp_path / "retrieval.jsonl", resident="gable")
    rec = log.log(
        query="what did plink say",
        subject_filter="plink",
        raw_ids=["a", "b"],
        distances=[0.1, None],
        returned_ids=["a"],
        caller=CALLER_SERVICE,
    )
    # on-disk line carries the full unified schema
    line = json.loads((tmp_path / "retrieval.jsonl").read_text().strip())
    assert set(line) == {
        "ts", "resident", "query", "subject_filter", "raw_ids", "distances",
        "returned_ids", "caller",
    }
    assert line["caller"] == CALLER_SERVICE
    assert line["resident"] == "gable"

    parsed = log.read()
    assert len(parsed) == 1
    assert parsed[0] == rec
    assert parsed[0].distances == [0.1, None]


def test_read_missing_file_is_empty(tmp_path):
    assert RetrievalLog(tmp_path / "nope.jsonl", resident="x").read() == []


def test_read_tolerates_legacy_and_garbage_lines(tmp_path):
    path = tmp_path / "legacy.jsonl"
    legacy = {  # claudette-shaped: no resident field
        "ts": "2026-07-01T00:00:00+00:00",
        "query": "old query",
        "subject_filter": None,
        "raw_ids": ["m1"],
        "distances": [0.5],
        "returned_ids": ["m1"],
    }
    path.write_text(json.dumps(legacy) + "\nnot json at all\n\n")
    records = read_records(path)
    assert len(records) == 1
    assert records[0].resident is None
    assert records[0].returned_ids == ["m1"]


def test_reference_counts_window(tmp_path):
    log = RetrievalLog(tmp_path / "retrieval.jsonl", resident="claudette")
    now = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)

    def line(ts, returned, caller=CALLER_SERVICE):
        rec = {
            "ts": ts.isoformat(),
            "resident": "claudette",
            "query": "q",
            "subject_filter": None,
            "raw_ids": returned,
            "distances": [0.1] * len(returned),
            "returned_ids": returned,
            "caller": caller,
        }
        with open(log.path, "a") as f:
            f.write(json.dumps(rec) + "\n")

    line(now - timedelta(days=1), ["a", "b"])
    line(now - timedelta(days=5), ["a"])
    line(now - timedelta(days=40), ["a", "c"])  # outside 30-day window
    counts = log.reference_counts(window_days=30, now=now)
    assert counts == {"a": 2, "b": 1}
    # wider window picks up the old reference — rent assessment is windowed
    assert log.reference_counts(window_days=60, now=now) == {"a": 3, "b": 1, "c": 1}


def test_reference_counts_skips_unparseable_ts(tmp_path):
    log = RetrievalLog(tmp_path / "retrieval.jsonl", resident="x")
    with open(log.path, "w") as f:
        f.write(json.dumps({"ts": "not-a-date", "query": "q",
                            "returned_ids": ["z"],
                            "caller": CALLER_SERVICE}) + "\n")
    assert log.reference_counts(window_days=30) == {}


# ==========================================================================
# Memory v2 phase 1 — the `caller` field.
#
# These pin the defect that killed consolidation v1: a read is not evidence
# of the same thing depending on who asked. Thirty proposals, zero approved,
# because reading about a memory promoted it.
# ==========================================================================

def _line(log, ts, returned, caller):
    rec = {
        "ts": ts.isoformat(), "resident": "claudette", "query": "q",
        "subject_filter": None, "raw_ids": returned,
        "distances": [0.1] * len(returned), "returned_ids": returned,
        "caller": caller,
    }
    with open(log.path, "a") as f:
        f.write(json.dumps(rec) + "\n")


def test_only_service_reads_feed_heat(tmp_path):
    """THE defect, in one assertion. Same memory, same window, four reads —
    only the one serving someone else's turn counts."""
    log = RetrievalLog(tmp_path / "r.jsonl", resident="claudette")
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    _line(log, now - timedelta(hours=1), ["hot"], CALLER_SERVICE)
    _line(log, now - timedelta(hours=2), ["hot"], CALLER_SELF_QUERY)
    _line(log, now - timedelta(hours=3), ["hot"], CALLER_CONSOLIDATION)
    _line(log, now - timedelta(hours=4), ["hot"], "daydream")
    assert log.reference_counts(window_days=30, now=now) == {"hot": 1}


def test_reviewing_a_candidate_does_not_promote_it(tmp_path):
    """The 2026-07-28 slate in miniature: nine of ten items were in her
    surfaced block AS she reviewed them. Self-query reads must be inert."""
    log = RetrievalLog(tmp_path / "r.jsonl", resident="claudette")
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    for i in range(20):
        _line(log, now - timedelta(minutes=i), ["candidate"], CALLER_SELF_QUERY)
    assert log.reference_counts(window_days=30, now=now) == {}


def test_legacy_lines_are_unattributable_not_service(tmp_path):
    """Pre-v2 lines have no caller. They must NOT be charitably counted —
    provenance cannot be backfilled, so they are excluded, and the rent epoch
    restarts on honest data."""
    log = RetrievalLog(tmp_path / "r.jsonl", resident="claudette")
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    with open(log.path, "a") as f:
        f.write(json.dumps({
            "ts": (now - timedelta(days=1)).isoformat(), "resident": "claudette",
            "query": "q", "subject_filter": None, "raw_ids": ["old"],
            "distances": [0.1], "returned_ids": ["old"],
        }) + "\n")
    assert log.read()[0].caller is None
    assert log.reference_counts(window_days=30, now=now) == {}


def test_default_caller_is_none_not_service(tmp_path):
    """A call site that never decided must produce an unattributable line.
    Failing open to `service` would rebuild the v1 loop by accident."""
    log = RetrievalLog(tmp_path / "r.jsonl", resident="gable")
    rec = log.log("q", None, ["a"], [0.1], ["a"])
    assert rec.caller is None
    assert rec.feeds_heat is False


def test_default_caller_applies_when_call_site_is_silent(tmp_path):
    log = RetrievalLog(tmp_path / "r.jsonl", resident="gable",
                       default_caller=CALLER_CONSOLIDATION)
    assert log.log("q", None, ["a"], [0.1], ["a"]).caller == CALLER_CONSOLIDATION


def test_explicit_caller_beats_default(tmp_path):
    log = RetrievalLog(tmp_path / "r.jsonl", resident="gable",
                       default_caller=CALLER_CONSOLIDATION)
    rec = log.log("q", None, ["a"], [0.1], ["a"], caller=CALLER_SERVICE)
    assert rec.caller == CALLER_SERVICE


def test_unknown_caller_is_fatal_at_write(tmp_path):
    """A typo'd caller would look like a legitimate no-heat read forever.
    Fail the write rather than mislabel provenance that cannot be fixed."""
    log = RetrievalLog(tmp_path / "r.jsonl", resident="gable")
    with pytest.raises(UnknownCaller):
        log.log("q", None, ["a"], [0.1], ["a"], caller="servce")
    assert not log.path.exists()


def test_unknown_default_caller_is_fatal_at_construction(tmp_path):
    with pytest.raises(UnknownCaller):
        RetrievalLog(tmp_path / "r.jsonl", resident="g", default_caller="nope")


def test_unknown_caller_on_read_is_tolerated(tmp_path):
    """Read side stays tolerant — a bad line already on disk must not make
    the whole log unreadable. It simply never feeds heat."""
    log = RetrievalLog(tmp_path / "r.jsonl", resident="g")
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    _line(log, now, ["a"], "garbage-caller")
    assert log.read()[0].caller == "garbage-caller"
    assert log.reference_counts(window_days=30, now=now) == {}


def test_caller_breakdown_keeps_the_ratio_visible(tmp_path):
    """Claudette's 07-28 ruling: self_query keeps its own value so the
    self-search : organic-surfacing ratio stays inspectable, rather than
    being absorbed into one 'doesn't count' bucket."""
    log = RetrievalLog(tmp_path / "r.jsonl", resident="claudette")
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    _line(log, now, ["a"], CALLER_SERVICE)
    _line(log, now, ["a"], CALLER_SELF_QUERY)
    _line(log, now, ["a"], CALLER_SELF_QUERY)
    with open(log.path, "a") as f:
        f.write(json.dumps({"ts": now.isoformat(), "returned_ids": ["a"]}) + "\n")
    assert log.caller_breakdown(window_days=30, now=now) == {
        "service": 1, "self_query": 2, "unattributed": 1,
    }


def test_callers_override_measures_without_changing_promotion(tmp_path):
    log = RetrievalLog(tmp_path / "r.jsonl", resident="claudette")
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    _line(log, now, ["a"], CALLER_SELF_QUERY)
    assert log.reference_counts(30, now=now) == {}
    assert log.reference_counts(
        30, now=now, callers=frozenset({CALLER_SELF_QUERY})) == {"a": 1}

"""RetrievalLog: write/parse, unified schema fields, legacy tolerance,
reference_counts windowing."""

import json
from datetime import datetime, timedelta, timezone

from house_memory import RetrievalLog, read_records


def test_write_and_parse(tmp_path):
    log = RetrievalLog(tmp_path / "retrieval.jsonl", resident="gable")
    rec = log.log(
        query="what did plink say",
        subject_filter="plink",
        raw_ids=["a", "b"],
        distances=[0.1, None],
        returned_ids=["a"],
    )
    # on-disk line carries the full unified schema
    line = json.loads((tmp_path / "retrieval.jsonl").read_text().strip())
    assert set(line) == {
        "ts", "resident", "query", "subject_filter", "raw_ids", "distances", "returned_ids"
    }
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

    def line(ts, returned):
        rec = {
            "ts": ts.isoformat(),
            "resident": "claudette",
            "query": "q",
            "subject_filter": None,
            "raw_ids": returned,
            "distances": [0.1] * len(returned),
            "returned_ids": returned,
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
        f.write(json.dumps({"ts": "not-a-date", "query": "q", "returned_ids": ["z"]}) + "\n")
    assert log.reference_counts(window_days=30) == {}

#!/usr/bin/env python3
"""Stub container-launch script for WP-H9 tests.

Stands in for run-resident.sh. Records its argv (minus argv[0]) and the prompt
it received on stdin to the JSON file named by RESIDENCY_STUB_RECORD, then
prints a canned session result on stdout. Behaviour is tunable by env so tests
can exercise the failure/timeout paths too:

  RESIDENCY_STUB_RECORD   path to write {"argv": [...], "stdin": "..."}
  RESIDENCY_STUB_STDOUT   exact stdout to emit (default: a canned JSON result)
  RESIDENCY_STUB_EXIT     exit code (default 0)
  RESIDENCY_STUB_SLEEP    seconds to sleep before responding (default 0)

Stream mode (BL-G1) — stand in for `claude -p --output-format stream-json`:

  RESIDENCY_STUB_STREAM      JSON array of event objects; each is emitted as
                             one line, flushed, in order. Overrides
                             RESIDENCY_STUB_STDOUT.
  RESIDENCY_STUB_LINE_SLEEP  seconds to sleep between stream lines, so a test
                             can prove the launcher killed the session BETWEEN
                             the init event and the reply.
  RESIDENCY_STUB_COMPLETED   path to create once every stream line has been
                             written. Its ABSENCE after a run is the evidence
                             that the pre-act gate killed the session before it
                             could produce a reply.
"""

import json
import os
import sys
import time

DEFAULT_STDOUT = json.dumps({"result": "Hello, this is Gable.", "num_turns": 4})


def _emit_stream(raw: str) -> None:
    events = json.loads(raw)
    line_sleep = float(os.environ.get("RESIDENCY_STUB_LINE_SLEEP", "0") or "0")
    for i, event in enumerate(events):
        if i and line_sleep:
            time.sleep(line_sleep)
        sys.stdout.write(json.dumps(event) + "\n")
        sys.stdout.flush()
    done = os.environ.get("RESIDENCY_STUB_COMPLETED")
    if done:
        with open(done, "w", encoding="utf-8") as fh:
            fh.write("completed")


def main() -> int:
    stdin_data = sys.stdin.read()
    record = os.environ.get("RESIDENCY_STUB_RECORD")
    if record:
        with open(record, "w", encoding="utf-8") as fh:
            json.dump({"argv": sys.argv[1:], "stdin": stdin_data}, fh)

    sleep = float(os.environ.get("RESIDENCY_STUB_SLEEP", "0") or "0")
    if sleep:
        time.sleep(sleep)

    stream = os.environ.get("RESIDENCY_STUB_STREAM")
    if stream:
        _emit_stream(stream)
    else:
        sys.stdout.write(os.environ.get("RESIDENCY_STUB_STDOUT", DEFAULT_STDOUT))
        sys.stdout.flush()
    return int(os.environ.get("RESIDENCY_STUB_EXIT", "0") or "0")


if __name__ == "__main__":
    raise SystemExit(main())

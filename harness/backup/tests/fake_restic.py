"""Stand-in for restic: snapshots are plain copies under $FAKE_RESTIC_REPO, restored at their absolute paths."""

import datetime as dt
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

VALUED = {"--tag", "--retry-lock", "--target", "--keep-daily", "--keep-weekly", "--keep-monthly"}

repo = Path(os.environ["FAKE_RESTIC_REPO"])
snaps = repo / "snaps"
snaps.mkdir(parents=True, exist_ok=True)
with open(repo / "calls.log", "a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\n")

cmd, rest = sys.argv[1], sys.argv[2:]
opts, args = {}, []
i = 0
while i < len(rest):
    if rest[i] in VALUED:
        opts[rest[i]] = rest[i + 1]
        i += 2
    else:
        args.append(rest[i])
        i += 1

if cmd == "backup":
    sid = uuid.uuid4().hex
    for p in map(Path, args):
        dest = snaps / sid / p.relative_to("/")
        dest.parent.mkdir(parents=True, exist_ok=True)
        (shutil.copytree if p.is_dir() else shutil.copy2)(p, dest)
    when = os.environ.get("FAKE_RESTIC_TIME") or dt.datetime.now(dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.123456789Z")
    (snaps / f"{sid}.json").write_text(json.dumps({"id": sid, "time": when, "tags": [opts["--tag"]]}))
elif cmd == "snapshots":
    print(json.dumps([json.loads(p.read_text()) for p in snaps.glob("*.json")]))
elif cmd == "restore":
    shutil.copytree(snaps / args[0], opts["--target"], dirs_exist_ok=True)

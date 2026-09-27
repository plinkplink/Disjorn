"""Stand-in for the sqlite3 CLI: `.backup 'dest'` and single SQL statements, rows printed |-joined."""

import sqlite3
import sys

db, cmd = sys.argv[1], sys.argv[2]
src = sqlite3.connect(db)
if cmd.startswith(".backup"):
    dest = sqlite3.connect(cmd.split(None, 1)[1].strip("'\""))
    src.backup(dest)
    dest.close()
else:
    for row in src.execute(cmd):
        print("|".join("" if v is None else str(v) for v in row))

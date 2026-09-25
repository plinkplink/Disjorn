import sys
from pathlib import Path

BACKUP = Path(__file__).resolve().parents[1]
HOUSE_MEMORY = BACKUP.parent / "house_memory"
sys.path[:0] = [str(BACKUP), str(HOUSE_MEMORY)]

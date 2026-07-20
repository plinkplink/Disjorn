"""Make harness/metrics/metrics.py importable in the metrics test suite,
independently of any install. No network, no chromadb — the producer parses
JSON-lines directly."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

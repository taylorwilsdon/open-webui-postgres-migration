"""Make the repository root importable so ``import migrate`` works.

pytest's default ``prepend`` import mode puts ``tests/`` on ``sys.path``, not
the project root, so without this ``pytest tests/`` fails at collection.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

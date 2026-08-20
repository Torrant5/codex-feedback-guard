"""Shared test bootstrap: put the package root on sys.path.

Imported (not collected) by every test module so `import exi.*` works when the
suite is run via unittest from any cwd.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

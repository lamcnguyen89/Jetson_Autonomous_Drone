"""SITL tooling sharing the flight implementation in the adjacent src directory."""

from pathlib import Path
import sys

# Flight modules currently use top-level imports (core, commands, camera, ...).
# Resolve their location from this package rather than the working directory.
_source_root = str(Path(__file__).resolve().parent.parent / "src")
if _source_root not in sys.path:
    sys.path.insert(0, _source_root)

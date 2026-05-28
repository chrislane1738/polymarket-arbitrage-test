"""Pytest configuration. Ensures project root is on sys.path so tests can
import top-level packages (analytics, feeds, store)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

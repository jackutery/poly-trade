import sys
from pathlib import Path

# Allow imports like `from core.state import StateStore` from any test
sys.path.insert(0, str(Path(__file__).resolve().parent))

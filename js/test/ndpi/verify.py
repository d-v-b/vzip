"""Checks the browser NDPI virtualizer against tifffile.

The NDPI fixtures in fixtures/ndpi/ go through the same check as the
TIFFs (js/test/tiff/verify.py): every level must equal what tifffile reads.

Usage: uv run python js/test/ndpi/verify.py
"""

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location("tiff_verify", HERE.parent / "tiff" / "verify.py")
tiff_verify = sys.modules["tiff_verify"] = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tiff_verify)

if __name__ == "__main__":
    sys.exit(tiff_verify.main(HERE.parents[2] / "fixtures" / "ndpi"))

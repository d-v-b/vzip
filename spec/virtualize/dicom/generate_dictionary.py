"""Writes python/src/vzip/virtualize/dicom/dictionary.json, the data dictionary that
the DICOM convention applies to elements whose VR the file does not state
(spec/virtualize/dicom.md §5).

    uv run python spec/virtualize/dicom/generate_dictionary.py

The table is PS3.6's registry of data elements as pydicom ships it: for each
VR, its tags as one string of 8-digit uppercase hexadecimal tags in ascending
order, and the repeating-group masks (`x` matches any hexadecimal digit). Tags
whose VR is ambiguous (`US or SS`, `OB or OW`, ...) or not a VR (`NONE`) are
left out: such elements stay `UN`. The file is normative: regenerating it
from another pydicom version changes the convention, and its version.
"""

from __future__ import annotations

import json
from pathlib import Path

import pydicom
from pydicom._dicom_dict import DicomDictionary, RepeatersDictionary

VRS = {"AE", "AS", "AT", "CS", "DA", "DS", "DT", "FD", "FL", "IS", "LO", "LT", "OB", "OD", "OF", "OL", "OV",
       "OW", "PN", "SH", "SL", "SQ", "SS", "ST", "SV", "TM", "UC", "UI", "UL", "UN", "UR", "US", "UT", "UV"}
OUT = Path(__file__).parents[3] / "python" / "src" / "vzip" / "virtualize" / "dicom" / "dictionary.json"


def main() -> None:
    tags: dict[str, str] = {}
    for t, v in sorted(DicomDictionary.items()):
        if v[0] in VRS and v[0] != "UN":
            tags[v[0]] = tags.get(v[0], "") + f"{t:08X}"
    tags = dict(sorted(tags.items()))
    masks = {m.upper().replace("X", "x"): v[0] for m, v in sorted(RepeatersDictionary.items())
             if v[0] in VRS and v[0] != "UN"}
    doc = {"source": f"PS3.6 as shipped by pydicom {pydicom.__version__}", "tags": tags, "masks": masks}
    OUT.write_text(json.dumps(doc, separators=(",", ":"), sort_keys=False) + "\n")
    print(f"wrote {sum(len(s) // 8 for s in tags.values())} tags and {len(masks)} masks to {OUT}")


if __name__ == "__main__":
    main()

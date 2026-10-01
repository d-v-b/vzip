#!/usr/bin/env python3
# @license
# Copyright 2026 Google Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Generates the vzip test archives.

Uses the reference vzip writer from the repository containing this fork
(`src/refstore`). From the repository root:

    uv run python neuroglancer/testdata/kvstore/vzip/generate_vzip.py
"""

from __future__ import annotations

import io
import struct
from pathlib import Path

from refstore.archive import VZipWriter
from refstore.pb import Concat, Range, Source

HERE = Path(__file__).parent
FILES = HERE.parent / "files"  # the files the generic kvstore tests expect
OME_ZARR = HERE.parents[1] / "datasource" / "zarr" / "ome_zarr"


def files_blob() -> dict[str, tuple[int, int]]:
    """Writes files.blob: every test file's bytes, with gaps; returns offsets."""
    layout, blob = {}, bytearray(b"\0" * 7)
    for path in sorted(p for p in FILES.rglob("*") if p.is_file()):
        key = path.relative_to(FILES).as_posix()
        data = path.read_bytes()
        layout[key] = (len(blob), len(data))
        blob += data + b"\0" * 5
    (HERE / "files.blob").write_bytes(bytes(blob))
    return layout


def write_files(name: str, layout: dict[str, tuple[int, int]], page_size=None) -> None:
    """The generic test files, each stored a different way."""
    buf = io.BytesIO()
    w = VZipWriter(buf, page_size=page_size)
    blob = w.url("files.blob")  # relative to the archive
    content = {k: (FILES / k).read_bytes() for k in layout}
    for key, (offset, length) in layout.items():
        data = content[key]
        if key == "a":  # Concat: a literal byte, then a url range
            w.add_ranges(key, [Range(data=data[:1]),
                               Range(source=blob, offset=offset + 1, length=length - 1)])
        elif key == "c":  # a plain bytes entry (pinned when paged)
            w.add_bytes(key, data, late=page_size is not None)
        elif key == "empty":  # an empty Concat
            w.add_ranges(key, [])
        elif key == "baz/first":  # a key source naming a deflated hidden entry
            hidden = w.internal(w.add_hidden("first", data, compress=True))
            w.add_ranges(key, [Range(source=hidden, offset=0, length=len(data))])
        elif key == "baz/x":  # a data source
            w.add_ranges(key, [Range(source=w.blob(data), offset=0, length=len(data))])
        else:  # a url range
            w.add_ref(key, "files.blob", offset, length)
    w.close()
    (HERE / name).write_bytes(buf.getvalue())


def write_pins(layout: dict[str, tuple[int, int]]) -> None:
    """Pins that pass and fail against the test HTTP server (spec §6.1)."""
    off, n = layout["b"]
    cases = {
        "size_ok": Source(url="files.blob", size=(HERE / "files.blob").stat().st_size),
        "size_wrong": Source(url="files.blob", size=1),
        "mtime_ok": Source(url="files.blob", modified_not_after=4102444800),  # 2100
        "mtime_too_early": Source(url="files.blob", modified_not_after=0),
        "etag_wrong": Source(url="files.blob", etag='"not-the-etag"'),
    }
    buf = io.BytesIO()
    w = VZipWriter(buf)
    for key, src in cases.items():
        w.add_ranges(key, [Range(source=w.source(src), offset=off, length=n)])
    w.close()
    (HERE / "pins.vzip").write_bytes(buf.getvalue())


def write_errors() -> None:
    """One key per error class of spec §8.4."""
    buf = io.BytesIO()
    w = VZipWriter(buf)
    ok = w.url("files.blob")
    missing = w.url("no-such-file.blob")
    ghost = w.source(Source(key="ghost"))
    w._skip_checks = True  # writers must reject the dangling key source
    w.add_bytes("ok", b"fine")
    # entry error: an extra field that does not parse
    w._entry("entry_error", b"x", b"\x01\x02\x03")
    # payload error: a truncated varint
    w._entry("payload_error", b"", struct.pack("<HH", 0x7A76, 2) + b"\x18\xcf")
    w._ref_names.add("payload_error")
    # resolution errors
    w.add_ranges("missing_url", [Range(source=missing, offset=0, length=4)])
    w.add_ranges("missing_key", [Range(source=ghost, offset=0, length=1)])
    w.add_ranges("past_end", [Range(source=ok, offset=0, length=10**6)])
    # the window [0, 2) touches only the literal part: no resolution happens
    w.add_ranges("partial", [Range(data=b"hi"), Range(source=missing, offset=0, length=4)])
    w.close()
    (HERE / "errors.vzip").write_bytes(buf.getvalue())


def write_future_version() -> None:
    buf = io.BytesIO()
    w = VZipWriter(buf)
    w.add_bytes("a", b"abc")
    w.close()
    data = bytearray(buf.getvalue())
    data[len(data) - 22 : len(data) - 16] = b"vzip/9"
    (HERE / "future_version.vzip").write_bytes(bytes(data))


def write_ome_zarr() -> None:
    """simple_0.5 as a vzip: metadata as bytes, chunks as references to the
    dataset's own chunk files (relative URLs)."""
    root = OME_ZARR / "simple_0.5"
    buf = io.BytesIO()
    w = VZipWriter(buf)
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        key = path.relative_to(root).as_posix()
        if path.name == "zarr.json":
            w.add_bytes(key, path.read_bytes(), late=True)
        else:
            w.add_ref(key, f"simple_0.5/{key}", 0, path.stat().st_size)
    w.close()
    (OME_ZARR / "simple_0.5.vzip").write_bytes(buf.getvalue())


if __name__ == "__main__":
    layout = files_blob()
    write_files("files.vzip", layout)
    write_files("files_paged.vzip", layout, page_size=64)
    write_pins(layout)
    write_errors()
    write_future_version()
    write_ome_zarr()
    print("wrote", sorted(p.name for p in HERE.glob("*.vzip")), "and simple_0.5.vzip")

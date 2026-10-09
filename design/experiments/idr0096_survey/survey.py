"""Checks the browser virtualizer's archives for IDR idr0096 against tifffile.

For each archive written by virtualize_all.ts:

1. Structure: every pyramid level's shape and chunk shape, and every tile
   reference (offset, length), must equal what tifffile reads from the TIFF's
   own directories; the only source must be the TIFF's URL, unpinned.
2. Samples: three tiles are downloaded (the largest at full resolution, the
   largest at a middle level, one at the coarsest level) and decoded with
   imagecodecs (OpenJPEG). They are written to <out>/tiles/ for wasm_check.mjs,
   which decodes them with the browser's JPEG 2000 decoder.

Usage: uv run python design/experiments/idr0096_survey/survey.py <virtualize jsonl> <archive dir> <out dir>
"""

from __future__ import annotations

import json
import struct
import sys
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import fsspec
import imagecodecs
import numpy as np
import tifffile

from vzip.pb import Range, decode_source_table

RANGE_ID = 0x7A76


def archive_refs(path: Path):
    z = zipfile.ZipFile(path)
    refs = {}
    for info in z.infolist():
        ex, p = info.extra, 0
        while p < len(ex):
            hid, n = struct.unpack_from("<HH", ex, p)
            if hid == RANGE_ID:
                r = Range.decode(ex[p + 4 : p + 4 + n])
                refs[info.filename] = (r.source, r.offset, r.length)
            p += 4 + n
    group = json.loads(z.read("zarr.json"))
    datasets = group["attributes"]["ome"]["multiscales"][0]["datasets"]
    arrays = [json.loads(z.read(f"{d['path']}/zarr.json")) for d in datasets]
    sources = decode_source_table(z.read("__vz__/sources"))  # zipfile inflates it
    return refs, arrays, sources


def fetch(url: str, offset: int, length: int) -> bytes:
    req = urllib.request.Request(url, headers={"Range": f"bytes={offset}-{offset + length - 1}"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
            if len(data) == length:
                return data
        except Exception:  # noqa: BLE001 - retried
            if attempt == 3:
                raise
    raise RuntimeError(f"short read at {offset}")


def check(row: dict, archive_dir: Path, tiles: Path) -> dict:
    i, url = row["i"], row["url"]
    out = {"i": i, "url": url, "problems": [], "samples": []}
    refs, arrays, sources = archive_refs(archive_dir / f"{i}.vzip")
    src = sources[0]
    if len(sources) != 1 or src.url != url or src.size is not None or src.etag is not None:
        out["problems"].append(f"unexpected source table {sources}")
    with fsspec.filesystem("http").open(url, block_size=1 << 16, cache_type="readahead") as f:
        tf = tifffile.TiffFile(f)
        levels = tf.series[0].levels
        if len(levels) != len(arrays):
            out["problems"].append(f"{len(arrays)} levels, tifffile has {len(levels)}")
        expected = {}
        for li, (lvl, arr) in enumerate(zip(levels, arrays)):
            p = lvl.pages[0]
            if list(lvl.shape) != arr["shape"]:
                out["problems"].append(f"level {li}: shape {arr['shape']} vs {list(lvl.shape)}")
            chunk = arr["chunk_grid"]["configuration"]["chunk_shape"]
            if chunk != [1, p.tilelength, p.tilewidth]:
                out["problems"].append(f"level {li}: chunk {chunk} vs tile {p.tilelength}x{p.tilewidth}")
            c, h, w = lvl.shape
            ty, tx = -(-h // p.tilelength), -(-w // p.tilewidth)
            for k, (o, n) in enumerate(zip(p.dataoffsets, p.databytecounts)):
                if n:
                    ci, rest = divmod(k, ty * tx)
                    yi, xi = divmod(rest, tx)
                    expected[f"{li}/c/{ci}/{yi}/{xi}"] = (0, int(o), int(n))
        got = {k: v for k, v in refs.items()}
        if got != expected:
            missing = sorted(set(expected) - set(got))[:3]
            extra = sorted(set(got) - set(expected))[:3]
            wrong = [k for k in expected if k in got and got[k] != expected[k]][:3]
            out["problems"].append(f"references differ: missing {missing}, extra {extra}, wrong {wrong}")
        out["references"] = len(got)
    # Samples: the largest tile at full resolution, the largest at a middle
    # level, and one at the coarsest level.
    by_level: dict[int, list] = {}
    for k, (_, o, n) in got.items():
        by_level.setdefault(int(k.split("/")[0]), []).append((n, k, o))
    last = max(by_level)
    picks = [max(by_level[0]), max(by_level[last // 2]), max(by_level[last])]
    for n, key, o in picks:
        data = fetch(url, o, n)
        name = f"{i}_{key.replace('/', '_')}"
        (tiles / f"{name}.j2k").write_bytes(data)
        try:
            img = imagecodecs.jpeg2k_decode(data)
        except Exception as e:  # noqa: BLE001
            out["problems"].append(f"{key}: imagecodecs cannot decode: {e}")
            continue
        (tiles / f"{name}.raw").write_bytes(np.ascontiguousarray(img).tobytes())
        (tiles / f"{name}.json").write_text(json.dumps({"shape": list(img.shape), "dtype": str(img.dtype)}))
        out["samples"].append({"key": key, "bytes": n, "shape": list(img.shape)})
    return out


def main(jsonl: str, archive_dir: str, out_dir: str) -> None:
    rows = [json.loads(line) for line in open(jsonl)]
    rows = {r["i"]: r for r in rows}  # later lines (retries) win
    out = Path(out_dir)
    tiles = out / "tiles"
    tiles.mkdir(parents=True, exist_ok=True)
    todo = [r for r in rows.values() if r["ok"]]
    results = []
    with ThreadPoolExecutor(8) as pool:
        for res in pool.map(lambda r: _safe(check, r, Path(archive_dir), tiles), todo):
            results.append(res)
            status = "ok" if not res["problems"] else res["problems"]
            print(f"{res['i']:3d} {res['url'].rsplit('/', 1)[-1][:45]:45s} {status}", flush=True)
    (out / "survey.json").write_text(json.dumps(results, indent=1))
    bad = [r for r in results if r["problems"]]
    print(f"\n{len(results)} archives checked, {len(bad)} with problems, "
          f"{sum(len(r['samples']) for r in results)} sample tiles decoded by imagecodecs")


def _safe(fn, row, *args):
    try:
        return fn(row, *args)
    except Exception as e:  # noqa: BLE001 - reported per file
        return {"i": row["i"], "url": row["url"], "problems": [f"check crashed: {type(e).__name__}: {e}"], "samples": []}


if __name__ == "__main__":
    main(*sys.argv[1:])

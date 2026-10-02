"""Writes small synthetic Imaris (IMS) files to web/test/fixtures/ims/ with
h5py, covering the rules of the IMS profile (profiles/ims.md §8): both HDF5
file formats (h5py's default, the earliest, and libver="latest": old and new
object headers, symbol-table and compact or dense groups, compact and dense
attributes, version 1 B-tree, single chunk and fixed array chunk indexes,
paged fixed arrays), tiny B-tree nodes, huge attributes, deflate and
uncompressed data, uint8, int16, uint16 and float32 in both byte orders,
pyramids, time points and channels, dimensions padded to whole chunks,
unallocated chunks, and absolute soft links (Imaris 10); and the inputs it
rejects (`ims_reject_*`).

web/test/ims/verify.py checks the virtualized files against h5py.

Usage: uv run python web/test/ims/write_fixtures.py
"""

from __future__ import annotations

import ctypes
from pathlib import Path

import h5py
import numpy as np

OUT = Path(__file__).parents[1] / "fixtures" / "ims"

rng = np.random.default_rng(0)


def text(value: str | bytes) -> np.ndarray:
    """An attribute as Imaris writes it: an array of 1-character strings."""
    raw = value.encode() if isinstance(value, str) else value
    return np.frombuffer(raw, dtype="S1")


def open_file(name: str, libver: str = "earliest", small_k: bool = False, sizes: tuple[int, int] | None = None):
    path = OUT / f"{name}.ims"
    if not small_k and sizes is None:
        return h5py.File(path, "w", libver=libver)
    fcpl = h5py.h5p.create(h5py.h5p.FILE_CREATE)
    if small_k:
        # Two entries per symbol table node and group B-tree node, and four per
        # chunk B-tree node: deep version 1 B-trees. h5py does not wrap these
        # setters, so call the HDF5 library it bundles.
        here = Path(h5py.__file__).parent
        lib = ctypes.CDLL(str(next(p for p in [*here.glob(".dylibs/libhdf5.*"), *here.parent.glob("h5py.libs/libhdf5-*")]
                                   if "_hl" not in p.name)))
        if lib.H5Pset_sym_k(ctypes.c_int64(fcpl.id), 1, 1) < 0 or lib.H5Pset_istore_k(ctypes.c_int64(fcpl.id), 2) < 0:
            raise RuntimeError("H5Pset_sym_k or H5Pset_istore_k failed")
    if sizes is not None:
        fcpl.set_sizes(*sizes)
    fapl = h5py.h5p.create(h5py.h5p.FILE_ACCESS)
    fapl.set_libver_bounds(h5py.h5f.LIBVER_EARLIEST, h5py.h5f.LIBVER_V18)
    fid = h5py.h5f.create(str(path).encode(), h5py.h5f.ACC_TRUNC, fcpl=fcpl, fapl=fapl)
    return h5py.File(fid)


def data_array(dims, dtype) -> np.ndarray:
    if np.dtype(dtype).kind == "f":
        return (rng.random(dims) * 100 - 20).astype(dtype)
    info = np.iinfo(np.dtype(dtype))
    return rng.integers(max(info.min, -300), min(info.max, 3000), dims, endpoint=True).astype(dtype)


def write_data(group, dims, chunks, dtype, *, compression=None, skip=(), maxshape=None, **kw):
    """A Data dataset of `dims`, written chunk by chunk except the chunks in `skip`."""
    d = group.create_dataset("Data", shape=dims, chunks=chunks, dtype=dtype, compression=compression,
                             maxshape=maxshape, **kw)
    values = data_array(dims, dtype)
    for index in np.ndindex(*[-(-n // c) for n, c in zip(dims, chunks)]):
        if index in skip:
            continue
        sl = tuple(slice(i * c, min((i + 1) * c, n)) for i, c, n in zip(index, chunks, dims))
        d[sl] = values[sl]
    return d


def imaris(f, levels: list[dict], times: int = 1, channels: int = 1, dtype="u2", compression=None,
           image: dict | None = None, channel_meta: list[dict] | None = None, time_meta: dict | None = None,
           skip: dict | None = None, extras: bool = True, **kw) -> None:
    """The Imaris layout. Each level is {"size": (z, y, x), "dims": (z, y, x), "chunks": (z, y, x)}."""
    f.attrs["ImarisDataSet"] = text("ImarisDataSet")
    f.attrs["ImarisVersion"] = text("5.5.0")
    f.attrs["DataSetDirectoryName"] = text("DataSet")
    f.attrs["DataSetInfoDirectoryName"] = text("DataSetInfo")
    f.attrs["NumberOfDataSets"] = np.array([1], dtype="u4")
    for r, level in enumerate(levels):
        for t in range(times):
            for c in range(channels):
                g = f.create_group(f"DataSet/ResolutionLevel {r}/TimePoint {t}/Channel {c}")
                z, y, x = level["size"]
                g.attrs["ImageSizeX"] = text(str(x))
                g.attrs["ImageSizeY"] = text(str(y))
                g.attrs["ImageSizeZ"] = text(str(z))
                write_data(g, level["dims"], level["chunks"], level.get("dtype", dtype), compression=compression,
                           skip=(skip or {}).get((r, t, c), ()), **kw)
                if extras:
                    g.attrs["HistogramMin"] = text("0.000")
                    g.attrs["HistogramMax"] = text("255.000")
                    g.create_dataset("Histogram", data=np.arange(16, dtype="u8"))
    if image is not None:
        g = f.create_group("DataSetInfo/Image")
        for k, v in image.items():
            g.attrs[k] = text(v) if isinstance(v, (str, bytes)) else v
    for c, meta in enumerate(channel_meta or []):
        g = f.create_group(f"DataSetInfo/Channel {c}")
        for k, v in meta.items():
            g.attrs[k] = text(v) if isinstance(v, (str, bytes)) else v
    if time_meta is not None:
        g = f.create_group("DataSetInfo/TimeInfo")
        for k, v in time_meta.items():
            g.attrs[k] = text(v)
    if extras:
        f.create_group("Thumbnail").create_dataset("Data", data=np.zeros((4, 16), dtype="u1"))


def extents(size, lo=(0.0, 0.0, 0.0), voxel=(0.25, 0.25, 1.0), unit: str | bytes = "um") -> dict:
    z, y, x = size
    out = {"X": str(x), "Y": str(y), "Z": str(z), "Unit": unit, "Name": "synthetic", "Description": "test image",
           "RecordingDate": "2021-03-04 05:06:07.000"}
    for i, n in enumerate((x, y, z)):
        out[f"ExtMin{i}"] = repr(lo[i])
        out[f"ExtMax{i}"] = repr(lo[i] + n * voxel[i])
    return out


def timestamps(times: int, start: str = "2021-03-04 05:06:07", step: float = 2.5) -> dict:
    out = {"DatasetTimePoints": str(times), "FileTimePoints": str(times)}
    for t in range(times):
        seconds = 7 + t * step
        out[f"TimePoint{t + 1}"] = f"{start[:17]}{int(seconds):02d}.{round((seconds % 1) * 1000):03d}"
    return out


def accepted() -> None:
    # Earliest format: superblock 0, version 1 object headers, symbol-table
    # groups, version 1 B-tree chunk indexes. Two levels, 2 time points, 2
    # channels, uint16, deflate, padded dimensions and an unallocated chunk.
    with open_file("ims_earliest_uint16_deflate") as f:
        imaris(f, [{"size": (5, 12, 14), "dims": (6, 16, 16), "chunks": (2, 8, 8)},
                   {"size": (3, 6, 7), "dims": (4, 8, 8), "chunks": (2, 4, 4)}],
               times=2, channels=2, dtype="<u2", compression="gzip",
               image=extents((5, 12, 14), lo=(10.5, -3.0, 2.0), voxel=(0.2, 0.2, 0.5)),
               channel_meta=[{"Name": "DAPI", "Color": "0.000 0.200 1.000", "ColorRange": "100.000 2500.000",
                              **{f"Extra{k}": f"value {k}" for k in range(40)}},
                             {"Name": "GFP", "Color": "0.192 0.878 0.192", "ColorRange": "0 4095"}],
               time_meta=timestamps(2), skip={(0, 1, 0): [(0, 1, 1)], (1, 0, 1): [(1, 1, 1)]})

    # Latest format: superblock 3, version 2 object headers, compact groups,
    # dense attributes (more than 8; in Channel 0, enough for an indirect
    # fractal heap block and a version 2 B-tree of depth 1), fixed array and
    # single chunk indexes.
    # 3 levels, 3 channels, uint8, unit "µm" in Latin-1.
    with open_file("ims_latest_uint8", "latest") as f:
        imaris(f, [{"size": (4, 9, 11), "dims": (4, 12, 12), "chunks": (4, 4, 4)},
                   {"size": (2, 5, 6), "dims": (2, 8, 8), "chunks": (2, 8, 8)},
                   {"size": (1, 3, 3), "dims": (1, 4, 4), "chunks": (1, 4, 4)}],
               channels=3, dtype="u1",
               image=extents((4, 9, 11), voxel=(0.5, 0.5, 2.0), unit=b"\xb5m"),
               channel_meta=[{"Name": "red", "Color": "1 0 0", "ColorRange": "0 255", "Gain": "1", "Pinhole": "0",
                              "Offset": "0", "Min": "0", "Max": "255", "Description": "", "GammaCorrection": "1",
                              "ColorOpacity": "1", **{f"Extra{k}": f"value {k}" for k in range(60)}},
                             {"Name": "", "Color": "0.5 0.5 0.5"},
                             {"Name": "far red", "Color": "1.5 0 0", "ColorRange": "20 30 40"}],
               skip={(0, 0, 2): [(0, 2, 0)]})

    # Latest format, big-endian float32 with deflate, 3 time points, no unit
    # (micrometres), one z plane (no z axis), an unallocated chunk.
    with open_file("ims_latest_float32_be", "latest") as f:
        image = extents((1, 7, 9), lo=(-5.0, 4.0, 0.0), voxel=(1.5, 1.5, 1.0))
        del image["Unit"]
        imaris(f, [{"size": (1, 7, 9), "dims": (1, 8, 12), "chunks": (1, 4, 4)}], times=3, dtype=">f4",
               compression="gzip", image=image, channel_meta=[{"Name": "intensity", "ColorRange": "-1.5 80.25"}],
               time_meta=timestamps(3, step=0.125), skip={(0, 2, 0): [(0, 1, 2)]})

    # Fifty time points: dense links (a fractal heap and a version 2 B-tree
    # of depth 1) in ResolutionLevel 0, and dense attributes in TimeInfo,
    # whose last time point is not a valid date (no time scale).
    with open_file("ims_latest_dense_links", "latest") as f:
        times = timestamps(50)
        times["TimePoint50"] = "2021-02-30 00:00:00"
        imaris(f, [{"size": (1, 2, 3), "dims": (1, 2, 4), "chunks": (1, 2, 4)}], times=50, dtype="<u2",
               image=extents((1, 2, 3)), time_meta=times, extras=False)

    # A fixed array of 2560 chunks in three pages, the middle one never written.
    with open_file("ims_latest_paged", "latest") as f:
        skip = {(0, 0, 0): [(0, y, x) for y in range(1, 79) for x in range(32)]}
        imaris(f, [{"size": (1, 80, 63), "dims": (1, 80, 64), "chunks": (1, 1, 2)}], dtype="u1", skip=skip)

    # Version 1 B-trees two entries wide: multi-level group and chunk B-trees
    # (superblock version 1, for the non-default chunk B-tree width).
    with open_file("ims_small_k", small_k=True) as f:
        imaris(f, [{"size": (3, 5, 6), "dims": (3, 6, 6), "chunks": (1, 2, 2)}], channels=5, dtype="<u2",
               image=extents((3, 5, 6)), skip={(0, 0, 3): [(1, 1, 1), (2, 2, 2)]}, extras=False)

    # Superblock version 2 (libver="v108"), no DataSetInfo, a single 2-D
    # image of int16; ImageSize with whitespace.
    with open_file("ims_2d_no_metadata", "v108") as f:
        imaris(f, [{"size": (1, 5, 6), "dims": (1, 8, 8), "chunks": (1, 8, 8)}], dtype="<i2")
        f["DataSet/ResolutionLevel 0/TimePoint 0/Channel 0"].attrs["ImageSizeX"] = text(" 6\t")

    # A huge attribute (beyond the fractal heap's largest managed object),
    # UTF-8 names, and a level of one filtered chunk (single chunk index).
    with open_file("ims_latest_huge_attribute", "latest") as f:
        image = extents((2, 6, 6), unit="nm")
        image["Description"] = "x" * 6000
        image["Name"] = "Bild ä"
        imaris(f, [{"size": (2, 6, 6), "dims": (2, 8, 8), "chunks": (2, 4, 4)},
                   {"size": (1, 3, 3), "dims": (1, 4, 4), "chunks": (1, 4, 4)}], channels=2, dtype="u1",
               compression="gzip", image=image, channel_meta=[{"Name": "Kanal α"}, {"Name": "Kanal β"}])


    # The layout of Imaris 10: the root group's DataSet and DataSetInfo are
    # soft links to absolute paths.
    with open_file("ims_latest_soft_links", "latest") as f:
        imaris(f, [{"size": (1, 5, 6), "dims": (1, 8, 8), "chunks": (1, 4, 4)}], channels=2, dtype="u1",
               image=extents((1, 5, 6)), channel_meta=[{"Name": "first"}, {"Name": "second"}], extras=False)
        f.create_group("Workflows/InitialImages")
        for name in ("DataSet", "DataSetInfo"):
            f.move(name, f"Workflows/InitialImages/{name}")
            f[name] = h5py.SoftLink(f"/Workflows/InitialImages/{name}")


def rejected() -> None:
    level = [{"size": (2, 6, 7), "dims": (2, 8, 8), "chunks": (2, 4, 4)}]

    with h5py.File(OUT / "ims_reject_not_imaris.ims", "w") as f:
        f.create_dataset("data", data=np.arange(12, dtype="u1").reshape(3, 4), chunks=(3, 4))
    with open_file("ims_reject_shuffle", "latest") as f:
        imaris(f, level, extras=False, compression="gzip", shuffle=True)
    with open_file("ims_reject_fletcher32", "latest") as f:
        imaris(f, level, extras=False, fletcher32=True)
    with open_file("ims_reject_fill_value", "latest") as f:
        imaris(f, level, extras=False, fillvalue=5)
    with open_file("ims_reject_float64", "latest") as f:
        imaris(f, level, extras=False, dtype="<f8")
    with open_file("ims_reject_extensible", "latest") as f:
        imaris(f, level, extras=False, maxshape=(None, 8, 8))
    with open_file("ims_reject_offset_size", sizes=(4, 8)) as f:
        imaris(f, level, extras=False)
    with open_file("ims_reject_size_mismatch", "latest") as f:
        imaris(f, level, extras=False, channels=2)
        f["DataSet/ResolutionLevel 0/TimePoint 0/Channel 1"].attrs["ImageSizeX"] = text("6")
    with open_file("ims_reject_chunk_mismatch", "latest") as f:
        imaris(f, level, extras=False, channels=2)
        g = f["DataSet/ResolutionLevel 0/TimePoint 0/Channel 1"]
        del g["Data"]
        write_data(g, (2, 8, 8), (2, 8, 4), "u2")
    with open_file("ims_reject_dtype_mismatch", "latest") as f:
        imaris(f, level, extras=False, channels=2)
        g = f["DataSet/ResolutionLevel 0/TimePoint 0/Channel 1"]
        del g["Data"]
        write_data(g, (2, 8, 8), (2, 4, 4), "u1")
    with open_file("ims_reject_image_larger", "latest") as f:
        imaris(f, level, extras=False)
        f["DataSet/ResolutionLevel 0/TimePoint 0/Channel 0"].attrs["ImageSizeY"] = text("9")
    with open_file("ims_reject_imagesize_text", "latest") as f:
        imaris(f, level, extras=False)
        f["DataSet/ResolutionLevel 0/TimePoint 0/Channel 0"].attrs["ImageSizeX"] = text("7a")
    with open_file("ims_reject_imagesize_vlen", "latest") as f:
        imaris(f, level, extras=False)
        f["DataSet/ResolutionLevel 0/TimePoint 0/Channel 0"].attrs["ImageSizeX"] = 7
    with open_file("ims_reject_missing_channel", "latest") as f:
        imaris(f, level + [{"size": (1, 3, 4), "dims": (1, 4, 4), "chunks": (1, 4, 4)}], channels=2, extras=False)
        del f["DataSet/ResolutionLevel 1/TimePoint 0/Channel 1"]
    with open_file("ims_reject_contiguous", "latest") as f:
        imaris(f, level, extras=False)
        g = f["DataSet/ResolutionLevel 0/TimePoint 0/Channel 0"]
        del g["Data"]
        g.create_dataset("Data", data=np.zeros((2, 8, 8), dtype="u2"))
    with open_file("ims_reject_rank", "latest") as f:
        imaris(f, level, extras=False)
        g = f["DataSet/ResolutionLevel 0/TimePoint 0/Channel 0"]
        del g["Data"]
        g.create_dataset("Data", data=np.zeros((8, 8), dtype="u2"), chunks=(4, 4))
    with open_file("ims_reject_relative_soft_link", "latest") as f:
        imaris(f, level, extras=False)
        g = f["DataSet/ResolutionLevel 0/TimePoint 0/Channel 0"]
        g.move("Data", "Pixels")
        g["Data"] = h5py.SoftLink("Pixels")
    with open_file("ims_reject_z_levels", "latest") as f:
        imaris(f, [{"size": (1, 6, 7), "dims": (1, 8, 8), "chunks": (1, 4, 4)},
                   {"size": (2, 3, 4), "dims": (2, 4, 4), "chunks": (1, 4, 4)}], extras=False)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for p in OUT.glob("ims_*.ims"):
        p.unlink()
    accepted()
    rejected()
    total = 0
    for p in sorted(OUT.glob("ims_*.ims")):
        total += p.stat().st_size
        print(p.name, p.stat().st_size)
    print("total", total)

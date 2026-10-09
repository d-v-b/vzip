"""Writes small synthetic Imaris (IMS) files to fixtures/ims/ with
h5py, covering the rules of the IMS profile (spec/virtualize.md §8): both HDF5
file formats (h5py's default, the earliest, and libver="latest": old and new
object headers, symbol-table and compact or dense groups, compact and dense
attributes, version 1 B-tree, single chunk and fixed array chunk indexes,
paged fixed arrays), tiny B-tree nodes, huge attributes, deflate and
uncompressed data, uint8, int16, uint16 and float32 in both byte orders,
pyramids, time points and channels, dimensions padded to whole chunks,
unallocated chunks, and absolute soft links (Imaris 10); and the inputs it
rejects (`ims_reject_*`).

js/test/ims/verify.py checks the virtualized files against h5py.

Usage: uv run python fixtures/generators/ims/write_fixtures.py
"""

from __future__ import annotations

import ctypes
import struct
from pathlib import Path

import h5py
import numpy as np

OUT = Path(__file__).parents[2] / "ims"

rng = np.random.default_rng(0)

# Reproducible files: HDF5 stamps an object's header with its creation and modification
# times unless its creation property list says not to. h5py's create_group and
# create_dataset say not to (track_times=False), but the property lists made here for the
# low-level calls, and those h5py makes for a virtual dataset, take HDF5's default (on):
# every dataset and group creation list made through h5py.h5p.create is made untimed.
_create = h5py.h5p.create


def _untimed(cls):
    plist = _create(cls)
    if isinstance(plist, (h5py.h5p.PropDCID, h5py.h5p.PropGCID)):
        plist.set_obj_track_times(False)
    return plist


h5py.h5p.create = _untimed


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

    # A 2-D image in chunks of 4 z planes: the z axis stays, of size 1, so
    # that each chunk's bytes still decode to its Zarr chunk.
    with open_file("ims_2d_deep_chunks", "v108") as f:
        imaris(f, [{"size": (1, 5, 6), "dims": (4, 8, 8), "chunks": (4, 8, 8)}], dtype="u1")

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


def reconstruction() -> None:
    """Files with the HDF5 objects of every kind that the source metadata
    node maps (spec/virtualize/ims.md §5), in both file formats."""
    for libver in ("earliest", "latest"):
        with open_file(f"ims_{libver}_reconstruction", libver) as f:
            imaris(f, [{"size": (2, 5, 6), "dims": (2, 8, 8), "chunks": (2, 4, 4)}], channels=2, dtype="u1",
                   image=extents((2, 5, 6)), channel_meta=[{"Name": "a"}, {"Name": "b"}])
            x = f.create_group("Extra")
            x.attrs["vlen text"] = "variable ✓"
            x.attrs["vlen texts"] = np.array(["one", "two", ""], dtype=h5py.string_dtype())
            x.attrs["scalar"] = np.float64(2.5)
            x.attrs["matrix"] = np.arange(6, dtype=">i2").reshape(2, 3)
            x.attrs["long"] = np.arange(100, dtype="f4")
            x.attrs["fixed"] = np.array([b"ab", b"cde"], dtype="S3")
            x.attrs["compound"] = np.array([(1, 2.0)], dtype=[("i", "<i4"), ("f", "<f8")])
            x.attrs["sequence"] = np.array([np.arange(3, dtype="i2"), np.arange(1, dtype="i2")],
                                           dtype=h5py.vlen_dtype(np.dtype("i2")))
            x.create_dataset("contiguous", data=np.arange(24, dtype=">f8").reshape(2, 3, 4))
            x.create_dataset("compact", data=np.arange(5, dtype="i1"),
                             dcpl=_compact(np.arange(5, dtype="i1")))
            x.create_dataset("scalar", data=np.uint32(7))
            x.create_dataset("empty", shape=(0, 4), dtype="u2")
            x.create_dataset("unallocated", shape=(3,), dtype="i4", fillvalue=-1)
            x.create_dataset("deflate", data=np.arange(300, dtype="<u2").reshape(10, 30), chunks=(4, 8),
                             compression="gzip", fillvalue=9)
            x.create_dataset("half", data=np.linspace(0, 1, 5, dtype="f2"))
            table = np.array([(i, i / 2, b"n%d" % i) for i in range(4)],
                             dtype=[("id", "<i8"), ("x", "<f4"), ("name", "S4")])
            x.create_dataset("table", data=table, chunks=(2,))
            x.create_dataset("enum", data=np.array([0, 1, 1], dtype="u1"),
                             dtype=h5py.enum_dtype({"off": 0, "on": 1}, basetype="u1"))
            x.create_dataset("points", shape=(2,), dtype=np.dtype(("<f4", (2, 3))))[...] = \
                np.arange(12, dtype="<f4").reshape(2, 2, 3)
            x.create_dataset("opaque", data=np.void(b"\x01\x02\x03"))
            x.create_dataset("names", data=np.array(["α", "beta", ""], dtype=h5py.string_dtype()))
            x.create_dataset("ragged", data=np.array([np.arange(2, dtype="<i4"), np.arange(5, dtype="<i4")],
                                                     dtype=h5py.vlen_dtype(np.dtype("<i4"))), chunks=(1,))
            x["contiguous"].attrs["unit"] = "nm"
            x["alias"] = x["contiguous"]
            x["soft"] = h5py.SoftLink("/Extra/contiguous")
            x["external"] = h5py.ExternalLink("other.h5", "/data")
            commit_type(x, "typedef", [("a", "<u2"), ("b", "<u2")])
            x.create_group("nested/deeper").attrs["level"] = np.int8(2)


def imaris_text(obj, name: str | bytes, value: bytes) -> None:
    """An attribute exactly as Imaris writes it: 1-character null-terminated ASCII strings."""
    tid = h5py.h5t.C_S1.copy()
    tid.set_strpad(h5py.h5t.STR_NULLTERM)
    data = np.frombuffer(value, dtype="S1")
    space = h5py.h5s.create_simple((len(data),))
    aid = h5py.h5a.create(obj.id, name.encode() if isinstance(name, str) else name, tid, space)
    aid.write(data, mtype=tid)


def review() -> None:
    """Files with the HDF5 objects that an adversarial review found the source
    metadata node to lose (spec/virtualize/ims.md §5): datatypes and shapes
    of attributes, named datatypes, references, external storage and links,
    null dataspaces, fill values, names that are not UTF-8 or collide, names
    that are not Zarr node names, and documents over the attribute budget."""
    for libver in ("earliest", "latest"):
        with open_file(f"ims_{libver}_review", libver) as f:
            imaris(f, [{"size": (2, 5, 6), "dims": (2, 8, 8), "chunks": (2, 4, 4)}], dtype="u1", extras=False)
            f.attrs["forward"] = f.create_group("Z").ref  # a reference to an object the walk meets later
            x = f.create_group("X")
            # Attributes of every class, shape and padding.
            x.attrs["i32"] = np.array([1, 2], dtype="<i4")
            x.attrs["f64"] = np.array([1, 2], dtype=">f8")
            x.attrs["u8s"] = np.uint8(3)
            x.attrs["s10"] = np.array([b"ab", b"cd"], dtype="S10")
            x.attrs["s2"] = np.array([b"ab", b"cd"], dtype="S2")
            x.attrs["s1_2d"] = np.array([[b"a", b"b"], [b"c", b"d"]], dtype="S1")
            x.attrs["s1_trail"] = np.array([b"a", b"b", b"", b""], dtype="S1")
            x.attrs["s3_nul"] = np.array([b"a\0b"], dtype="S3")
            imaris_text(x, "imaris", b"2.5 micrometer")
            imaris_text(x, "imaris_nul", b"a\0b")
            x.attrs["vstr"] = np.array(["ab", "cd"], dtype=h5py.string_dtype())
            x.attrs["vstr_ascii"] = np.array([b"ab", b"cd"], dtype=h5py.string_dtype("ascii"))
            x.attrs["u64"] = np.array([2**64 - 1, 2**53, 3], dtype="<u8")
            x.attrs["i64"] = np.array([-2**63, -2**53 - 1], dtype=">i8")
            x.attrs["f16"] = np.array([np.nan, np.inf, -0.0, 65504, 6e-8], dtype="<f2")
            x.attrs["f32"] = np.array([0.1, -np.inf], dtype=">f4")
            x.attrs["enum"] = np.array([0, 1], dtype=h5py.enum_dtype({"a": 0, "b": 2**62}, basetype="u8"))
            x.attrs["bool"] = np.array([True, False])
            x.attrs["vv"] = np.array([np.array([1.5, 2], dtype="f4"), np.array([], dtype="f4")],
                                     dtype=h5py.vlen_dtype("f4"))
            x.attrs["cplx"] = np.array([1 + 2j])
            x.attrs["arr"] = np.zeros((2,), dtype=np.dtype(("<i2", (2, 2))))
            x.attrs["nested"] = np.zeros((1,), dtype=[("a", [("b", "<f4", (3,)), ("c", "S3")]),
                                                      ("e", h5py.enum_dtype({"x": 1}, basetype="i1"))])
            bits = h5py.h5a.create(x.id, b"bits", h5py.h5t.STD_B8LE, h5py.h5s.create_simple((3,)))
            bits.write(np.arange(3, dtype="u1"), mtype=h5py.h5t.STD_B8LE)
            x.attrs["null"] = h5py.Empty("f4")
            x.attrs["ref"] = f["DataSet"].ref
            x.attrs["refs"] = np.array([f["DataSet"].ref, x.ref, h5py.Reference()], dtype=h5py.ref_dtype)
            x.attrs.create(b"n\xe9", np.int8(1))  # not UTF-8
            x.attrs.create("n\u00e9", np.int8(2))  # reads like it, and comes first
            x.attrs.create("n\u00c3A", np.int8(3))  # comes after b"n\xc3A", which is not UTF-8 and reads like it
            x.attrs.create(b"n\xc3A", np.int8(4))
            # Named datatypes, used by a dataset and an attribute before the walk meets them.
            commit_type(x, "zT", "<i2")
            x["zT"].attrs["note"] = np.int32(5)
            x.create_dataset("a_typed", data=np.arange(3, dtype="<i2"), dtype=x["zT"])
            x.attrs.create("typed", np.arange(2, dtype="<i2"), dtype=x["zT"])
            # References, external storage, nested variable-length data and region references.
            x.create_dataset("refs", data=np.array([f["DataSet"].ref, x.ref, f["Z"].ref, h5py.Reference()],
                                                   dtype=h5py.ref_dtype))
            x.create_dataset("external", shape=(4,), dtype="<i4", external=[("external.bin", 0, 16)])
            x["external"].attrs["kept"] = np.int8(1)
            x.create_dataset("cvlen", data=np.array([(1, "hello"), (2, "world")],
                                                    dtype=[("a", "<i4"), ("s", h5py.string_dtype())]))
            x.create_dataset("regions", data=np.array([x["refs"].regionref[1:2]]), dtype=h5py.regionref_dtype)
            # Null dataspaces, fill values, empty and two-dimensional variable-length data.
            x.create_dataset("null", data=h5py.Empty("f4"))
            x.create_dataset("strfill", shape=(3,), dtype="S4", fillvalue=b"N/A")
            x.create_dataset("nanfill", shape=(3,), dtype="<f2", fillvalue=np.nan)
            x.create_dataset("negzero", shape=(3,), dtype=">f8", fillvalue=-0.0)
            x.create_dataset("f32fill", shape=(3,), dtype=">f4", fillvalue=0.1)
            x.create_dataset("shuffled", data=np.arange(100, dtype="<i4"), chunks=(10,), compression="gzip",
                             shuffle=True)
            x["shuffled"].attrs["kept"] = "yes"
            x.create_dataset("allempty", data=np.array(["", ""], dtype=h5py.string_dtype()))
            x.create_dataset("noelements", shape=(0,), dtype=h5py.string_dtype())
            x.create_dataset("vv2d", data=np.array([[np.arange(2, dtype="i1"), np.arange(1, dtype="i1")],
                                                    [np.arange(0, dtype="i1"), np.arange(3, dtype="i1")]],
                                                   dtype=object), dtype=h5py.vlen_dtype("i1"))
            x.create_dataset("bitfield", data=np.arange(3, dtype="u1"), dtype=h5py.h5t.STD_B8LE)
            x.create_dataset("edge", data=np.arange(7 * 5, dtype="<u2").reshape(7, 5), chunks=(3, 2),
                             compression="gzip")
            # Links, and names that are not UTF-8, collide or are not Zarr node names.
            x["elsewhere"] = h5py.ExternalLink("other.h5", "/data")
            x["loop"] = h5py.SoftLink("/X/loop")
            x["cycle"] = x
            x.create_group(b"caf\xc3\xa9")
            x.create_group(b"caf\xe9")  # reads like the one before
            x.create_group(b"lat\xe9n").attrs["kept"] = np.int8(1)
            x.create_group("zarr.json").attrs["kept"] = np.int8(1)
            x.create_group("c").create_dataset("zarr.json", data=np.arange(3))
            x.create_group("__reserved")
            commit_type(x, "zarr.json type", "<u1")
            # A document over the attribute budget: Imaris's long protocol texts.
            custom = f.create_group("DataSetInfo/CustomData")
            for i in range(12):
                imaris_text(custom, f"Protocol {i:02d}", (b"%02d " % i) * 2600)
            custom.attrs["Small"] = np.int8(1)
    # Integer fill values beyond 2^53, in a directory of their own: the documents of
    # python/tests/test_virtualize_conventions.py pass through JavaScript's JSON.parse, which
    # rounds them (compare.py and verify.py read them exactly).
    (OUT / "exact").mkdir(exist_ok=True)
    with open_file("exact/ims_big_fill", "latest") as f:
        imaris(f, [{"size": (2, 5, 6), "dims": (2, 8, 8), "chunks": (2, 4, 4)}], dtype="u1", extras=False)
        f.create_dataset("bigfill", shape=(3,), dtype="<i8", fillvalue=2**60 + 1)
        f.create_dataset("u64fill", shape=(3,), dtype="<u8", fillvalue=2**64 - 1)
        f.create_dataset("negfill", shape=(3,), dtype=">i8", fillvalue=-2**63)


def indexes() -> None:
    """The chunk indexes of HDF5's newer format (spec/virtualize.md §8.5): an
    extensible array (one unlimited dimension), a version 2 B-tree (several),
    implicit (allocated early) and fixed arrays at maximum dimensions, with and
    without filters, for the image and for other datasets."""
    with open_file("ims_latest_extensible", "latest") as f:
        imaris(f, [{"size": (2, 6, 7), "dims": (2, 8, 8), "chunks": (2, 4, 4)}], dtype="u2", maxshape=(None, 8, 8),
               skip={(0, 0, 0): [(0, 1, 0)]}, extras=False)
    with open_file("ims_latest_btree2", "latest") as f:
        imaris(f, [{"size": (2, 6, 7), "dims": (2, 8, 8), "chunks": (1, 2, 2)}], dtype="u1", compression="gzip",
               maxshape=(None, None, None), skip={(0, 0, 0): [(1, 1, 1)]}, extras=False)
    with open_file("ims_latest_indexes", "latest") as f:
        imaris(f, [{"size": (2, 5, 6), "dims": (2, 8, 8), "chunks": (2, 4, 4)}], dtype="u1", extras=False)
        g = f.create_group("Indexes")
        # Extensible arrays: enough chunks for super blocks, the unlimited dimension
        # first, last, and filtered; a hole that is never written.
        ea = g.create_dataset("ea", shape=(700,), maxshape=(None,), chunks=(1,), dtype="<i2")
        ea[:300] = np.arange(300)
        ea[400:] = -np.arange(300)
        g.create_dataset("ea_last", data=np.arange(6 * 40, dtype="<u4").reshape(6, 40), maxshape=(6, None),
                         chunks=(4, 3))
        g.create_dataset("ea_deflate", data=np.arange(500, dtype="<f8"), maxshape=(None,), chunks=(7,),
                         compression="gzip", fletcher32=True)
        # Version 2 B-trees: two unlimited dimensions, deep enough for internal nodes.
        bt = g.create_dataset("bt2", shape=(40, 50), maxshape=(None, None), chunks=(2, 3), dtype="<i4")
        bt[:30, :] = np.arange(30 * 50).reshape(30, 50)
        g.create_dataset("bt2_deflate", data=np.arange(10 * 10, dtype=">u2").reshape(10, 10), maxshape=(None, None),
                         chunks=(5, 5), compression="gzip", shuffle=True)
        # An implicit index: chunks allocated early, in order.
        dcpl = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
        dcpl.set_chunk((10,))
        dcpl.set_alloc_time(h5py.h5d.ALLOC_TIME_EARLY)
        dcpl.set_fill_time(h5py.h5d.FILL_TIME_ALLOC)
        did = h5py.h5d.create(g.id, b"implicit", h5py.h5t.STD_I32LE, h5py.h5s.create_simple((95,)), dcpl=dcpl)
        g["implicit"][:] = np.arange(95)
        did.close()
        dcpl = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
        dcpl.set_chunk((4, 4))
        dcpl.set_alloc_time(h5py.h5d.ALLOC_TIME_EARLY)
        did = h5py.h5d.create(g.id, b"implicit_max", h5py.h5t.STD_U8LE, h5py.h5s.create_simple((6, 7), (10, 13)),
                              dcpl=dcpl)
        g["implicit_max"][:] = np.arange(42, dtype="u1").reshape(6, 7)
        did.close()
        # A fixed array at maximum dimensions larger than the dimensions.
        g.create_dataset("farray_max", data=np.arange(30, dtype="<i8").reshape(5, 6), maxshape=(9, 11), chunks=(2, 2))


def review2() -> None:
    """Files with what the second adversarial review found (spec/virtualize/ims.md
    §5): documents over their budget (shared datatypes, references, hard links),
    a string datatype of no bytes, dimension scales, references inside other
    datatypes, compound datasets with variable-length members, filters,
    region references, a virtual dataset and NaNs that JSON does not keep."""
    for libver in ("earliest", "latest"):
        path = OUT / f"ims_{libver}_review2.ims"
        with open_file(f"ims_{libver}_review2", libver) as f:
            imaris(f, [{"size": (2, 5, 6), "dims": (2, 8, 8), "chunks": (2, 4, 4)}], dtype="u1", extras=False)
            # A shared compound datatype of 200 members, used by 120 attributes.
            commit_type(f, "T", [(f"member_{i:03d}", "u1") for i in range(200)])
            g = f.create_group("Shared")
            for i in range(120):
                h5py.h5a.create(g.id, f"a{i:03d}".encode(), f["T"].id, h5py.h5s.create(h5py.h5s.SCALAR))
            # Long paths: references to them, and hard links to them, over the budget.
            deep = f.create_group("Deep")
            for i in range(4):
                deep = deep.create_group("n" * 200 + str(i))
            r = f.create_group("Refs")
            r.attrs.create("many", np.array([deep.ref] * 300), dtype=h5py.ref_dtype)
            r.attrs.create("few", np.array([deep.ref, h5py.Reference()]), dtype=h5py.ref_dtype)
            r.create_dataset("refs", data=np.array([deep.ref, f["Deep"].ref, h5py.Reference()] * 50),
                             dtype=h5py.ref_dtype)
            h = f.create_group("Hard")
            for i in range(100):
                h[f"l{i:03d}"] = deep
            # Attributes over the document's budget: their entries are spilled.
            many = f.create_group("Many")
            for i in range(300):
                many.attrs[f"{'k' * 250}{i:03d}"] = np.int8(i % 100)
            # A string datatype of no bytes (h5py cannot write one: in the earliest format, the attribute's
            # size is patched below).
            f.create_group("Empty").attrs.create("zz", np.array([b"QWERTYU"] * 3), dtype="S7")
            # Dimension scales, references inside a compound and a variable-length datatype.
            x = f.create_group("X")
            sc = x.create_dataset("scale", data=np.arange(10.0))
            sc.make_scale("x")
            dd = x.create_dataset("withdim", data=np.arange(10))
            dd.dims[0].attach_scale(sc)
            x.attrs.create("nested", np.array([((1, x.ref), [x.ref, sc.ref])],
                                              dtype=[("a", [("i", "<i4"), ("r", h5py.ref_dtype)]),
                                                     ("b", h5py.ref_dtype, (2,))]))
            # Compound datasets with variable-length and reference members.
            dt = np.dtype([("a", "<i4"), ("s", h5py.string_dtype()), ("v", h5py.vlen_dtype("<i2")),
                           ("r", h5py.ref_dtype)])
            cv = x.create_dataset("cvlen", (3,), dtype=dt)
            cv[0] = (1, "x", np.arange(2, dtype="<i2"), x.ref)
            cv[1] = (2, "yy", np.arange(0, dtype="<i2"), sc.ref)
            x.create_dataset("cvlen_chunked", data=np.array([(5, "a", np.arange(3, dtype="<i2"), h5py.Reference())] * 4,
                                                            dtype=dt), chunks=(3,), compression="gzip")
            # Filters: Fletcher32 alone and after deflate, shuffle alone and with deflate.
            values = np.arange(100, dtype="<i4")
            x.create_dataset("fletcher", data=values, chunks=(30,), fletcher32=True)
            x.create_dataset("fletcher_deflate", data=values, chunks=(30,), compression="gzip", fletcher32=True)
            x.create_dataset("shuffle", data=values, chunks=(30,), shuffle=True)
            x.create_dataset("shuffle_all", data=values.astype(">f8"), chunks=(30,), shuffle=True, compression="gzip",
                             fletcher32=True)
            x.create_dataset("scaleoffset", data=values, chunks=(30,), scaleoffset=0)
            # Region references: an attribute and a dataset; points and hyperslabs.
            tgt = x.create_dataset("target", data=np.arange(60).reshape(6, 10))
            regions = x.create_dataset("regions", (4,), dtype=h5py.regionref_dtype)
            regions[0] = tgt.regionref[1:3, 2:8:2]
            regions[1] = tgt.regionref[...]
            regions[2] = tgt.regionref[1:2, 3:4]
            x.attrs.create("region", tgt.regionref[0:2, 5], dtype=h5py.regionref_dtype)
            space = tgt.id.get_space()
            space.select_elements(np.array([[0, 1], [5, 9], [2, 2]]))
            x.attrs.create("points", h5py.h5r.create(tgt.id, b".", h5py.h5r.DATASET_REGION, space),
                           dtype=h5py.regionref_dtype)
            # NaNs: the canonical quiet NaN, which JSON keeps, and others, which it does not.
            x.attrs["nan"] = np.array([np.nan, 1.5], dtype="<f4")
            x.attrs["nan_payload"] = np.frombuffer(struct.pack("<II", 0x7FC00001, 0xFFC00000), dtype="<f4")
            x.attrs["negzero"] = np.array([-0.0], dtype=">f8")
        # A virtual dataset of two sources.
        with h5py.File(path, "a") as f:
            layout = h5py.VirtualLayout(shape=(2, 10), dtype="<i8")
            layout[0] = h5py.VirtualSource(f["X/withdim"])
            layout[1, :5] = h5py.VirtualSource("other.h5", "/data", shape=(5,))
            f.create_virtual_dataset("X/virtual", layout)
        if libver == "earliest":  # version 1 object headers have no checksum to update
            data = bytearray(path.read_bytes())
            pattern = bytes([0x13, 1, 0, 0, 7, 0, 0, 0])  # the S7 datatype message: its size becomes 0
            assert data.count(pattern) == 1, path
            data[data.find(pattern) + 4] = 0
            path.write_bytes(bytes(data))


def _implicit(group, name: str, shape, chunks, maxshape=None) -> None:
    """A uint8 dataset whose chunks are allocated early, so that HDF5 indexes them implicitly."""
    dcpl = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
    dcpl.set_chunk(chunks)
    dcpl.set_alloc_time(h5py.h5d.ALLOC_TIME_EARLY)
    space = h5py.h5s.create_simple(shape, maxshape or shape)
    h5py.h5d.create(group.id, name.encode(), h5py.h5t.NATIVE_UINT8, space, dcpl=dcpl)
    group[name][...] = (np.arange(int(np.prod(shape))) % 251).astype("u1").reshape(shape)


def review3() -> None:
    """Files with what the third adversarial review found (spec/virtualize/ims.md
    §4, §5): region references sharing a global heap object, and over the budget
    of selections; object references in a spilled document; shuffled
    variable-length data, which HDF5 leaves unshuffled (filter mask bit 0); a
    chunk of references too large to decode; tiny chunks indexed implicitly;
    attributes whose data the file holds in one run; and names and channels the
    root does not list."""
    with open_file("ims_latest_review3", "latest") as f:
        imaris(f, [{"size": (2, 5, 6), "dims": (2, 8, 8), "chunks": (2, 4, 4)}], dtype="u1", extras=False)
        deep = f.create_group("Deep")
        for i in range(4):
            deep = deep.create_group("n" * 200 + str(i))
        d = deep.create_dataset("d", data=np.arange(100, dtype="u1").reshape(10, 10))
        line = deep.create_dataset("line", data=np.zeros(20000, dtype="u1"))
        r = f.create_group("R")
        # Region references: 40 elements of one global heap object, and an attribute.
        regions = r.create_dataset("regions", (40,), dtype=h5py.regionref_dtype)
        one = d.regionref[1:3, 2:8:2]
        for i in range(40):
            regions[i] = one
        r.attrs.create("region", d.regionref[[0, 2], 1:3], dtype=h5py.regionref_dtype)
        # 400 elements of one selection of 11000 points (about 20 KB): over the budget of selections.
        over = r.create_dataset("over", (400,), dtype=h5py.regionref_dtype)
        many = line.regionref[sorted(np.random.default_rng(1).choice(20000, 11000, replace=False).tolist())]
        for i in range(400):
            over[i] = many
        # Variable-length data with shuffle, which HDF5 skips (filter mask bit 0).
        names = r.create_dataset("names", (4,), dtype=h5py.string_dtype(), chunks=(4,), shuffle=True, compression="gzip")
        names[...] = ["a", "bb", "", "dddd"]
        # References in a chunk of more than 2^24 bytes.
        big = r.create_dataset("bigchunk", (1,), maxshape=(None,), chunks=((1 << 21) + 1,), dtype=h5py.ref_dtype,
                               compression="gzip")
        big[0] = d.ref
        # Tiny chunks, indexed implicitly: in row-major order, by rows, and neither.
        _implicit(r, "tiny", (1000,), (1,))
        _implicit(r, "slabs", (6, 4), (2, 4))
        _implicit(r, "rows", (3, 5), (1, 2), (3, 6))
        _implicit(r, "gaps", (3, 4), (1, 2), (3, 8))
        _implicit(r, "blocks", (4, 4), (2, 2))
        # Attribute data in one run: in the object header, a managed and a huge fractal heap object.
        r.attrs["header"] = np.arange(100, dtype=">i2")
        s = f.create_group("Spill")
        for i in range(700):  # the document spills
            s.attrs[f"a{i:04d}"] = np.arange(10, dtype="<u8")
        s.attrs["managed"] = np.arange(200, dtype="<f8")
        s.attrs["huge"] = np.arange(20000, dtype="<u4")
        s.attrs.create("zz", np.array([d.ref, h5py.Reference(), deep.ref]), dtype=h5py.ref_dtype)
    with open_file("ims_latest_long_names", "latest") as f:
        imaris(f, [{"size": (1, 2, 2), "dims": (1, 2, 2), "chunks": (1, 2, 2)}], channels=2, dtype="u1", extras=False,
               image={"Name": "y" * 257}, channel_meta=[{"Name": "x" * 257}, {"Name": "é" * 128}])
    with open_file("ims_latest_many_channels", "latest") as f:
        imaris(f, [{"size": (1, 2, 2), "dims": (1, 2, 2), "chunks": (1, 2, 2)}], channels=65, dtype="u1", extras=False,
               image={"Name": "y" * 256}, channel_meta=[{"Name": "first"}])


def _compact(data: np.ndarray):
    dcpl = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
    dcpl.set_layout(h5py.h5d.COMPACT)
    return dcpl


def commit_type(group, name: str, dtype) -> None:
    """`group[name] = dtype` (a named datatype), untimed: h5py commits a type with HDF5's
    default creation property list, which tracks times, and wraps no other."""
    lib = _hdf5_library()
    lib.H5open()
    lib.H5Pcreate.restype = ctypes.c_int64
    tcpl = lib.H5Pcreate(ctypes.c_int64.in_dll(lib, "H5P_CLS_DATATYPE_CREATE_ID_g"))
    try:
        if lib.H5Pset_obj_track_times(ctypes.c_int64(tcpl), ctypes.c_uint(0)) < 0:
            raise RuntimeError("H5Pset_obj_track_times failed")
        tid = h5py.h5t.py_create(np.dtype(dtype), logical=True)
        if lib.H5Tcommit2(ctypes.c_int64(group.id.id), name.encode(), ctypes.c_int64(tid.id), ctypes.c_int64(0),
                          ctypes.c_int64(tcpl), ctypes.c_int64(0)) < 0:
            raise RuntimeError(f"H5Tcommit2 of {name!r} failed")
    finally:
        lib.H5Pclose(ctypes.c_int64(tcpl))


def _hdf5_library():
    """The HDF5 library h5py bundles, for the setters h5py does not wrap."""
    here = Path(h5py.__file__).parent
    return ctypes.CDLL(str(next(p for p in [*here.glob(".dylibs/libhdf5.*"), *here.parent.glob("h5py.libs/libhdf5-*")]
                                if "_hl" not in p.name)))


def _raw_edges(group, name: str, shape, chunks, dtype, data, maxshape=None, shuffle=False, fletcher32=False) -> None:
    """A deflated dataset whose partial edge chunks HDF5 stores unfiltered (H5Pset_chunk_opts)."""
    dcpl = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
    dcpl.set_chunk(chunks)
    if shuffle:
        dcpl.set_shuffle()
    dcpl.set_deflate(6)
    if fletcher32:
        dcpl.set_fletcher32()
    if _hdf5_library().H5Pset_chunk_opts(ctypes.c_int64(dcpl.id), ctypes.c_uint(2)) < 0:  # H5D_CHUNK_DONT_FILTER_PARTIAL_CHUNKS
        raise RuntimeError("H5Pset_chunk_opts failed")
    space = h5py.h5s.create_simple(shape, maxshape or shape)
    h5py.h5d.create(group.id, name.encode(), dtype, space, dcpl=dcpl)
    group[name][...] = data


def review4() -> None:
    """Files with what the fourth adversarial review found (spec/virtualize/ims.md
    §5.1, §5.4): a time-lapse whose image tree would use up a walk of a fixed
    number of objects, and partial edge chunks stored unfiltered."""
    with open_file("ims_latest_timelapse", "latest") as f:
        levels = [{"size": (1, 4, 4), "dims": (1, 4, 4), "chunks": (1, 4, 4)},
                  {"size": (1, 2, 2), "dims": (1, 2, 2), "chunks": (1, 2, 2)}]
        imaris(f, levels, times=3, channels=2, dtype="u1", image=extents((1, 4, 4)),
               channel_meta=[{"Name": "a"}, {"Name": "b"}], time_meta=timestamps(3))
        for r in range(2):
            for t in range(3):
                for c in range(2):
                    f[f"DataSet/ResolutionLevel {r}/TimePoint {t}/Channel {c}"].create_dataset(
                        "Histogram1024", data=np.arange(1024, dtype="u8"))
    with open_file("ims_latest_raw_edges", "latest") as f:
        imaris(f, [{"size": (2, 5, 6), "dims": (2, 8, 8), "chunks": (2, 4, 4)}], dtype="u1", extras=False)
        g = f.create_group("E")
        i32 = h5py.h5t.STD_I32LE
        _raw_edges(g, "fixed", (10,), (4,), i32, np.arange(10, dtype="<i4"))  # a fixed array index
        _raw_edges(g, "extensible", (10,), (4,), i32, np.arange(10, dtype="<i4"), (h5py.h5s.UNLIMITED,))
        _raw_edges(g, "btree", (10, 7), (4, 4), i32, np.arange(70, dtype="<i4").reshape(10, 7),
                   (h5py.h5s.UNLIMITED, h5py.h5s.UNLIMITED))
        _raw_edges(g, "single", (3,), (4,), i32, np.arange(3, dtype="<i4"), (4,))  # one chunk, itself partial
        _raw_edges(g, "whole", (8,), (4,), i32, np.arange(8, dtype="<i4"))  # no partial chunk: referenced
        _raw_edges(g, "pipeline", (10, 7), (4, 4), h5py.h5t.IEEE_F64BE, np.linspace(0, 1, 70).reshape(10, 7),
                   shuffle=True, fletcher32=True)
        names = h5py.string_dtype()
        _raw_edges(g, "names", (5,), (2,), h5py.h5t.py_create(names, logical=True), ["a", "bb", "", "dddd", "e"])
    with open_file("ims_reject_raw_edges_image", "latest") as f:
        f.attrs["ImarisDataSet"] = text("ImarisDataSet")
        g = f.create_group("DataSet/ResolutionLevel 0/TimePoint 0/Channel 0")
        for axis, n in zip("ZYX", (2, 5, 6)):
            g.attrs[f"ImageSize{axis}"] = text(str(n))
        _raw_edges(g, "Data", (2, 5, 6), (2, 4, 4), h5py.h5t.STD_U8LE, data_array((2, 5, 6), "u1"))


if __name__ == "__main__":
    import sys

    OUT.mkdir(parents=True, exist_ok=True)
    # With arguments, only those writers run, and the other files are kept (h5py
    # does not write the same bytes twice, so regenerating them all changes them).
    writers = {"accepted": accepted, "rejected": rejected, "reconstruction": reconstruction, "review": review,
               "indexes": indexes, "review2": review2, "review3": review3, "review4": review4}
    if len(sys.argv) == 1:
        for p in OUT.glob("ims_*.ims"):
            p.unlink()
    for name in sys.argv[1:] or writers:
        writers[name]()
    total = 0
    for p in sorted(OUT.glob("ims_*.ims")):
        total += p.stat().st_size
        print(p.name, p.stat().st_size)
    print("total", total)

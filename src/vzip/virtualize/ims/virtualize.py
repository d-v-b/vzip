"""The IMS profile (profiles/ims.md, §8)."""

from __future__ import annotations

import math
import re

from vzip.virtualize.common import LENGTHS, UNITS, Output, Reader, Rejected, array_json, root_json, image_ome
from vzip.virtualize.ims.source import source_json
from vzip.virtualize.ims.hdf5 import MAX_SAFE, Hdf5, attribute_value, le

MAX_LEVELS = 64
MAX_DATASETS = 100000
WS = " \t\r\n"
DECIMAL = re.compile(r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?")
DIGITS = re.compile(r"[0-9]+")
TIMESTAMP = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2}) ([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]+))?")


# ---- attribute values (conventions/ims/README.md §2.1)

def text(attrs: dict[bytes, bytes], name: str) -> str | None:
    """The text of a string attribute, or None if it is absent."""
    d = attrs.get(name.encode())
    if d is None:
        return None
    datatype, _, data = attribute_value(d)
    if datatype.cls != 3:
        raise Rejected(f"attribute {name} is not a string")
    raw = data.split(b"\0", 1)[0]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def decimal(s: str | None) -> float | None:
    """A decimal number, or None if `s` is absent or not one."""
    if s is None or not DECIMAL.fullmatch(s.strip(WS)):
        return None
    v = float(s.strip(WS))
    return v if math.isfinite(v) else None


def decimals(s: str | None, n: int) -> list[float] | None:
    """`n` decimal numbers separated by whitespace, or None."""
    if s is None:
        return None
    parts = re.split(f"[{WS}]+", s.strip(WS))
    if len(parts) != n:
        return None
    values = [decimal(p) for p in parts]
    return None if any(v is None for v in values) else values


def days(y: int, m: int, d: int) -> int:
    """Days from 1970-01-01 to y-m-d in the proleptic Gregorian calendar."""
    y -= m <= 2
    era = y // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def timestamp(s: str | None) -> tuple[int, float] | None:
    """(whole seconds from 1970-01-01 00:00:00, fraction of a second), or None (conventions/ims/README.md §3)."""
    m = TIMESTAMP.fullmatch(s) if s is not None else None
    if m is None:
        return None
    y, mo, d, h, mi, sec = (int(m[i]) for i in range(1, 7))
    leap = y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)
    month_days = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    if not (1 <= mo <= 12 and 1 <= d <= month_days[mo - 1] and h <= 23 and mi <= 59 and sec <= 59):
        return None
    return days(y, mo, d) * 86400 + h * 3600 + mi * 60 + sec, float("0." + (m[7] or "0"))


# ---- the Imaris layout (conventions/ims/README.md §2.2)

def virtualize_ims(url: str, read: Reader, size: int) -> Output:
    f = Hdf5(read, size)

    def group(links: dict, name: str, required: bool = True) -> int | None:
        """The object a group's link `name` leads to."""
        if name.encode() not in links:
            if required:
                raise Rejected(f"no {name}")
            return None
        return f.follow(links, name.encode())

    root = f.links(f.root)
    if b"DataSet" not in root:
        raise Rejected("not an Imaris file (no DataSet group)")
    dataset = f.links(group(root, "DataSet"))
    if b"ResolutionLevel 0" not in dataset:
        raise Rejected("not an Imaris file (no ResolutionLevel 0)")
    levels = 0
    while f"ResolutionLevel {levels}".encode() in dataset:
        levels += 1
        if levels > MAX_LEVELS:
            raise Rejected(f"more than {MAX_LEVELS} resolution levels")
    level_links = [f.links(group(dataset, "ResolutionLevel 0"))]
    times = 0
    while f"TimePoint {times}".encode() in level_links[0]:
        times += 1
    if times == 0:
        raise Rejected("no TimePoint 0")
    first_time = f.links(group(level_links[0], "TimePoint 0"))
    channels = 0
    while f"Channel {channels}".encode() in first_time:
        channels += 1
    if channels == 0:
        raise Rejected("no Channel 0")
    if levels * times * channels > MAX_DATASETS:
        raise Rejected(f"more than {MAX_DATASETS} datasets")

    # conventions/ims/README.md §2.2: the datasets of every level, time point and channel.
    data_type = None
    level_info = []  # per level: (sizes z, y, x; chunk z, y, x; compressed)
    chunk_refs: list[tuple[int, int, int, tuple[int, ...], tuple[int, int]]] = []
    channel_groups: list[tuple[str, int]] = []  # for the source metadata
    for r in range(levels):
        links = level_links[0] if r == 0 else f.links(group(dataset, f"ResolutionLevel {r}"))
        info = None
        for t in range(times):
            time_links = first_time if r == 0 and t == 0 else f.links(group(links, f"TimePoint {t}"))
            for c in range(channels):
                channel = group(time_links, f"Channel {c}")
                channel_groups.append((f"ResolutionLevel {r}/TimePoint {t}/Channel {c}", channel))
                attrs = f.attributes(channel)
                sizes = []
                for axis in "ZYX":
                    s = text(attrs, f"ImageSize{axis}")
                    if s is None or not DIGITS.fullmatch(s.strip(WS)) or not 1 <= int(s.strip(WS)) <= MAX_SAFE:
                        raise Rejected(f"ImageSize{axis} of level {r} is not a positive integer")
                    sizes.append(int(s.strip(WS)))
                ds = f.dataset(group(f.links(channel), "Data"))
                dt = _data_type(ds.datatype)
                if len(ds.dims) != 3:
                    raise Rejected(f"a dataset of level {r} is not 3-dimensional")
                if any(s > n for s, n in zip(sizes, ds.dims)):
                    raise Rejected(f"the image of level {r} is larger than its dataset")
                if ds.filters not in ([], [1]):
                    raise Rejected(f"unsupported HDF5 filters {ds.filters}")
                if data_type is None:
                    data_type = dt
                elif dt != data_type:
                    raise Rejected("the datasets have different data types")
                this = (tuple(sizes), tuple(ds.chunk), bool(ds.filters))
                if info is None:
                    info = this
                elif this != info:
                    raise Rejected(f"the datasets of level {r} differ in size, chunk shape or filters")
                for coords, ref in f.chunks(ds).items():
                    chunk_refs.append((r, t, c, coords, ref))
        level_info.append(info)

    # conventions/ims/README.md §3: metadata.
    meta = f.links(group(root, "DataSetInfo")) if b"DataSetInfo" in root else {}

    def meta_attrs(name: str) -> dict[bytes, bytes]:
        at = group(meta, name, False)
        if at is None:
            return {}
        f.links(at)  # it MUST be a group
        return f.attributes(at)

    image = meta_attrs("Image")
    ext_min = [decimal(text(image, f"ExtMin{i}")) for i in range(3)]
    ext_max = [decimal(text(image, f"ExtMax{i}")) for i in range(3)]
    unit_text = text(image, "Unit")
    name = text(image, "Name")
    channel_attrs = [meta_attrs(f"Channel {c}") for c in range(channels)]
    labels, colors, ranges = [], [], []
    for c, attrs in enumerate(channel_attrs):
        label = text(attrs, "Name")
        labels.append(label if label else f"Channel {c}")
        rgb = decimals(text(attrs, "Color"), 3)
        if rgb is not None and all(0 <= v <= 1 for v in rgb):
            colors.append("".join(f"{math.floor(v * 255 + 0.5):02X}" for v in rgb))
        else:
            colors.append("FFFFFF")
        ranges.append(decimals(text(attrs, "ColorRange"), 2))
    time_attrs = meta_attrs("TimeInfo")
    period = None
    if times > 1:
        first, last = timestamp(text(time_attrs, "TimePoint1")), timestamp(text(time_attrs, f"TimePoint{times}"))
        if first is not None and last is not None:
            elapsed = (last[0] - first[0]) + (last[1] - first[1])
            if elapsed > 0:
                period = elapsed / (times - 1)

    # conventions/ims/README.md §4: output; profiles/ims.md §8.8: the chunk references.
    z0 = level_info[0][0][0]
    # A z axis also when chunks hold several z planes, so that they decode to Zarr chunks.
    has_z = z0 > 1 or any(info[1][0] > 1 for info in level_info)
    axes = (["t"] if times > 1 else []) + (["c"] if channels > 1 else []) + (["z"] if has_z else []) + ["y", "x"]
    if z0 == 1 and any(info[0][0] != 1 for info in level_info):
        raise Rejected("a level has more than one z plane, and level 0 has one")
    unit = "micrometer" if not unit_text else UNITS.get(unit_text)
    if unit not in LENGTHS:
        unit = None
    extent = {}
    for axis, i in (("x", 0), ("y", 1), ("z", 2)):
        if ext_min[i] is not None and ext_max[i] is not None:
            e = ext_max[i] - ext_min[i]
            if not math.isfinite(e):
                raise Rejected(f"ExtMax{i} - ExtMin{i} is not finite")
            if e > 0:
                extent[axis] = e
    spatial = [a for a in axes if a in "zyx"]
    units = {"t": "second" if period is not None else None,
             **{a: unit if a in extent else None for a in spatial}}
    scales, arrays = [], []
    for r, (sizes, chunk, compressed) in enumerate(level_info):
        n = dict(zip("zyx", sizes))
        scale = {"t": period if period is not None else 1, "c": 1,
                 **{a: extent[a] / n[a] if a in extent else 1 for a in spatial}}
        scales.append([scale[a] for a in axes])
        shape = {"t": times, "c": channels, **n}
        chunks = {"t": 1, "c": 1, **dict(zip("zyx", chunk))}
        codecs = [{"name": "bytes", "configuration": {"endian": data_type[1]}} if data_type[1] else {"name": "bytes"}]
        if compressed:
            codecs.append({"name": "zlib", "configuration": {"level": 1}})
        arrays.append(array_json([shape[a] for a in axes], data_type[0], [chunks[a] for a in axes], codecs, axes))
    translations = None
    if all(a in extent for a in spatial):
        origin = {"x": ext_min[0], "y": ext_min[1], "z": ext_min[2]}
        translations = [[origin.get(a, 0) for a in axes]] * levels
    ome = image_ome(axes, units, scales, name if name else None, translations)
    lo, hi = data_type[2]
    channels_json = []
    for label, color, rng in zip(labels, colors, ranges):
        window = {}
        if lo is not None:
            start, end = rng if rng is not None else (lo, hi)
            window = {"window": {"min": lo, "max": hi, "start": start, "end": end}}
        elif rng is not None:
            window = {"window": {"min": rng[0], "max": rng[1], "start": rng[0], "end": rng[1]}}
        channels_json.append({"label": label, "color": color, "active": True, **window})
    ome["omero"] = {"channels": channels_json}
    out = Output(url)
    # The source metadata (conventions/ims/README.md §5).
    out.json("zarr.json", root_json(ome, "ims", url, source_json(f, root, meta, channel_groups)))
    for r, array in enumerate(arrays):
        out.json(f"{r}/zarr.json", array)
    for r, t, c, coords, (offset, n) in chunk_refs:
        sizes, chunk, _ = level_info[r]
        if any(i * k >= s for i, k, s in zip(coords, chunk, sizes)):
            continue  # padding, wholly outside the image
        index = {"t": t, "c": c, **dict(zip("zyx", coords))}
        out.refs[f"{r}/c/" + "/".join(str(index[a]) for a in axes)] = [(offset, n)]
    out.summary = {
        "levels": levels,
        "sizes": {"t": times, "c": channels, **dict(zip("zyx", level_info[0][0]))},
        "dataType": data_type[0],
        "chunkShape": list(level_info[0][1]),
        "compressed": [info[2] for info in level_info],
        "chunks": len(out.refs),
        "channels": labels,
    }
    return out


def _data_type(dt) -> tuple[str, str | None, tuple[int | None, int | None]]:
    """(Zarr data type, byte order or None for one byte, integer range) of a Data dataset (conventions/ims/README.md §2.2)."""
    order = "big" if dt.bits & 1 else "little"
    if dt.cls == 0 and dt.size in (1, 2, 4):
        if le(dt.props, 0, 2) != 0 or le(dt.props, 2, 2) != 8 * dt.size:
            raise Rejected("fixed-point data with padding bits")
        bits = 8 * dt.size
        if dt.bits & 8:
            return f"int{bits}", order if bits > 8 else None, (-(2 ** (bits - 1)), 2 ** (bits - 1) - 1)
        return f"uint{bits}", order if bits > 8 else None, (0, 2**bits - 1)
    if dt.cls == 1 and dt.size == 4:
        if (dt.bits & 0x40 or (dt.bits >> 4) & 3 != 2 or (dt.bits >> 8) & 0xFF != 31
                or [le(dt.props, 0, 2), le(dt.props, 2, 2), *dt.props[4:8], le(dt.props, 8, 4)] != [0, 32, 23, 8, 0, 23, 127]):
            raise Rejected("floating-point data that is not IEEE binary32")
        return "float32", order, (None, None)
    raise Rejected(f"unsupported HDF5 datatype (class {dt.cls}, {dt.size} bytes)")

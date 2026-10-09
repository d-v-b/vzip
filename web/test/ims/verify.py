"""Checks the browser IMS virtualizer against h5py.

Each web/test/fixtures/ims/*.ims (or each .ims file given as an argument) is
served over local HTTP and virtualized by web/conformance/virtualize.ts (the
browser code, run under Node). Every level of the archive is read through the
reference reader and zarr-python, and must equal, for every time point and
channel, what h5py reads from `/DataSet/ResolutionLevel r/TimePoint t/Channel
c/Data`, cropped to the channel's ImageSize (unallocated chunks read as 0 in
both). `ims_reject_*` files must be rejected.

Usage: uv run python web/test/ims/verify.py [<file.ims> ...]
"""

from __future__ import annotations

import base64
import json
import math
import os
import struct
import subprocess
import sys
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path

import h5py
import numpy as np
import zarr
from zarr.abc.codec import BytesBytesCodec
from zarr.core.sync import sync
from zarr.registry import register_codec

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "conformance"))
from http_server import Server  # noqa: E402

from vzip.policy import Policy  # noqa: E402
from vzip.store import VZipStore  # noqa: E402

# The sources are served on 127.0.0.1, which spec §8.7 rule 3 refuses by default.
LOOPBACK_SOURCES = Policy(allow_private_hosts=True)


@dataclass(frozen=True)
class ZlibCodec(BytesBytesCodec):
    """'zlib' (as Neuroglancer names it), which zarr-python lacks."""

    is_fixed_size = False
    level: int = 1

    @classmethod
    def from_dict(cls, data):
        return cls(**data.get("configuration", {}))

    def to_dict(self):
        return {"name": "zlib", "configuration": {"level": self.level}}

    async def _decode_single(self, chunk_bytes, chunk_spec):
        return chunk_spec.prototype.buffer.from_bytes(zlib.decompress(chunk_bytes.to_bytes()))

    async def _encode_single(self, chunk_bytes, chunk_spec):
        return chunk_spec.prototype.buffer.from_bytes(zlib.compress(chunk_bytes.to_bytes()))

    def compute_encoded_size(self, input_byte_length, chunk_spec):
        raise NotImplementedError


register_codec("zlib", ZlibCodec)


def image_size(group) -> tuple[int, ...]:
    return tuple(int(b"".join(group.attrs[f"ImageSize{a}"].tolist()).split(b"\0")[0]) for a in "ZYX")


def text(b: bytes):
    """A text value (conventions §6)."""
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return {"latin1": b.decode("latin-1")}


def number(v):
    v = v.item() if hasattr(v, "item") else v
    if isinstance(v, float) and not math.isfinite(v):
        return "NaN" if math.isnan(v) else "Infinity" if v > 0 else "-Infinity"
    return v


def shaped(dims: tuple, values: list):
    if not dims:
        return values[0]
    return values if len(dims) == 1 else {"shape": list(dims), "value": values}


def plain(dt: np.dtype) -> bool:
    """Whether h5py reads a datatype as the numbers that §5 writes as JSON."""
    return dt.kind in "iuf" and dt.names is None and dt.subdtype is None and h5py.check_enum_dtype(dt) is None


def text_bytes(v) -> bytes:
    """The bytes of a text value (conventions §6)."""
    return v["latin1"].encode("latin-1") if isinstance(v, dict) else v.encode("utf-8")


def decode(b: bytes) -> str:
    """A name as text: UTF-8 if valid, else ISO 8859-1."""
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("latin-1")


H5T = h5py.h5t
CLASSES = {H5T.INTEGER: "integer", H5T.FLOAT: "float", H5T.TIME: "time", H5T.STRING: "string",
           H5T.BITFIELD: "bitfield", H5T.OPAQUE: "opaque", H5T.COMPOUND: "compound", H5T.REFERENCE: "reference",
           H5T.ENUM: "enum", H5T.VLEN: "variable-length", H5T.ARRAY: "array"}
IMARIS_TEXT = {"class": "string", "size": 1, "padding": "null-terminated", "charset": "ascii"}
# The reasons of datasets over the limits of conventions/ims/README.md §5.4.
LIMITS = ("a chunk of more than 2^24 bytes to decode", "over the budget of region selections")


def type_ok(d: dict, tid) -> bool:
    """Whether the description `d` (conventions/ims/README.md §5.2) is the datatype `tid`."""
    cls = tid.get_class()
    if cls == H5T.STRING and tid.is_variable_str():
        return (d["class"] == "variable-length" and d.get("string") is True
                and d["padding"] == ["null-terminated", "null-padded", "space-padded"][tid.get_strpad()]
                and d["charset"] == ["ascii", "utf-8"][tid.get_cset()])
    # h5py gives the sizes of variable-length data inside other types in memory.
    if CLASSES.get(cls) != d["class"] or (tid.get_size() != d["size"] and not holds_addresses(d)):
        return False
    order = lambda: "big" if tid.get_order() == H5T.ORDER_BE else "little"  # noqa: E731
    if cls == H5T.INTEGER:
        return d["order"] == order() and d["signed"] == (tid.get_sign() == H5T.SGN_2)
    if cls in (H5T.FLOAT, H5T.BITFIELD, H5T.TIME):
        return d["order"] == order()
    if cls == H5T.STRING:
        return (d["padding"] == ["null-terminated", "null-padded", "space-padded"][tid.get_strpad()]
                and d["charset"] == ["ascii", "utf-8"][tid.get_cset()])
    if cls == H5T.OPAQUE:
        return d["tag"] == text(tid.get_tag())
    if cls == H5T.COMPOUND:
        return len(d["members"]) == tid.get_nmembers() and all(
            m["name"] == decode(tid.get_member_name(i))
            and (m["offset"] == tid.get_member_offset(i) or holds_addresses(d))  # h5py gives offsets in memory
            and type_ok(m["type"], tid.get_member_type(i)) for i, m in enumerate(d["members"]))
    if cls == H5T.ENUM:
        values = {decode(tid.get_member_name(i)): tid.get_member_value(i) for i in range(tid.get_nmembers())}
        return type_ok(d["base"], tid.get_super()) and {k: int(v) for k, v in d["members"].items()} == values
    if cls == H5T.ARRAY:
        return list(tid.get_array_dims()) == d["shape"] and type_ok(d["base"], tid.get_super())
    if cls == H5T.VLEN:
        return type_ok(d["base"], tid.get_super())
    return True


def holds_addresses(d: dict) -> bool:
    if d["class"] in ("reference", "variable-length"):
        return True
    if d["class"] == "compound":
        return any(holds_addresses(m["type"]) for m in d["members"])
    return d["class"] == "array" and holds_addresses(d["base"])


def numeric(d: dict) -> bool:
    return (d["class"] == "integer" and "offset" not in d and d["size"] in (1, 2, 4, 8)
            or d["class"] == "float" and "layout" not in d and d["size"] in (2, 4, 8)) and d["order"] != "vax"


def supported_column(d: dict) -> bool:
    """Whether §5.4 maps a dataset of datatype `d`, which holds addresses."""
    def column(t: dict) -> bool:
        return (t["class"] == "variable-length" and not holds_addresses(t["base"])
                or t["class"] == "reference" and (t["kind"], t["size"]) in ((0, 8), (1, 12)))
    if d["class"] == "compound":
        return all(column(m["type"]) or not holds_addresses(m["type"]) for m in d["members"])
    return column(d)


def same_selection(sel: dict, space) -> bool:
    """A selection's description (conventions/ims/README.md §5.3) against an h5py dataspace's."""
    kind = space.get_select_type()
    if sel["select"] == "all":
        return kind == h5py.h5s.SEL_ALL
    if sel["select"] == "none":
        return kind == h5py.h5s.SEL_NONE
    if sel["select"] == "points":
        return kind == h5py.h5s.SEL_POINTS and space.get_select_elem_pointlist().tolist() == sel["points"]
    if kind != h5py.h5s.SEL_HYPERSLABS:
        return False
    want = space.get_select_hyper_blocklist().tolist()  # [start, end] of each block
    if "blocks" in sel:
        return want == sel["blocks"]
    blocks = []
    for index in np.ndindex(*sel["count"]):
        start = [s + i * t for s, i, t in zip(sel["start"], index, sel["stride"])]
        blocks.append([start, [a + b - 1 for a, b in zip(start, sel["block"])]])
    return sorted(want) == sorted(blocks)


def address_of(obj) -> int:
    return h5py.h5o.get_info(obj).addr


def number_bytes(d: dict, values: list) -> bytes:
    """Numbers of the description `d` as the file holds them (NaN by its canonical bits)."""
    order = ">" if d["order"] == "big" else "<"
    if d["class"] == "integer":
        return b"".join(int(v).to_bytes(d["size"], "big" if order == ">" else "little", signed=d["signed"])
                        for v in values)
    fmt = {2: "e", 4: "f", 8: "d"}[d["size"]]
    return b"".join(struct.pack(order + fmt, float(v.replace("Infinity", "inf")) if isinstance(v, str) else v)
                    for v in values)


def same_numbers(d: dict, got: bytes, want: bytes) -> bool:
    if d["class"] == "integer":
        return got == want
    dt = np.dtype(("<" if d["order"] == "little" else ">") + {2: "f2", 4: "f4", 8: "f8"}[d["size"]])
    return np.array_equal(np.frombuffer(got, dt), np.frombuffer(want, dt), equal_nan=True)


def aid_value(aid):
    """An attribute's value as h5py reads it, by its identifier."""
    arr = np.empty(aid.shape, dtype=aid.dtype)
    aid.read(arr)
    return arr


class Checker:
    """Rebuilds every HDF5 object from the source metadata node (conventions/ims/README.md §5)
    and compares it with h5py."""

    def __init__(self, f, source) -> None:
        self.f, self.source = f, source
        self.paths: dict[int, str] = {}  # object header address -> the path where the walk met it
        self.problems: list[str] = []
        self.pending: list = []  # checks of references and named datatypes, after the walk
        # The object table (§5.7): each object's path, from its parent's and its key.
        parents, names = np.asarray(source["objects/parent"][...]), self.family(source["objects/name"])
        self.table: list[str] = []
        for parent, name in zip(parents, names):
            self.table.append("/" if parent < 0 else self.table[parent].rstrip("/") + "/" + name.decode())

    @staticmethod
    def family(node) -> list[bytes]:
        """A family of byte values (conventions §7): a 2-D array, or a group of offsets and data."""
        if isinstance(node, zarr.Group):
            starts = node["offsets"][...]
            data = bytes(node["data"][...]) if "data" in node else b""
            return [data[a:b] for a, b in zip(starts[:-1], starts[1:])]
        return [bytes(r) for r in node[...]]

    def load(self, node) -> dict:
        """A document's source metadata, with its spilled entries merged in (§5.6)."""
        s = dict(dict(node.attrs).get("vzip_virtualized", {}).get("ims", {}))
        if "spilled" in s:
            for text in self.family(self.source[s.pop("spilled")]):
                for k, v in json.loads(text).items():
                    if k in ("attributes", "names", "links", "images", "datatypes", "unsupported") and v is not None:
                        s[k] = {**s.get(k, {}), **v}
                    elif k in ("attribute_collisions", "collisions"):
                        s[k] = [*s.get(k, []), *v]
                    else:
                        s[k] = v
        return s

    def described(self, entry: dict) -> dict:
        """An entry's datatype description: its own, or its named datatype's (§5.2)."""
        if "datatype" in entry or not isinstance(entry.get("named"), int):
            return entry.get("datatype", {"class": None})
        parent, _, key = self.table[entry["named"]].rpartition("/")
        s = self.load(self.source["hdf5" + parent])
        return s.get("datatypes", {}).get(key, {}).get("datatype", {"class": None})

    def ref_path(self, index) -> str | None:
        return None if index is None or index < 0 else self.table[index]

    def path_ok(self, path, at: int | None) -> bool:
        """Whether a path of the source metadata leads to the object at `at` (None: to nothing)."""
        return (path is None) if at is None else self.paths.get(at) == path

    def array_bytes(self, value: dict, d: dict) -> bytes | list:
        """An attribute array's bytes, a family's members, or the paths of references."""
        node = self.source[value["array"]]
        if isinstance(node, zarr.Group) or node.metadata.dimension_names == ("index", "byte"):
            return self.family(node)
        if d["class"] == "reference":
            return [self.ref_path(int(i)) for i in np.asarray(node[...]).reshape(-1)]
        arr = node[...]
        if arr.dtype.kind in "iuf" and arr.dtype.itemsize > 1:
            arr = arr.astype(arr.dtype.newbyteorder(">" if d["order"] == "big" else "<"))
        return np.ascontiguousarray(arr).tobytes()

    # ---- attributes

    def attributes(self, where: str, obj, entry: dict) -> None:
        """h5py's attributes of `obj` against the members `attributes` and
        `attribute_collisions` of `entry` (§5.3)."""
        got, collided = entry.get("attributes", {}), list(entry.get("attribute_collisions", []))
        if got is None:
            self.problems.append(f"{where}: attributes not read")
            return
        try:
            names = sorted(h5py.h5a.open(obj.id, index=i).name for i in range(h5py.h5a.get_num_attrs(obj.id)))
        except OSError as e:
            if "invalid datatype size" in str(e):
                return  # a string of no bytes, which h5py cannot open (tests/test_virtualize_ims.py checks it)
            raise
        keys: set[str] = set()
        for name in names:
            key = decode(name)
            if key in keys:
                value = collided.pop(0) if collided else None
                if value is None or text_bytes(value.get("name", "")) != name:
                    self.problems.append(f"{where}: collision {name!r} not listed")
                    continue
            else:
                keys.add(key)
                value = got.get(key)
                if value is None:
                    self.problems.append(f"{where}: attribute {key!r} missing")
                    continue
                if isinstance(value, dict) and "name" in value and text_bytes(value["name"]) != name:
                    self.problems.append(f"{where}: attribute {key!r} has the name {value['name']}")
            self.attribute(f"{where}@{decode(name)}", obj, name, value)
        if set(got) != keys or collided:
            self.problems.append(f"{where}: attribute names {sorted(got)} != {sorted(keys)}")

    def attribute(self, where: str, obj, name: bytes, value) -> None:
        aid = h5py.h5a.open(obj.id, name)
        tid = aid.get_type()
        if not isinstance(value, dict) or ("datatype" not in value and "named" not in value and "reason" not in value):
            value = {"datatype": IMARIS_TEXT, "value": value}
        if "datatype" not in value and "named" not in value:
            self.problems.append(f"{where}: unread: {value.get('reason')}")
            return
        d = self.described(value)
        if not type_ok(d, tid):
            self.problems.append(f"{where}: datatype {d} differs")
            return
        self.named(where, value, tid)
        if aid.get_space().get_simple_extent_type() == h5py.h5s.NULL:
            if value.get("shape", 0) is not None:
                self.problems.append(f"{where}: a null dataspace not kept")
            return
        dims = list(aid.shape)
        if value.get("shape", dims if len(dims) == 1 else None) != dims and not (
                "value" in value and not dims and "shape" not in value):
            self.problems.append(f"{where}: shape {value.get('shape')} != {dims}")
            return
        if "reason" in value:
            self.problems.append(f"{where}: unread: {value['reason']}")
            return
        count = int(np.prod(dims)) if dims else 1
        values = value.get("value")
        if "json" in value:
            values = json.loads(bytes(self.source[value["json"]][...]))
        if ("value" in value or "json" in value) and not dims:
            values = [values]
        if (d["class"] == "reference" and d["kind"] == 0) or (
                d["class"] == "variable-length" and not holds_addresses(d["base"])):
            flat = np.asarray(aid_value(aid), dtype=object).reshape(-1)
            # Object references are indexes into the object table, as arrays and as JSON.
            got = (self.array_bytes(value, d) if "array" in value
                   else [self.ref_path(v) for v in values] if d["class"] == "reference" else values)
            self.addressed(where, d, flat, got)
            return
        if holds_addresses(d):
            flat = np.asarray(aid_value(aid)).reshape(-1)
            if len(values) != len(flat):
                self.problems.append(f"{where}: {len(values)} values, not {len(flat)}")
            for v, want in zip(values, flat):
                self.structured(where, d, v, want, aid.get_type())
            return
        buf = np.empty(dims, dtype=np.dtype(f"V{d['size']}"))
        aid.read(buf, mtype=tid)
        raw = buf.tobytes()
        if "data" in value:
            ok = base64.b64decode(value["data"]) == raw
        elif "array" in value:
            ok = self.array_bytes(value, d) == raw
        elif d["class"] == "string":
            if isinstance(values, (str, dict)) or (values and not dims and len(dims) == 1):
                want = text_bytes(values)
            else:
                pad = b" " if d["padding"] == "space-padded" else b"\0"
                want = b"".join(text_bytes(v).ljust(d["size"], pad) for v in values)
            ok = want == raw
        else:
            ok = len(values) == count and same_numbers(d, raw, number_bytes(d, values))
        if not ok:
            self.problems.append(f"{where}: value {str(value)[:80]} differs")

    def addressed(self, where: str, d: dict, flat, got) -> None:
        """References and variable-length values against h5py's."""
        if len(got) != len(flat):
            self.problems.append(f"{where}: {len(got)} values, not {len(flat)}")
            return
        for v, want in zip(got, flat):
            if d["class"] == "reference":
                at = address_of(self.f[want].id) if want else None
                self.pending.append((where, v, at))
            elif d.get("string"):
                w = want.encode() if isinstance(want, str) else want
                if (v if isinstance(v, bytes) else text_bytes(v)) != w:
                    self.problems.append(f"{where}: text {v!r} != {w!r}")
            else:
                w = np.asarray(want)
                g = (np.frombuffer(v, w.dtype.newbyteorder("<" if d["base"].get("order") != "big" else ">"))
                     if isinstance(v, bytes) else
                     np.frombuffer(base64.b64decode(v), w.dtype) if isinstance(v, str) else np.asarray(v))
                if not np.array_equal(g.astype(w.dtype) if g.dtype != w.dtype else g, w, equal_nan=True):
                    self.problems.append(f"{where}: sequence {v} != {w}")

    def structured(self, where: str, d: dict, v, want, tid) -> None:
        """A value of a datatype that holds addresses (§5.3) against h5py's."""
        cls = d["class"]
        if not holds_addresses(d):
            raw = np.asarray(want).tobytes() if not isinstance(want, bytes) else want
            if numeric(d) and (not isinstance(v, str) or v in ("NaN", "Infinity", "-Infinity")):
                x = float(v.replace("Infinity", "inf")) if isinstance(v, str) else v
                w = np.asarray([want])
                ok = np.array_equal(np.asarray([x]).astype(w.dtype), w, equal_nan=True)
            else:
                ok = base64.b64decode(v) == raw if isinstance(v, str) else False
            if not ok:
                self.problems.append(f"{where}: {v} != {want!r}")
            return
        if cls == "reference" and d["kind"] == 0:
            self.pending.append((where, self.ref_path(v), address_of(self.f[want].id) if want else None))
        elif cls == "reference":
            if not want:
                if v is not None:
                    self.problems.append(f"{where}: region {v} is not null")
                return
            target = self.f[want]
            self.pending.append((where, self.ref_path(v["object"]), address_of(target.id)))
            if not same_selection(v["selection"], h5py.h5r.get_region(want, target.id)):
                self.problems.append(f"{where}: selection {v['selection']} differs")
        elif cls == "variable-length":
            if d.get("string"):
                w = want.encode() if isinstance(want, str) else want
                if text_bytes(v) != w:
                    self.problems.append(f"{where}: text {v!r} != {w!r}")
                return
            items = list(want)
            if not holds_addresses(d["base"]):
                self.addressed(where, d, [np.asarray(want)], [v])
                return
            if len(v) != len(items):
                self.problems.append(f"{where}: {len(v)} elements, not {len(items)}")
            for x, w in zip(v, items):
                self.structured(where, d["base"], x, w, tid.get_super())
        elif cls == "compound":
            for i, m in enumerate(d["members"]):
                self.structured(f"{where}.{m['name']}", m["type"], v[m["name"]], want[i], tid.get_member_type(i))
        else:
            flat = np.asarray(want).reshape(-1)
            for x, w in zip(v, flat):
                self.structured(where, d["base"], x, w, tid.get_super())

    def named(self, where: str, entry: dict, tid) -> None:
        if tid.committed():
            named = entry.get("named", "?")
            self.pending.append((where, self.ref_path(named) if isinstance(named, int) else named, address_of(tid)))
        elif "named" in entry:
            self.problems.append(f"{where}: named {entry['named']} but not committed")

    # ---- datasets

    def dataset(self, where: str, ds, node) -> None:
        """h5py's dataset `ds` against its array, or its group (§5.4)."""
        s = self.load(node)
        d = self.described(s)
        if not type_ok(d, ds.id.get_type()):
            self.problems.append(f"{where}: datatype {d} differs")
            return
        self.named(where, s, ds.id.get_type())
        self.attributes(where, ds, s)
        if ds.shape is None:
            if not isinstance(node, zarr.Group) or s.get("shape", 0) is not None:
                self.problems.append(f"{where}: a null dataspace not kept")
            return
        want = ds[()]
        if d["class"] == "compound" and holds_addresses(d):
            data = np.asarray(node["data"][...])
            if data.shape != (*ds.shape, d["size"]):
                self.problems.append(f"{where}: data {data.shape} differs")
                return
            data = data.reshape(-1, d["size"])
            flat = np.asarray(want).reshape(-1)
            for i, m in enumerate(d["members"]):
                mt, o = m["type"], m["offset"]
                column = flat[ds.dtype.names[i]]
                if holds_addresses(mt):
                    if data[:, o : o + mt["size"]].any():
                        self.problems.append(f"{where}.{m['name']}: addresses not zeroed")
                    self.column(f"{where}.{m['name']}", mt, node[str(i)], column, list(ds.shape))
                elif data[:, o : o + mt["size"]].tobytes() != np.ascontiguousarray(column).tobytes():
                    self.problems.append(f"{where}.{m['name']}: bytes differ")
            return
        if d["class"] in ("variable-length", "reference"):
            self.column(where, d, node, np.asarray(want, dtype=object).reshape(-1), list(ds.shape), s)
            return
        got = node[...]
        fill = np.zeros((1,), dtype=ds.dtype)
        ds.id.get_create_plist().get_fill_value(fill)
        raw = fill.tobytes()
        want = np.asarray(want)
        if "fill" in s:
            # A reader fills the chunks that are absent with the fill value's bytes.
            got = got.copy()
            grid = [-(-n // c) for n, c in zip(node.shape, node.chunks)]
            for index in np.ndindex(*grid):
                key = f"{node.path}/c/" + "/".join(map(str, index))
                if not sync(node.store.exists(key)):
                    sl = tuple(slice(i * c, (i + 1) * c) for i, c in zip(index, node.chunks))
                    region = got[sl]
                    endian = node.metadata.codecs[0].to_dict().get("configuration", {}).get("endian")
                    pattern = np.frombuffer(base64.b64decode(s["fill"]), dtype=got.dtype.newbyteorder(
                        ">" if endian == "big" else "<")).astype(got.dtype)
                    region[...] = np.resize(pattern, region.size).reshape(region.shape) if region.size else region
        if got.shape == want.shape:
            want = want.astype(want.dtype.newbyteorder("="))
            ok = np.array_equal(got, want, equal_nan=True)
        else:
            ok = bytes(np.ascontiguousarray(got)) == want.tobytes() and got.shape == (*want.shape, ds.dtype.itemsize)
        if not ok:
            self.problems.append(f"{where}: data {got.shape} differs")
        if "fill" in s:
            if base64.b64decode(s["fill"]) != raw or node.metadata.fill_value != 0:
                self.problems.append(f"{where}: fill {s['fill']} differs")
        else:
            item = np.dtype(node.metadata.data_type.to_native_dtype()).itemsize
            element = np.frombuffer(raw[:item], dtype=got.dtype.newbyteorder(
                ">" if node.metadata.codecs[0].to_dict().get("configuration", {}).get("endian") == "big" else "<"))
            if not np.array_equal(element, np.asarray([node.metadata.fill_value], dtype=got.dtype), equal_nan=True):
                self.problems.append(f"{where}: fill value {node.metadata.fill_value} != {element}")

    def column(self, where: str, d: dict, node, flat, shape: list[int], s: dict | None = None) -> None:
        """Elements that hold addresses (§5.4) against h5py's."""
        flat = np.asarray(flat, dtype=object).reshape(-1)
        if d["class"] == "reference" and d["kind"] == 0:
            if list(node.shape) != shape:
                self.problems.append(f"{where}: shape {node.shape} != {shape}")
            self.addressed(where, d, flat, [self.ref_path(int(i)) for i in np.asarray(node[...]).reshape(-1)])
            return
        if s is not None and s.get("shape") != shape:
            self.problems.append(f"{where}: shape {s.get('shape')} != {shape}")
        rows = self.family(node)
        if d["class"] == "reference":
            values = [json.loads(r) for r in rows]
            if len(values) != len(flat):
                self.problems.append(f"{where}: {len(values)} regions, not {len(flat)}")
            for v, w in zip(values, flat):
                self.structured(where, d, v, w, None)
            return
        self.addressed(where, d, flat, rows)

    def unsupported(self, where: str, obj, entry: dict) -> None:
        """An unsupported object's record against h5py: what it keeps (§5.1)."""
        if "reason" not in entry:
            self.problems.append(f"{where}: no reason")
        if isinstance(obj, (h5py.Dataset, h5py.Datatype)):
            tid = obj.id.get_type() if isinstance(obj, h5py.Dataset) else obj.id
            if ("datatype" in entry or "named" in entry) and not type_ok(self.described(entry), tid):
                self.problems.append(f"{where}: datatype {entry['datatype']} differs")
        if isinstance(obj, h5py.Dataset) and entry.get("shape", "absent") not in ("absent", list(obj.shape or [])) \
                and not (obj.shape is None and entry["shape"] is None):
            self.problems.append(f"{where}: shape {entry['shape']} != {obj.shape}")
        if "attributes" in entry or not h5py.h5a.get_num_attrs(obj.id):
            self.attributes(where, obj, entry)
        else:
            self.problems.append(f"{where}: attributes not kept")

    # ---- groups

    def tree(self) -> list[str]:
        f, source = self.f, self.source
        self.paths[address_of(f.id)] = "/"
        stack = [("/", f, "hdf5")]
        while stack:
            p, g, zpath = stack.pop()
            node = source[zpath]
            s = self.load(node)
            self.attributes(p, g, s)
            links, images, unsupported = s.get("links", {}), s.get("images", {}), s.get("unsupported", {})
            datatypes, spelled, collisions = s.get("datatypes", {}), s.get("names", {}), list(s.get("collisions", []))
            children = []
            keys: set[str] = set()
            for name in sorted(g.id):
                key = decode(name)
                if key in keys:
                    if not collisions or text_bytes(collisions.pop(0)) != name:
                        self.problems.append(f"{p}: collision {name!r} not listed")
                    continue
                keys.add(key)
                if (key in spelled) != (decode(name) != name.decode("utf-8", "replace")) or (
                        key in spelled and text_bytes(spelled[key]) != name):
                    self.problems.append(f"{p}: name {name!r} not spelled")
                child = (p if p != "/" else "") + "/" + key
                info = g.id.links.get_info(name)
                if info.type == h5py.h5l.TYPE_SOFT:
                    if links.get(key) != {"soft": text(g.id.links.get_val(name))}:
                        self.problems.append(f"{child}: soft link {links.get(key)}")
                    continue
                if info.type == h5py.h5l.TYPE_EXTERNAL:
                    file, path = g.id.links.get_val(name)
                    if links.get(key) != {"external": {"file": text(file), "path": text(path)}}:
                        self.problems.append(f"{child}: external link {links.get(key)}")
                    continue
                at = address_of(h5py.h5o.open(g.id, name))
                if at in self.paths:
                    hard = links.get(key, {}).get("hard")
                    if not isinstance(hard, int) or self.ref_path(hard) != self.paths[at]:
                        self.problems.append(f"{child}: hard link {links.get(key)} != {self.paths[at]}")
                    continue
                self.paths[at] = child
                oid = h5py.h5o.open(g.id, name)
                obj = {h5py.h5i.GROUP: h5py.Group, h5py.h5i.DATASET: h5py.Dataset,
                       h5py.h5i.DATATYPE: h5py.Datatype}[h5py.h5i.get_type(oid)](oid)
                if key in unsupported:
                    self.unsupported(child, obj, unsupported[key])
                    # What §5.4 does not map: other filters, external or virtual data,
                    # addresses inside other datatypes than its columns, and what is
                    # over its limits.
                    if not (key in ("zarr.json",) or key.startswith("__")
                            or unsupported[key]["reason"] in LIMITS
                            or isinstance(obj, h5py.Dataset) and (
                                obj.external or obj.is_virtual or obj.compression in ("lzf",) or obj.scaleoffset is not None
                                or holds_addresses(self.described(unsupported[key]))
                                and not supported_column(self.described(unsupported[key])))):
                        self.problems.append(f"{child}: unsupported: {unsupported[key]['reason']}")
                elif isinstance(obj, h5py.Datatype):
                    entry = datatypes.get(key)
                    if entry is None or not type_ok(entry["datatype"], obj.id):
                        self.problems.append(f"{child}: named datatype {entry}")
                    else:
                        self.attributes(child, obj, entry)
                elif isinstance(obj, h5py.Group):
                    children.append((child, obj, f"{zpath}/{key}"))
                elif key in images:
                    if images[key].get("shape") != list(obj.shape):
                        self.problems.append(f"{child}: image shape {images[key].get('shape')} != {obj.shape}")
                    self.attributes(child, obj, images[key])
                else:
                    try:
                        arr = source[f"{zpath}/{key}"]
                    except KeyError:
                        self.problems.append(f"{child}: no array")
                        continue
                    self.dataset(child, obj, arr)
            if collisions:
                self.problems.append(f"{p}: collisions {collisions} not in the file")
            stack.extend(reversed(children))
        for where, path, at in self.pending:
            if not self.path_ok(path, at):
                self.problems.append(f"{where}: path {path} does not lead to the object {at}")
        return self.problems


def tree_problems(root, path: Path) -> list[str]:
    """Rebuilds every HDF5 object from the source metadata node (conventions/ims/README.md §5)
    and compares it with h5py."""
    with h5py.File(path, "r") as f:
        return Checker(f, root["vzip_source"]).tree()


def check(path: Path, out: Path) -> list[str]:
    """The differences between the archive at 'out' and h5py's reading of 'path'."""
    problems = []
    root = zarr.open_group(VZipStore(str(out), policy=LOOPBACK_SOURCES), mode="r", zarr_format=3)
    levels = root.attrs["ome"]["multiscales"][0]["datasets"]
    problems += tree_problems(root, path)
    with h5py.File(path, "r") as f:
        for r in range(len(levels)):
            arr = root[str(r)]
            axes = list(arr.metadata.dimension_names)
            sizes = dict(zip(axes, arr.shape))
            for t in range(sizes.get("t", 1)):
                for c in range(sizes.get("c", 1)):
                    group = f[f"DataSet/ResolutionLevel {r}/TimePoint {t}/Channel {c}"]
                    z, y, x = image_size(group)
                    want = group["Data"][:z, :y, :x]
                    want = want.astype(want.dtype.newbyteorder("="))  # zarr reads in native byte order
                    if "z" not in axes:
                        want = want[0]
                    index = tuple(t if a == "t" else c if a == "c" else slice(None) for a in axes)
                    got = arr[index]
                    if got.dtype != want.dtype or not np.array_equal(got, want):
                        problems.append(f"level {r} t {t} c {c}: {got.dtype} {got.shape} != {want.dtype} {want.shape}")
    return problems


def main(paths: list[str]) -> int:
    files = [Path(p) for p in paths] or sorted((HERE.parent / "fixtures" / "ims").rglob("ims_*.ims"))
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        servers: dict[Path, Server] = {}
        for path in files:
            server = servers.setdefault(path.parent, Server(path.parent))
            out = Path(tmp) / f"{path.stem}.vzip"
            # VERIFY_PY=1 runs the reference implementation instead of the browser code.
            command = ([sys.executable, "-m", "vzip.virtualize", "--allow-private-hosts"] if os.environ.get("VERIFY_PY")
                       else ["node", str(ROOT / "web" / "conformance" / "virtualize.ts"), "--allow-private-hosts"])
            p = subprocess.run([*command, server.base + path.name, str(out)], capture_output=True, text=True)
            if path.stem.startswith("ims_reject"):
                ok = p.returncode == 3 and not out.exists()
                failures += not ok
                print(f"{path.name:36s} {'rejected: ' + p.stderr.strip()[-90:] if ok else 'NOT REJECTED ' + p.stderr[-200:]}")
                continue
            if p.returncode:
                failures += 1
                print(f"{path.name:36s} FAILED: {p.stderr.strip()[-300:]}")
                continue
            problems = check(path, out)
            failures += bool(problems)
            print(f"{path.name:36s} {problems[:3] if problems else 'ok'} {p.stdout.strip()[:160]}")
    print(f"\n{failures} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

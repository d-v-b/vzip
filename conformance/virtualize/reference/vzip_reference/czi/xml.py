"""The values the CZI layout reads from the metadata XML (spec/virtualize/czi.md §2.7)."""

from __future__ import annotations

import bisect
import math
import re
from dataclasses import dataclass, field

from vzip_reference.tiff.virtualize import DECIMAL, _decode, scan

MAX_XML = 1 << 26  # the XML is read for the layout only when at most this many bytes
INTEGER = re.compile(r"[0-9]+")
COLOR = re.compile(r"#(?:[0-9A-Fa-f]{2})?([0-9A-Fa-f]{6})")


@dataclass
class Channel:
    name: str | None = None
    color: str | None = None
    bits: int | None = None
    low: float | None = None
    high: float | None = None


@dataclass
class XmlValues:
    px: float | None = None  # metres
    py: float | None = None
    pz: float | None = None
    inc: float | None = None  # seconds
    bits: int | None = None
    info: list[Channel] = field(default_factory=list)  # Information/Image/Dimensions/Channels/Channel[k]
    display: list[Channel] = field(default_factory=list)  # DisplaySetting/Channels/Channel[k]
    scenes: dict[int, str | None] = field(default_factory=dict)  # S Index -> Name of its first Scene


class Tree:
    """The scan's tags as nested elements (spec/virtualize/czi.md §2.7)."""

    def __init__(self, xml: str) -> None:
        self.xml = xml
        self.tags, self.skipped = scan(xml)
        self.skipped_ends = [b for _, b in self.skipped]
        self.children: list[list[int]] = []  # element -> its child elements
        self.tag: list[int] = []  # element -> its start tag's index
        self.root = None
        stack: list[int] = []
        for ti, (_, _, closing, name, _, self_closing) in enumerate(self.tags):
            if closing:
                for k in range(len(stack) - 1, -1, -1):
                    if self.tags[self.tag[stack[k]]][3] == name:
                        del stack[k:]
                        break
                continue
            e = len(self.tag)
            self.tag.append(ti)
            self.children.append([])
            if stack:
                self.children[stack[-1]].append(e)
            elif self.root is None:
                self.root = e
            if not self_closing:
                stack.append(e)

    def name(self, e: int) -> str:
        return self.tags[self.tag[e]][3]

    def attrs(self, e: int) -> dict:
        return self.tags[self.tag[e]][4]

    def child(self, e: int | None, name: str, k: int = 0) -> int | None:
        if e is None:
            return None
        for c in self.children[e]:
            if self.name(c) == name:
                if k == 0:
                    return c
                k -= 1
        return None

    def path(self, p: str) -> int | None:
        e = self.root if self.root is not None and self.name(self.root) == "ImageDocument" else None
        for part in p.split("/"):
            m = re.fullmatch(r"(.+)\[([0-9]+)\]", part)
            e = self.child(e, m[1], int(m[2])) if m else self.child(e, part)
        return e

    def text(self, e: int | None) -> str | None:
        if e is None:
            return None
        ti = self.tag[e]
        start, end, _, _, _, self_closing = self.tags[ti]
        if self_closing:
            return ""
        stop = self.tags[ti + 1][0] if ti + 1 < len(self.tags) else len(self.xml)
        pieces, pos = [], end
        for k in range(bisect.bisect_right(self.skipped_ends, end), len(self.skipped)):
            a, b = self.skipped[k]
            if a >= stop:
                break
            pieces.append(self.xml[pos:a])
            pos = b
        pieces.append(self.xml[pos:stop])
        return _decode("".join(pieces)).strip(" \t\r\n")


def decimal(t: str | None) -> float | None:
    if t is None or not DECIMAL.fullmatch(t):
        return None
    v = float(t)
    return v if math.isfinite(v) else None


def integer(t: str | None) -> int | None:
    if t is None or not INTEGER.fullmatch(t) or len(t.lstrip("0")) > 16:
        return None
    v = int(t)
    return v if v <= 2**53 - 1 else None


def color(t: str | None) -> str | None:
    m = COLOR.fullmatch(t) if t is not None else None
    return m[1].upper() if m else None


def _positive(v: float | None) -> float | None:
    return v if v is not None and v > 0 else None


def read_xml_values(data: bytes) -> XmlValues:
    out = XmlValues()
    if len(data) > MAX_XML:
        return out
    if data[:3] == b"\xef\xbb\xbf":
        data = data[3:]
    try:
        xml = data.decode("utf-8")
    except UnicodeDecodeError:
        return out
    t = Tree(xml)
    if t.root is None or t.name(t.root) != "ImageDocument":
        return out
    items = t.path("Metadata/Scaling/Items")
    for axis in "XYZ":
        for c in t.children[items] if items is not None else []:
            if t.name(c) == "Distance" and t.attrs(c).get("Id") == axis:
                setattr(out, f"p{axis.lower()}", _positive(decimal(t.text(t.child(c, "Value")))))
                break
    out.inc = _positive(decimal(t.text(t.path("Metadata/Information/Image/Dimensions/T/Positions/Interval/Increment"))))
    out.bits = integer(t.text(t.path("Metadata/Information/Image/ComponentBitCount")))
    for parent, target, with_bits in (("Metadata/Information/Image/Dimensions/Channels", out.info, True),
                                      ("Metadata/DisplaySetting/Channels", out.display, False)):
        e = t.path(parent)
        for c in t.children[e] if e is not None else []:
            if t.name(c) != "Channel":
                continue
            ch = Channel(t.attrs(c).get("Name"), color(t.text(t.child(c, "Color"))))
            if with_bits:
                ch.bits = integer(t.text(t.child(c, "ComponentBitCount")))
            else:
                ch.low = decimal(t.text(t.child(c, "Low")))
                ch.high = decimal(t.text(t.child(c, "High")))
            target.append(ch)
    scenes = t.path("Metadata/Information/Image/Dimensions/S/Scenes")
    for c in t.children[scenes] if scenes is not None else []:
        if t.name(c) == "Scene":
            index = integer(t.attrs(c).get("Index"))
            name = t.attrs(c).get("Name")
            if index is not None and index not in out.scenes:
                out.scenes[index] = name
    return out

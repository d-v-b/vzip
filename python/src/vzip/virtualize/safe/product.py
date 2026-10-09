"""A SAFE product's objects, product metadata and tile metadata (spec/virtualize/safe.md §2)."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from vzip.virtualize.common import Rejected
from vzip.virtualize.store import Element, parse_xml

MAX_XML = 1 << 24
MAX_U32 = 0xFFFFFFFF
WS = " \t\r\n"
MARKER = "_$folder$"
LEVELS = {"MTD_MSIL1C.xml": ("L1C", "Level-1C_User_Product", "Level-1C_Tile_ID"),
          "MTD_MSIL2A.xml": ("L2A", "Level-2A_User_Product", "Level-2A_Tile_ID")}
GRANULE = "General_Info/Product_Info/Product_Organisation/Granule_List/Granule"
# The names of PSD 14.2 (Level-2A, processing baselines 02.04–02.06) that stand for later ones (§2.1).
ALIASES = {
    "Product_Info": "L2A_Product_Info",
    "Product_Organisation": "L2A_Product_Organisation",
    "IMAGE_FILE": "IMAGE_FILE_2A",
    "PRODUCT_URI": "PRODUCT_URI_2A",
    "Product_Image_Characteristics": "L2A_Product_Image_Characteristics",
    "QUANTIFICATION_VALUES_LIST": "L1C_L2A_Quantification_Values_List",
    "BOA_QUANTIFICATION_VALUE": "L2A_BOA_QUANTIFICATION_VALUE",
    "AOT_QUANTIFICATION_VALUE": "L2A_AOT_QUANTIFICATION_VALUE",
    "WVP_QUANTIFICATION_VALUE": "L2A_WVP_QUANTIFICATION_VALUE",
    "Scene_Classification_List": "L2A_Scene_Classification_List",
    "Scene_Classification_ID": "L2A_Scene_Classification_ID",
    "SCENE_CLASSIFICATION_TEXT": "L2A_SCENE_CLASSIFICATION_TEXT",
    "SCENE_CLASSIFICATION_INDEX": "L2A_SCENE_CLASSIFICATION_INDEX",
    "TILE_ID": "TILE_ID_2A",
}
DECIMAL = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")
POSITIVE = re.compile(r"[1-9][0-9]*\Z")
BAND_NAME = re.compile(r"[A-Za-z0-9]+\Z")


# ---------------------------------------------------------------- XML elements (§2.1)


def local(name: str) -> str:
    """An element's local name: its name after the last `:`."""
    return name.rpartition(":")[2]


def kids(el: Element, name: str) -> list[Element]:
    """The children of `el` whose local name is `name`, or its PSD 14.2 alias."""
    alias = ALIASES.get(name, name)
    return [c for c in el.children if local(c.name) in (name, alias)]


def path(el: Element, p: str) -> list[Element]:
    """The elements at the path `p` (local names separated by `/`) under `el`."""
    found = [el]
    for name in p.split("/"):
        found = [c for e in found for c in kids(e, name)]
    return found


def attr(el: Element, name: str) -> str | None:
    """The value of the attribute `name` (as written), the first if repeated."""
    for n, v in el.attributes:
        if n == name:
            return v
    return None


def text(el: Element) -> str | None:
    """An element's text, without leading and trailing whitespace; None when it has a child element."""
    if el.children:
        return None
    return "".join(el.text).strip(WS)


def one(els: list[Element], what: str) -> Element:
    if len(els) != 1:
        raise Rejected(f"{what}: {len(els)} elements where there must be one")
    return els[0]


def layout_text(el: Element, what: str) -> str:
    t = text(el)
    if t is None:
        raise Rejected(f"{what} has a child element")
    return t


def decimal(t: str) -> float | None:
    """A decimal number's binary64 value (§6.1), or None if `t` is not one."""
    if not DECIMAL.match(t):
        return None
    return float(t)


def parse_document(data: bytes, what: str) -> Element:
    """A product's metadata document: UTF-8, in the XML subset of spec/virtualize.md §1.5 (§2.1)."""
    if data.startswith(b"\xef\xbb\xbf"):
        raise Rejected(f"{what} starts with a byte order mark")
    try:
        s = data.decode("utf-8")
    except UnicodeDecodeError:
        raise Rejected(f"{what} is not UTF-8") from None
    return parse_xml(s, what)


# ---------------------------------------------------------------- objects (§2.1)


def folders(keys, directories=()) -> tuple[set[str], list[str]]:
    """(the folder markers among `keys`, the empty directories) (§2.1). A folder marker is
    a key `d_$folder$` with `d` not empty; `directories` are the keys, ending in `/`, of a
    zip file's directory entries or of a listing's empty `d/` objects. The directories they
    name are empty when no key (theirs and the markers' included) is under them."""
    markers = {k for k in keys if k.endswith(MARKER) and len(k) > len(MARKER)}
    named = {k[: -len(MARKER)] for k in markers} | {d[:-1] for d in directories if len(d) > 1}
    parents = set()
    for k in [*keys, *named]:
        i = k.find("/")
        while i > 0:
            parents.add(k[:i])
            i = k.find("/", i + 1)
    return markers, sorted(named - parents)


def is_xml(key: str) -> bool:
    return key == "manifest.safe" or key.endswith((".xml", ".xsd"))


# ---------------------------------------------------------------- the product (§2.2–§2.5)


@dataclass
class Image:
    text: str  # the image file F
    key: str  # its band file, F.jp2
    basename: str


@dataclass
class Geocoding:
    code: str
    sizes: dict[int, tuple[int, int]]  # r -> (NROWS, NCOLS)
    positions: dict[int, tuple[float, float, float, float]]  # r -> (ULX, ULY, XDIM, YDIM)
    element: Element


@dataclass
class Product:
    level: str
    metadata_key: str
    metadata: Element  # the product metadata's root
    granule: str
    images: list[Image]
    tile_key: str
    tile: Element  # the tile metadata's root
    geocoding: Geocoding
    extra: dict = field(default_factory=dict)


def _u32(t: str, what: str) -> int:
    if not POSITIVE.match(t) or len(t) > 10 or int(t) > MAX_U32:
        raise Rejected(f"{what} {t[:40]!r} is not a positive integer below 2^32")
    return int(t)


def product_level(sizes: dict[str, int]) -> str:
    """The product metadata's key (§2.2)."""
    found = [k for k in LEVELS if k in sizes]
    if len(found) != 1:
        raise Rejected("not a SAFE product of the compact format: the root has "
                       + ("both MTD_MSIL1C.xml and MTD_MSIL2A.xml" if found else
                          "no MTD_MSIL1C.xml or MTD_MSIL2A.xml (products before December 2016 are not supported)"))
    if "manifest.safe" not in sizes:
        raise Rejected("a SAFE product has no manifest.safe")
    return found[0]


def read_product(sizes: dict[str, int], whole) -> Product:
    """The product (§2.2–§2.4): `sizes` maps each object's key to its size, and
    `whole(key)` reads an object."""
    key = product_level(sizes)
    level, root_name, tile_name = LEVELS[key]
    if sizes[key] > MAX_XML:
        raise Rejected(f"{key} is larger than 2^24 bytes")
    root = parse_document(whole(key), key)
    if local(root.name) != root_name:
        raise Rejected(f"{key}'s root element is not {root_name}")
    files = [f for granule in path(root, GRANULE) for f in kids(granule, "IMAGE_FILE")]
    if not files:
        raise Rejected(f"{key}: no granule has an IMAGE_FILE")
    images, seen, g = [], set(), None
    for el in files:
        f = layout_text(el, f"{key}: an IMAGE_FILE")
        parts = f.split("/")
        if len(parts) < 4 or parts[0] != "GRANULE" or "" in parts:
            raise Rejected(f"{key}: IMAGE_FILE {f[:200]!r} is not GRANULE/<g>/<directory>/<file>")
        if g is None:
            g = parts[1]
        elif parts[1] != g:
            raise Rejected(f"{key}: the image files are in two granule directories")
        if f in seen:
            raise Rejected(f"{key}: IMAGE_FILE {f[:200]!r} is listed twice")
        seen.add(f)
        if parts[2] != "IMG_DATA":  # a DEM, cloud or snow image of PSD 14.2: not a band file (§2.3)
            continue
        if f + ".jp2" not in sizes:
            raise Rejected(f"the band file {f[:200]}.jp2 is not in the product")
        images.append(Image(f, f + ".jp2", parts[-1]))
    if not images:
        raise Rejected(f"{key}: no image file is in IMG_DATA")
    tile_key = f"GRANULE/{g}/MTD_TL.xml"
    if tile_key not in sizes:
        raise Rejected(f"the tile metadata {tile_key[:200]} is not in the product")
    if sizes[tile_key] > MAX_XML:
        raise Rejected("the tile metadata is larger than 2^24 bytes")
    tile = parse_document(whole(tile_key), "MTD_TL.xml")
    if local(tile.name) != tile_name:
        raise Rejected(f"the tile metadata's root element is not {tile_name}")
    return Product(level, key, root, g, images, tile_key, tile, geocoding(tile))


def geocoding(tile: Element) -> Geocoding:
    """The tile geocoding (§2.4)."""
    tg = one(path(tile, "Geometric_Info/Tile_Geocoding"), "MTD_TL.xml: Geometric_Info/Tile_Geocoding")
    code = layout_text(one(kids(tg, "HORIZONTAL_CS_CODE"), "Tile_Geocoding/HORIZONTAL_CS_CODE"),
                       "HORIZONTAL_CS_CODE")
    if not re.fullmatch(r"EPSG:[0-9]+", code):
        raise Rejected(f"HORIZONTAL_CS_CODE {code[:40]!r} is not EPSG: and digits")
    sizes: dict[int, tuple[int, int]] = {}
    texts: dict[int, str] = {}
    size_els = kids(tg, "Size")
    if not size_els:
        raise Rejected("Tile_Geocoding has no Size")
    for el in size_els:
        rt = attr(el, "resolution")
        if rt is None:
            raise Rejected("a Size has no resolution")
        r = _u32(rt, "a Size's resolution")
        if r in sizes:
            raise Rejected(f"two Sizes have the resolution {r}")
        rows = _u32(layout_text(one(kids(el, "NROWS"), "Size/NROWS"), "NROWS"), "NROWS")
        cols = _u32(layout_text(one(kids(el, "NCOLS"), "Size/NCOLS"), "NCOLS"), "NCOLS")
        sizes[r], texts[r] = (rows, cols), rt
    positions = {}
    geos = kids(tg, "Geoposition")
    for r, rt in texts.items():
        el = one([g for g in geos if attr(g, "resolution") == rt], f"Geoposition of resolution {rt}")
        vals = []
        for name in ("ULX", "ULY", "XDIM", "YDIM"):
            t = layout_text(one(kids(el, name), f"Geoposition/{name}"), name)
            v = decimal(t)
            if v is None or not math.isfinite(v):
                raise Rejected(f"Geoposition {name} {t[:40]!r} is not a finite decimal number")
            vals.append(v)
        if vals[2] == 0 or vals[3] == 0:
            raise Rejected("a Geoposition's XDIM or YDIM is 0")
        positions[r] = tuple(vals)
    return Geocoding(code, sizes, positions, tg)


def band_name(basename: str, r: int) -> str:
    """A band file's band name (§2.5)."""
    b = basename
    suffix = f"_{r}m"
    if b.endswith(suffix):
        b = b[: -len(suffix)]
    name = b.rpartition("_")[2]
    if not BAND_NAME.match(name) or not name.isascii():
        raise Rejected(f"the band name {name[:40]!r} of {basename[:100]!r} is not ASCII letters and digits")
    return name


def resolution(geo: Geocoding, width: int, height: int, key: str) -> int:
    """The band file's resolution: the one Size of its image size (§2.5)."""
    found = [r for r, (rows, cols) in geo.sizes.items() if rows == height and cols == width]
    if len(found) != 1:
        raise Rejected(f"the band file {key[:200]} ({width} × {height}) matches "
                       f"{'no' if not found else 'several'} resolution of the tile geocoding")
    return found[0]

"""The GeoZarr attributes and the source metadata of a SAFE product
(conventions/safe/README.md §5, §6)."""

from __future__ import annotations

import json
import re

from vzip.virtualize.common import json_number
from vzip.virtualize.safe.product import DECIMAL, Element, Geocoding, Product, attr, kids, path, text

MAX_SAFE = 2**53 - 1

# The zarr-conventions CMOs (§5), at tag v0.1.
PROJ = {"uuid": "f17cb550-5864-4468-aeb7-f3180cfb622f",
        "schema_url": "https://raw.githubusercontent.com/zarr-conventions/proj/refs/tags/v0.1/schema.json",
        "spec_url": "https://github.com/zarr-conventions/proj/blob/v0.1/README.md",
        "name": "proj", "description": "Coordinate reference system information for geospatial data"}
SPATIAL = {"uuid": "689b58e2-cf7b-45e0-9fff-9cfc0883d6b4",
           "schema_url": "https://raw.githubusercontent.com/zarr-conventions/spatial/refs/tags/v0.1/schema.json",
           "spec_url": "https://github.com/zarr-conventions/spatial/blob/v0.1/README.md",
           "name": "spatial", "description": "Spatial coordinate information"}
MULTISCALES = {"uuid": "d35379db-88df-4056-af3a-620245f8e347",
               "schema_url": "https://raw.githubusercontent.com/zarr-conventions/multiscales/refs/tags/v0.1/schema.json",
               "spec_url": "https://github.com/zarr-conventions/multiscales/blob/v0.1/README.md",
               "name": "multiscales", "description": "Multiscale layout of zarr datasets"}

_DEG = 'ANGLEUNIT["degree",0.0174532925199433]'
_M = 'LENGTHUNIT["metre",1]'
WKT2 = (
    'PROJCRS["WGS 84 / UTM zone {z}{h}",BASEGEOGCRS["WGS 84",ENSEMBLE["World Geodetic System 1984 ensemble",'
    'MEMBER["World Geodetic System 1984 (Transit)"],MEMBER["World Geodetic System 1984 (G730)"],'
    'MEMBER["World Geodetic System 1984 (G873)"],MEMBER["World Geodetic System 1984 (G1150)"],'
    'MEMBER["World Geodetic System 1984 (G1674)"],MEMBER["World Geodetic System 1984 (G1762)"],'
    'MEMBER["World Geodetic System 1984 (G2139)"],MEMBER["World Geodetic System 1984 (G2296)"],'
    f'ELLIPSOID["WGS 84",6378137,298.257223563,{_M}],ENSEMBLEACCURACY[2.0]],'
    f'PRIMEM["Greenwich",0,{_DEG}],ID["EPSG",4326]],'
    'CONVERSION["UTM zone {z}{h}",METHOD["Transverse Mercator",ID["EPSG",9807]],'
    f'PARAMETER["Latitude of natural origin",0,{_DEG},ID["EPSG",8801]],'
    'PARAMETER["Longitude of natural origin",{lon},' f'{_DEG},ID["EPSG",8802]],'
    'PARAMETER["Scale factor at natural origin",0.9996,SCALEUNIT["unity",1],ID["EPSG",8805]],'
    f'PARAMETER["False easting",500000,{_M},ID["EPSG",8806]],'
    'PARAMETER["False northing",{fn},' f'{_M},ID["EPSG",8807]]],'
    f'CS[Cartesian,2],AXIS["(E)",east,ORDER[1],{_M}],'
    f'AXIS["(N)",north,ORDER[2],{_M}],' 'ID["EPSG",{c}]]'
)


def wkt2(code: str) -> str | None:
    """`proj:wkt2` for a WGS 84 / UTM zone's CRS code (§5.2), else None."""
    m = re.fullmatch(r"EPSG:([1-9][0-9]*)", code)
    if m is None or len(m.group(1)) != 5:
        return None
    c = int(m.group(1))
    if 32601 <= c <= 32660:
        z, h = c - 32600, "N"
    elif 32701 <= c <= 32760:
        z, h = c - 32700, "S"
    else:
        return None
    return WKT2.format(z=z, h=h, lon=6 * z - 183, fn=0 if h == "N" else 10000000, c=c)


# ---------------------------------------------------------------- the grids (§5.1)


def grid(geo: Geocoding, r: int) -> tuple[list[float], list[int], list[float]]:
    """(transform, shape, bounding box) of resolution r (§5.1)."""
    rows, cols = geo.sizes[r]
    ulx, uly, xdim, ydim = geo.positions[r]
    x1, y1 = ulx + xdim * cols, uly + ydim * rows
    return ([xdim, 0.0, ulx, 0.0, ydim, uly], [rows, cols],
            [min(ulx, x1), min(uly, y1), max(ulx, x1), max(uly, y1)])


def crs(geo: Geocoding) -> dict:
    out = {"proj:code": geo.code}
    w = wkt2(geo.code)
    if w is not None:
        out["proj:wkt2"] = w
    return out


def group_attributes(geo: Geocoding, r: int) -> dict:
    """A resolution group's attributes (§5.2)."""
    transform, shape, bbox = grid(geo, r)
    return {"zarr_conventions": [PROJ, SPATIAL], **crs(geo), "spatial:dimensions": ["y", "x"],
            "spatial:transform": transform, "spatial:shape": shape, "spatial:bbox": bbox,
            "spatial:registration": "pixel"}


def root_geozarr(product: Product, resolutions: list[int]) -> tuple[list[dict], dict]:
    """The root's GeoZarr CMOs and members (§5.3)."""
    geo = product.geocoding
    members = {**crs(geo), "spatial:bbox": grid(geo, resolutions[0])[2]}
    cmos = [PROJ, SPATIAL]
    if product.level == "L2A":
        cmos = [MULTISCALES, PROJ, SPATIAL]
        members["spatial:dimensions"] = ["y", "x"]
        members["spatial:registration"] = "pixel"
        members["multiscales"] = {"layout": [
            {"asset": f"r{r}m", "spatial:shape": grid(geo, r)[1], "spatial:transform": grid(geo, r)[0]}
            for r in resolutions]}
    return cmos, members


# ---------------------------------------------------------------- values (§6.1)


def typed(t: str):
    """A text as a value (§6.1): a decimal number as a JSON number, else the string."""
    if not DECIMAL.match(t):
        return t
    if "." in t or "e" in t or "E" in t:
        return json_number(float(t))
    sign = "-" if t[0] == "-" else ""
    digits = t.lstrip("+-").lstrip("0") or "0"
    if len(digits) > 16 or int(digits) > MAX_SAFE:
        return sign + digits
    return int(sign + digits)


def value(els: list[Element]):
    """The value of the one element of `els`, or None (absent)."""
    if len(els) != 1:
        return None
    t = text(els[0])
    return None if t is None else typed(t)


def string(els: list[Element]):
    if len(els) != 1:
        return None
    return text(els[0])


def measure(els: list[Element]):
    """A measure (§6.1): {"value", "unit"} when the element has a unit, else its value."""
    v = value(els)
    if v is None:
        return None
    unit = attr(els[0], "unit")
    return v if unit is None else {"value": v, "unit": unit}


def index(t: str | None) -> int | None:
    """An index attribute read as an integer: one to nine digits."""
    return int(t) if t is not None and re.fullmatch(r"[0-9]{1,9}", t) else None


def by_index(els: list[Element], name: str, i: int) -> list[Element]:
    return [e for e in els if index(attr(e, name)) == i]


def _put(out: dict, key: str, v) -> None:
    if v is not None and v != {} and v != []:
        out[key] = v


MAX_RECORDS = 64  # the most elements of a list copied as records (§6.2, §6.3)


def _records(els: list[Element], names: tuple[str, ...]) -> list[dict] | None:
    """One record per element, or None (absent) when there are more than MAX_RECORDS."""
    if len(els) > MAX_RECORDS:
        return None
    out = []
    for el in els:
        rec: dict = {}
        for n in names:
            _put(rec, n, value(kids(el, n)))
        out.append(rec)
    return out


def characteristics(product: Product) -> Element | None:
    found = path(product.metadata, "General_Info/Product_Image_Characteristics")
    return found[0] if len(found) == 1 else None


def band_metadata(product: Product, image_text: str, name: str, siz_b64: str) -> dict:
    """A band array's source metadata (§6.2)."""
    s: dict = {"IMAGE_FILE": image_text}
    x = characteristics(product)
    l2a = product.level == "L2A"
    m = re.fullmatch(r"B([0-9])([0-9])", name)
    pb = None
    if m:
        pb = "B" + (m.group(2) if m.group(1) == "0" else m.group(1) + m.group(2))
    elif name == "B8A":
        pb = "B8A"
    if x is not None and pb is not None:
        infos = [e for e in path(x, "Spectral_Information_List/Spectral_Information")
                 if attr(e, "physicalBand") == pb and index(attr(e, "bandId")) is not None]
        if len(infos) == 1:
            info = infos[0]
            i = index(attr(info, "bandId"))
            s["bandId"] = i
            s["physicalBand"] = pb
            _put(s, "RESOLUTION", value(kids(info, "RESOLUTION")))
            wl = kids(info, "Wavelength")
            if len(wl) == 1:
                w: dict = {}
                for n in ("MIN", "MAX", "CENTRAL"):
                    _put(w, n, measure(kids(wl[0], n)))
                _put(s, "Wavelength", w)
            _put(s, "PHYSICAL_GAINS", value(by_index(kids(x, "PHYSICAL_GAINS"), "bandId", i)))
            _put(s, "SOLAR_IRRADIANCE", measure(by_index(
                path(x, "Reflectance_Conversion/Solar_Irradiance_List/SOLAR_IRRADIANCE"), "bandId", i)))
            if l2a:
                _put(s, "BOA_QUANTIFICATION_VALUE", measure(path(x, "QUANTIFICATION_VALUES_LIST/BOA_QUANTIFICATION_VALUE")))
                _put(s, "BOA_ADD_OFFSET", value(by_index(
                    path(x, "BOA_ADD_OFFSET_VALUES_LIST/BOA_ADD_OFFSET"), "band_id", i)))
            else:
                _put(s, "QUANTIFICATION_VALUE", measure(kids(x, "QUANTIFICATION_VALUE")))
                _put(s, "RADIO_ADD_OFFSET", value(by_index(
                    path(x, "Radiometric_Offset_List/RADIO_ADD_OFFSET"), "band_id", i)))
    if x is not None and l2a:
        if name in ("AOT", "WVP"):
            _put(s, f"{name}_QUANTIFICATION_VALUE", measure(path(x, f"QUANTIFICATION_VALUES_LIST/{name}_QUANTIFICATION_VALUE")))
        elif name == "SCL":
            _put(s, "Scene_Classification_List", _records(
                path(x, "Scene_Classification_List/Scene_Classification_ID"),
                ("SCENE_CLASSIFICATION_TEXT", "SCENE_CLASSIFICATION_INDEX")))
    s["siz"] = siz_b64
    return s


def root_metadata(product: Product) -> dict:
    """The root's source metadata (§6.3)."""
    s: dict = {}
    info = path(product.metadata, "General_Info/Product_Info")
    for n in ("PRODUCT_URI", "PROCESSING_LEVEL", "PRODUCT_TYPE", "PROCESSING_BASELINE"):
        _put(s, n, string([c for e in info for c in kids(e, n)]))
    x = characteristics(product)
    if x is not None:
        _put(s, "Special_Values", _records(kids(x, "Special_Values"), ("SPECIAL_VALUE_TEXT", "SPECIAL_VALUE_INDEX")))
        _put(s, "U", value(path(x, "Reflectance_Conversion/U")))
    for n in ("TILE_ID", "SENSING_TIME"):
        _put(s, n, string(path(product.tile, f"General_Info/{n}")))
    tg = product.geocoding.element
    for n in ("HORIZONTAL_CS_NAME", "HORIZONTAL_CS_CODE"):
        _put(s, n, string(kids(tg, n)))
    return s


# ---------------------------------------------------------------- vzip_source's budget (§6.3)

BUDGET = 65536


def json_size(v) -> int:
    """The size of a value's JSON, as JSON.stringify writes it without whitespace, in UTF-8."""
    return len(json.dumps(v, separators=(",", ":"), ensure_ascii=False).encode())


MAX_TEXT = 1 << 16  # the largest XML document kept as text
READ_AHEAD = 16  # the candidates read together


def choose_texts(xml_sizes: dict[str, int], read_texts, lists: dict[str, list[str]]) -> dict:
    """The XML documents kept as text, with their text values (§6.3): `xml_sizes` maps each
    XML document's key to its size, `read_texts(keys)` reads the text values of candidates
    (documents of at most 65536 bytes), and `lists` holds the other members of `S`
    (`empty`, `empty_dirs`, `ignored`), which never move. The candidates are read in the
    order they are taken, at most READ_AHEAD at a time, and no longer once the next one
    cannot fit (§12.2): `S` with only its texts and `lists`, J bytes of JSON, and a
    candidate of n bytes as text is more than J + n bytes."""
    key_size = {k: json_size(k) for k in xml_sizes}
    fixed = [json_size(n) + 1 + 2 + sum(json_size(k) for k in v) + len(v) - 1 for n, v in lists.items() if v]
    name_xml, name_arrays = json_size("xml"), json_size("xml_arrays")
    texts: dict = {}
    xml_total = 0  # the members of `xml`, with their commas
    arrays_total = sum(key_size.values()) + len(key_size) - 1  # the members of `xml_arrays`, with commas
    n_arrays = len(key_size)

    def size(xml_total, n_text, arrays_total, n_arrays) -> int:
        members = list(fixed)
        if n_text:
            members.append(name_xml + 1 + 2 + xml_total)
        if n_arrays:
            members.append(name_arrays + 1 + 2 + arrays_total)
        return 2 + sum(members) + max(0, len(members) - 1)

    def fits(k: str) -> bool:  # could the candidate k still become text?
        return size(xml_total, len(texts), 0, 0) + xml_sizes[k] <= BUDGET

    candidates = sorted((k for k, n in xml_sizes.items() if n <= MAX_TEXT), key=lambda k: (xml_sizes[k], k))
    i = 0
    while i < len(candidates) and fits(candidates[i]):
        batch = []
        while i + len(batch) < len(candidates) and len(batch) < READ_AHEAD and fits(candidates[i + len(batch)]):
            batch.append(candidates[i + len(batch)])
        values = dict(zip(batch, read_texts(batch)))
        for k in batch:
            i += 1
            if not fits(k):
                break
            text = values[k]
            xt = xml_total + key_size[k] + 1 + json_size(text) + (1 if texts else 0)
            at = arrays_total - key_size[k] - (1 if n_arrays > 1 else 0)
            if size(xt, len(texts) + 1, at, n_arrays - 1) <= BUDGET:
                texts[k] = text
                xml_total, arrays_total, n_arrays = xt, at, n_arrays - 1
    return texts

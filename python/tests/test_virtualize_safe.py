"""The Python SAFE virtualizer (spec/virtualize/safe/profile.md) on the synthetic products.

Equivalence with the browser virtualizer is checked by
conformance/virtualize/compare.py, and pixel correctness, georeferencing and
the rebuilding of every file against GDAL by js/test/safe/verify.py.
"""

import io
import json
import struct
import zipfile
from pathlib import Path

import pytest

from vzip.virtualize import Rejected, virtualize
from vzip.virtualize.common import text_json
from vzip.virtualize.safe import jp2, zipdir
from vzip.virtualize.safe.metadata import BUDGET, MULTISCALES, PROJ, SPATIAL, choose_texts, json_size, typed, wkt2
from vzip.virtualize.safe.product import folders
from vzip.virtualize.safe.virtualize import Shared

FIXTURES = Path(__file__).parents[2] / "fixtures" / "safe"
URL = "https://data.test/safe/{}"
J2K = [{"name": "imagecodecs_jpeg2k"}]
RGB = [{"name": "transpose", "configuration": {"order": [1, 2, 0]}}, {"name": "imagecodecs_jpeg2k"}]


def run(name: str):
    p = FIXTURES / name
    return virtualize(str(p), url=URL.format(name) + ("/" if p.is_dir() else ""))


# product: (summary members, {array: (shape, data type, chunk shape, codecs)})
CASES = {
    "safe_l1c": ({"level": "L1C", "form": "store", "groups": 3, "bands": 14, "chunks": 215, "edgeChunks": 92,
                  "dataSources": 12, "objects": 52, "folderMarkers": 10, "xmlText": 12, "xmlArrays": 5,
                  "otherObjects": 10, "emptyObjects": 1, "tileParts": 215}, {
        "r10m/B02": ([110, 110], "uint16", [32, 32], J2K),
        "r10m/TCI": ([3, 110, 110], "uint8", [3, 16, 16], RGB),
        "r20m/B8A": ([55, 55], "uint16", [20, 20], J2K),
        "r60m/B10": ([19, 19], "uint16", [6, 6], J2K),
    }),
    "safe_l1c.SAFE.zip": ({"level": "L1C", "form": "zip", "bands": 14, "chunks": 215, "objects": 42,
                           "folderMarkers": 0, "xmlText": 12, "xmlArrays": 5, "otherObjects": 10}, {}),
    "safe_l2a": ({"level": "L2A", "groups": 3, "bands": 36, "chunks": 461, "edgeChunks": 218, "dataSources": 27,
                  "xmlArrays": 6, "otherObjects": 11, "emptyObjects": 1}, {
        "r10m/AOT": ([110, 110], "uint16", [32, 32], J2K),
        "r20m/SCL": ([55, 55], "uint8", [20, 20], J2K),
        "r20m/TCI": ([3, 55, 55], "uint8", [3, 40, 40], RGB),
        "r60m/TCI": ([3, 19, 19], "uint8", [3, 16, 16], RGB),
        "r60m/WVP": ([19, 19], "uint16", [6, 6], J2K),
    }),
    "safe_l2a_deflated_xml.SAFE.zip": ({"form": "zip", "bands": 36, "chunks": 461, "xmlText": 12, "xmlArrays": 6}, {}),
    "safe_l1c_pb0207": ({"bands": 4, "otherObjects": 12}, {"r20m/B05": ([55, 55], "uint16", [20, 20], J2K)}),
    "safe_l2a_pb0212": ({"bands": 22, "groups": 3}, {"r60m/B02": ([19, 19], "uint16", [6, 6], J2K)}),
    "safe_latin1_xml": ({"bands": 1, "xmlText": 4, "xmlArrays": 0}, {}),
    "safe_metadata_gaps": ({"bands": 4, "groups": 2}, {}),
    "safe_big_header": ({"bands": 2, "chunks": 25, "dataSources": 6}, {}),
    "safe_zip64.SAFE.zip": ({"form": "zip", "bands": 2, "chunks": 25}, {}),
    # Three granules (processing baseline 02.07), and an empty directory.
    "safe_l2a_pb0207": ({"level": "L2A", "bands": 22, "groups": 3, "emptyDirs": 1, "otherObjects": 11}, {
        "r60m/B01": ([19, 19], "uint16", [6, 6], J2K)}),
    "safe_l2a_pb0207.SAFE.zip": ({"form": "zip", "bands": 22, "emptyDirs": 1, "otherObjects": 11}, {}),
    # The names of PSD 14.2, and image files outside IMG_DATA (the cloud probability an other object).
    "safe_l2a_pb0206": ({"level": "L2A", "bands": 22, "emptyDirs": 0, "otherObjects": 12}, {
        "r20m/SCL": ([55, 55], "uint8", [20, 20], J2K)}),
    "safe_metadata_many_records": ({"level": "L2A", "bands": 2}, {}),
}


def test_virtualizes_the_synthetic_products():
    for name, (summary, arrays) in CASES.items():
        fmt, out = run(name)
        assert fmt == "safe", name
        assert {**out.summary, **summary} == out.summary, (name, out.summary)
        for path, (shape, dtype, chunk_shape, codecs) in arrays.items():
            d = out.docs[f"{path}/zarr.json"]
            assert (d["shape"], d["data_type"], d["chunk_grid"]["configuration"]["chunk_shape"], d["codecs"],
                    d["dimension_names"]) == (shape, dtype, chunk_shape, codecs,
                                              ["c", "y", "x"] if len(shape) == 3 else ["y", "x"]), (name, path)
        root = out.docs["zarr.json"]["attributes"]
        level = out.summary["level"]
        assert [c["name"] for c in root["zarr_conventions"]] == (
            ["vzip_virtualized", "multiscales", "proj", "spatial"] if level == "L2A" else
            ["vzip_virtualized", "proj", "spatial"]), name
        assert ("multiscales" in root) == (level == "L2A"), name
        # Every chunk of every band, and every reference within the payload limit.
        for key, d in out.docs.items():
            if d.get("node_type") == "array" and key.startswith("r"):
                grid = [-(-s // c) for s, c in zip(d["shape"], d["chunk_grid"]["configuration"]["chunk_shape"])]
                n = sum(1 for k in out.refs if k.startswith(key[: -len("zarr.json")] + "c/"))
                assert n == grid[-1] * grid[-2], (name, key)


def test_georeferencing():
    _, out = run("safe_l2a")
    g = out.docs["r20m/zarr.json"]["attributes"]
    assert g["zarr_conventions"] == [PROJ, SPATIAL]
    assert g["proj:code"] == "EPSG:32632" and g["proj:wkt2"] == wkt2("EPSG:32632")
    assert g["spatial:transform"] == [20.0, 0.0, 499980.0, 0.0, -20.0, 5200020.0]
    assert g["spatial:shape"] == [55, 55]
    assert g["spatial:bbox"] == [499980.0, 5200020.0 - 20 * 55, 499980.0 + 20 * 55, 5200020.0]
    assert g["spatial:registration"] == "pixel" and g["spatial:dimensions"] == ["y", "x"]
    root = out.docs["zarr.json"]["attributes"]
    assert root["zarr_conventions"][1] == MULTISCALES
    assert root["spatial:bbox"] == out.docs["r10m/zarr.json"]["attributes"]["spatial:bbox"]
    assert root["multiscales"] == {"layout": [
        {"asset": f"r{r}m", "spatial:shape": [n, n], "spatial:transform": [float(r), 0.0, 499980.0, 0.0, -float(r), 5200020.0]}
        for r, n in ((10, 110), (20, 55), (60, 19))]}
    # The band arrays carry neither proj nor spatial (spec/virtualize/safe.md §5.2).
    assert set(out.docs["r20m/B05/zarr.json"]["attributes"]) == {"zarr_conventions", "vzip_virtualized"}
    _, out = run("safe_l1c")
    assert "multiscales" not in out.docs["zarr.json"]["attributes"]


def test_wkt2_is_pyproj_s_definition():
    pyproj = pytest.importorskip("pyproj")
    for c in [*range(32601, 32661), *range(32701, 32761)]:
        w = wkt2(f"EPSG:{c}")
        crs, epsg = pyproj.CRS.from_wkt(w), pyproj.CRS.from_epsg(c)
        assert crs.equals(epsg) and crs.to_epsg(min_confidence=100) == c, c
        assert w == epsg.to_wkt().split(",USAGE[")[0] + f',ID["EPSG",{c}]]', c
    for code in ("EPSG:32600", "EPSG:32661", "EPSG:4326", "EPSG:032632", "EPSG:327010"):
        assert wkt2(code) is None, code


def test_source_metadata():
    _, out = run("safe_l2a")
    root = out.docs["zarr.json"]["attributes"]["vzip_virtualized"]
    assert root["source"] == {"url": URL.format("safe_l2a") + "/"}
    assert root["safe"] == {
        "PRODUCT_URI": "S2B_MSIL2A_20240103T101329_N0510_R022_T32TNS_20240103T113848.SAFE",
        "PROCESSING_LEVEL": "Level-2A", "PRODUCT_TYPE": "S2MSI2A", "PROCESSING_BASELINE": "05.10",
        "Special_Values": [{"SPECIAL_VALUE_TEXT": "NODATA", "SPECIAL_VALUE_INDEX": 0},
                           {"SPECIAL_VALUE_TEXT": "SATURATED", "SPECIAL_VALUE_INDEX": 65535}],
        "U": 1.03421885250175,
        "TILE_ID": "S2B_OPER_MSI_L2A_TL_2BPS_20240103T110304_A035655_T32TNS_N05.10",
        "SENSING_TIME": "2024-01-03T10:18:03.604815Z",
        "HORIZONTAL_CS_NAME": "WGS84 / UTM zone 32N", "HORIZONTAL_CS_CODE": "EPSG:32632",
    }
    b04 = out.docs["r10m/B04/zarr.json"]["attributes"]["vzip_virtualized"]["safe"]
    assert {k: v for k, v in b04.items() if k != "siz"} == {
        "IMAGE_FILE": "GRANULE/L2A_T32TNS_A035655_20240103T101328/IMG_DATA/R10m/T32TNS_20240103T101329_B04_10m",
        "bandId": 3, "physicalBand": "B4", "RESOLUTION": 10,
        "Wavelength": {"MIN": {"value": 646, "unit": "nm"}, "MAX": {"value": 685, "unit": "nm"},
                       "CENTRAL": {"value": 665, "unit": "nm"}},
        "PHYSICAL_GAINS": 4.76278246, "SOLAR_IRRADIANCE": {"value": 1512.79, "unit": "W/m²/µm"},
        "BOA_QUANTIFICATION_VALUE": {"value": 10000, "unit": "none"}, "BOA_ADD_OFFSET": -1000,
    }
    scl = out.docs["r20m/SCL/zarr.json"]["attributes"]["vzip_virtualized"]["safe"]
    assert scl["Scene_Classification_List"][9] == {"SCENE_CLASSIFICATION_TEXT": "SC_CLOUD_HIGH_PROBA",
                                                   "SCENE_CLASSIFICATION_INDEX": 9}
    wvp = out.docs["r20m/WVP/zarr.json"]["attributes"]["vzip_virtualized"]["safe"]
    assert wvp["WVP_QUANTIFICATION_VALUE"] == {"value": 1000.0, "unit": "cm"} and "bandId" not in wvp
    # Level-1C names; before PB 04.00 there are no offsets.
    _, out = run("safe_l1c")
    b8a = out.docs["r20m/B8A/zarr.json"]["attributes"]["vzip_virtualized"]["safe"]
    assert (b8a["physicalBand"], b8a["QUANTIFICATION_VALUE"], b8a["RADIO_ADD_OFFSET"]) == (
        "B8A", {"value": 10000, "unit": "none"}, -1000)
    _, out = run("safe_l1c_pb0207")
    assert "RADIO_ADD_OFFSET" not in out.docs["r10m/B02/zarr.json"]["attributes"]["vzip_virtualized"]["safe"]
    # Missing, non-numeric and repeated values are absent, not rejected (§6.1).
    _, out = run("safe_metadata_gaps")
    s = {b: out.docs[f"r10m/{b}/zarr.json"]["attributes"]["vzip_virtualized"]["safe"] for b in ("B02", "B03", "B04")}
    assert "SOLAR_IRRADIANCE" not in s["B02"] and "SOLAR_IRRADIANCE" in s["B04"]
    assert set(s["B03"]) == {"IMAGE_FILE", "siz"}
    assert s["B04"]["RADIO_ADD_OFFSET"] == "n/a"
    # More than 64 records: the list is absent.
    _, out = run("safe_metadata_many_records")
    assert "Special_Values" not in out.docs["zarr.json"]["attributes"]["vzip_virtualized"]["safe"]
    assert set(out.docs["r60m/SCL/zarr.json"]["attributes"]["vzip_virtualized"]["safe"]) == {"IMAGE_FILE", "siz"}
    # The names of PSD 14.2 give the same members as the later ones.
    old, new = run("safe_l2a_pb0206")[1], run("safe_l2a_pb0207")[1]
    for key in ("r20m/B05/zarr.json", "r20m/SCL/zarr.json", "r10m/AOT/zarr.json"):
        assert old.docs[key] == new.docs[key], key
    a = old.docs["zarr.json"]["attributes"]["vzip_virtualized"]["safe"]
    b = new.docs["zarr.json"]["attributes"]["vzip_virtualized"]["safe"]
    assert {**a, "PRODUCT_URI": 0, "PROCESSING_BASELINE": 0, "TILE_ID": 0} == \
        {**b, "PRODUCT_URI": 0, "PROCESSING_BASELINE": 0, "TILE_ID": 0}
    assert a["PRODUCT_URI"].endswith("_N0206_R022_T32TNS_20240103T113848.SAFE") and a["TILE_ID"].endswith("N02.06")
    assert len(a["Special_Values"]) == 2
    assert old.docs["r20m/SCL/zarr.json"]["attributes"]["vzip_virtualized"]["safe"]["Scene_Classification_List"][9] == \
        {"SCENE_CLASSIFICATION_TEXT": "SC_CLOUD_HIGH_PROBA", "SCENE_CLASSIFICATION_INDEX": 9}
    assert "vzip_source/objects/GRANULE/L2A_T32TNS_A035655_20240103T101328/QI_DATA/T32TNS_20240103T101329_CLD_60m.jp2" \
        in old.refs


def test_vzip_source():
    _, out = run("safe_l1c")
    s = out.docs["vzip_source/zarr.json"]["attributes"]["vzip_virtualized"]["safe"]
    assert set(s) == {"xml", "xml_arrays", "empty"}
    assert s["empty"] == ["GRANULE/L1C_T32TNS_A035655_20240103T101328/AUX_DATA/AUX_EMPTY"]
    assert "GRANULE/L1C_T32TNS_A035655_20240103T101328/MTD_TL.xml" in s["xml_arrays"]
    assert json_size(s) <= BUDGET
    # The budget (spec/virtualize/safe.md §6.3), restated: from every XML document an array,
    # the candidates, smallest first, become text while S stays within 65536 bytes of JSON.
    base = FIXTURES / "safe_l1c"
    files = {p.relative_to(base).as_posix(): p.read_bytes() for p in base.rglob("*") if p.is_file()}
    xml = sorted(k for k in files if k in s["xml"] or k in s["xml_arrays"])
    want = {"xml": {}, "xml_arrays": list(xml), "empty": s["empty"]}
    for k in sorted((k for k in xml if len(files[k]) <= 65536), key=lambda k: (len(files[k]), k)):
        trial = {"xml": {**want["xml"], k: text_json(files[k])}, "xml_arrays": [x for x in want["xml_arrays"] if x != k],
                 "empty": s["empty"]}
        if json_size({m: v for m, v in trial.items() if v}) <= BUDGET:
            want = trial
    assert s["xml"] == want["xml"] and s["xml_arrays"] == want["xml_arrays"]
    assert any(len(files[k]) <= 65536 for k in s["xml_arrays"])  # the budget moved some
    for i, k in enumerate(s["xml_arrays"]):
        assert out.docs[f"vzip_source/xml/{i}/zarr.json"]["shape"] == [len(files[k])]
    # Folder markers are dropped; the quicklook and the masks are other objects.
    assert not any("$folder$" in k for k in [*out.refs, *json.dumps(s)])
    others = sorted(k for k in out.refs if k.startswith("vzip_source/objects/"))
    assert "vzip_source/objects/S2B_MSIL1C_20240103T101329_N0510_R022_T32TNS_20240103T110304-ql.jpg" in others
    assert len(others) == 10
    assert out.docs["vzip_source/jp2/r60m/B01/zarr.json"]["dimension_names"] == ["byte"]
    # A document that is not UTF-8 is a tagged text value.
    _, out = run("safe_latin1_xml")
    s = out.docs["vzip_source/zarr.json"]["attributes"]["vzip_virtualized"]["safe"]
    assert s["xml"]["DATASTRIP/DS_2BPS_20240103T110304_S20240103T101328/QI_DATA/GENERAL_QUALITY.xml"] == {
        "latin1": '<?xml version="1.0" encoding="ISO-8859-1"?>\n<report>Qualité: bonne</report>\n'}


def _bytes(out, sources: dict) -> dict[str, bytes]:
    """Each reference entry's bytes, with `sources` giving each source's bytes."""
    data = {}
    for key, ranges in out.refs.items():
        b = b""
        for r in ranges:
            if isinstance(r, Shared):
                b += r.value
            elif isinstance(r, tuple):
                b += sources[r[0]][r[1] : r[1] + r[2]]
            else:
                b += r
        data[key] = b
    return data


def test_the_two_forms_agree():
    for d, z in (("safe_l1c", "safe_l1c.SAFE.zip"), ("safe_l2a", "safe_l2a_deflated_xml.SAFE.zip"),
                 ("safe_l2a_pb0207", "safe_l2a_pb0207.SAFE.zip")):
        _, a = run(d)
        _, b = run(z)
        docs_a, docs_b = dict(a.docs), dict(b.docs)
        assert docs_a.pop("zarr.json")["attributes"]["vzip_virtualized"].pop("source") != \
            docs_b.pop("zarr.json")["attributes"]["vzip_virtualized"].pop("source")
        assert docs_a == docs_b, d
        files = {p.relative_to(FIXTURES / d).as_posix(): p.read_bytes() for p in (FIXTURES / d).rglob("*") if p.is_file()}
        zbytes = (FIXTURES / z).read_bytes()
        bytes_a, bytes_b = _bytes(a, files), _bytes(b, {0: zbytes})
        bytes_b.update(b.bytes_entries)
        assert bytes_a.keys() | a.bytes_entries.keys() == bytes_b.keys(), d
        assert all(bytes_a[k] == bytes_b[k] for k in bytes_a), d
        # The source tables: one url source per referenced object, or the zip file; then the data sources.
        urls_a, data_a, _ = a.table()
        urls_b, data_b, _ = b.table()
        assert urls_b == [URL.format(z)] and data_a == data_b
        assert urls_a == sorted(urls_a) and all(u.startswith(URL.format(d) + "/") for u in urls_a)
    # The empty directory: a folder marker, and a zip directory entry.
    assert a.docs["vzip_source/zarr.json"]["attributes"]["vzip_virtualized"]["safe"]["empty_dirs"] == ["AUX_DATA"]


def test_folders():
    """Folder markers and directory keys, and the empty directories they name (spec/virtualize/safe.md §2.1)."""
    cases = [
        (["a/x", "a_$folder$"], [], {"a_$folder$"}, []),
        (["a/x", "a_$folder$", "b_$folder$", "c/d_$folder$", "c_$folder$"], [],
         {"a_$folder$", "b_$folder$", "c/d_$folder$", "c_$folder$"}, ["b", "c/d"]),
        (["_$folder$", "x$folder$"], [], set(), []),  # no directory name; not the suffix
        (["a/x"], ["a/", "e/", "e/f/", "g/"], set(), ["e/f", "g"]),  # zip directory entries, or S3 `d/` objects
        (["g/h_$folder$"], ["g/"], {"g/h_$folder$"}, ["g/h"]),
    ]
    for keys, directories, markers, empty in cases:
        assert folders(keys, directories) == (markers, empty), keys


def test_texts_are_read_only_while_they_can_fit():
    """The candidates are read in order, and no longer once one cannot fit (spec/virtualize/safe/profile.md §12.2)."""
    sizes = {f"d{i:02d}.xml": 30000 for i in range(40)} | {"small.xml": 10, "big.xml": 70000}
    read = []

    def texts(keys):
        read.extend(keys)
        return ["x" * sizes[k] for k in keys]

    got = choose_texts(sizes, texts, {"empty": [], "empty_dirs": ["e"], "ignored": []})
    assert list(got) == ["small.xml", "d00.xml", "d01.xml"][: len(got)] and "d00.xml" in got
    assert len(read) <= 16 + 1 and "big.xml" not in read


def test_chunks():
    _, out = run("safe_l1c")
    files = {p.relative_to(FIXTURES / "safe_l1c").as_posix(): p.read_bytes()
             for p in (FIXTURES / "safe_l1c").rglob("*") if p.is_file()}
    band = "GRANULE/L1C_T32TNS_A035655_20240103T101328/IMG_DATA/T32TNS_20240103T101329_B01.jp2"
    interior, corner = out.refs["r60m/B01/c/0/0"], out.refs["r60m/B01/c/3/3"]
    head, rest, sot, body, tail = interior
    assert isinstance(rest, Shared) and body[0] == band and tail == b"\xff\xd9"
    # SOC, then SIZ with Rsiz 0, the image [0, 6)², tiles of 6 at 0.
    assert head[:4] == b"\xff\x4f\xff\x51" and struct.unpack(">HIIIIIIIIH", head[6:42]) == (0, 6, 6, 0, 0, 6, 6, 0, 0, 1)
    assert sot[:6] == b"\xff\x90\x00\x0a\x00\x00" and sot[10:] == b"\x00\x01"
    data = files[band]
    assert data[body[1] - 12 : body[1] - 6] == b"\xff\x90\x00\x0a\x00\x00"  # tile 0's own SOT
    # The corner tile [18, 19)²: the image [18, 24)², tiles of 1 × 1 at 18, and 35 empty tiles.
    head, rest, sot, body, shared = corner
    assert isinstance(shared, Shared) and out.refs["r60m/B01/c/3/2"][4] != shared
    tail = shared.value
    assert struct.unpack(">HIIIIIIIIH", head[6:42]) == (0, 24, 24, 18, 18, 1, 1, 18, 18, 1)
    assert data[body[1] - 12 : body[1] - 6] == b"\xff\x90\x00\x0a\x00\x0f"  # tile 15
    tiles, o = [], 0
    while tail[o : o + 2] == b"\xff\x90":
        isot, psot = struct.unpack_from(">HI", tail, o + 4)
        assert tail[o + 12 : o + 14] == b"\xff\x93" and set(tail[o + 14 : o + psot]) <= {0}
        tiles.append((isot, psot - 14))
        o += psot
    assert tail[o:] == b"\xff\xd9"
    # One decomposition level, one precinct per resolution: a 1 × 1 tile at (x, y) has a packet at
    # resolution 1, and one at resolution 0 when x and y are even (ISO/IEC 15444-1 B.5).
    def packets(k):
        x, y = 18 + k % 6, 18 + k // 6
        return 1 + (x % 2 == 0 and y % 2 == 0)

    assert tiles == [(k, packets(k)) for k in range(1, 36)]


def _plt_counts(data: bytes, cs: jp2.Codestream) -> list[int]:
    out = []
    for s, _ in cs.tiles:
        q, n = s + 12, 0
        while data[q : q + 2] != b"\xff\x93":
            m, length = struct.unpack_from(">HH", data, q)
            if m == 0xFF58:
                n += sum(1 for b in data[q + 5 : q + 2 + length] if not b & 0x80)
            q += 2 + length
        out.append(n)
    return out


def test_empty_packets_match_the_plt_markers():
    """The packet count of spec/virtualize/safe/profile.md §12.6 against the PLT markers' packet lengths
    of every tile of every fixture band file, edge tiles included."""
    checked = 0
    for name in ("safe_l1c", "safe_l2a", "safe_big_header"):
        for p in sorted((FIXTURES / name).rglob("*.jp2")):
            if "IMG_DATA" not in p.as_posix():
                continue
            data = p.read_bytes()
            cs, _ = jp2.read_band(lambda o, n: data[o : o + n], 0, len(data))
            nx, _ = cs.grid
            for t, plt in enumerate(_plt_counts(data, cs)):
                x0, y0 = (t % nx) * cs.tile_w, (t // nx) * cs.tile_h
                x1, y1 = min(x0 + cs.tile_w, cs.width), min(y0 + cs.tile_h, cs.height)
                assert jp2.empty_packets(cs, x0, x1, y0, y1) == plt, (p.name, t)
                checked += 1
    assert checked > 500


def test_values():
    for text, value in [("10", 10), ("-1000", -1000), ("+7", 7), ("007", 7), ("-0", 0), ("1.5", 1.5),
                        ("1000.0", 1000.0), ("1e3", 1000.0), ("-2.5E-1", -0.25), ("9007199254740991", 9007199254740991),
                        ("9007199254740992", "9007199254740992"), ("-00012345678901234567890", "-12345678901234567890"),
                        ("1e999", "Infinity"), ("05.10", 5.1), (".5", ".5"), ("5.", "5."), ("1,5", "1,5"),
                        ("NODATA", "NODATA"), ("١٢", "١٢")]:
        assert typed(text) == value, text


def test_zip_directories(tmp_path):
    """Zip files written by Python's zipfile: stored and deflated entries, and ZIP64 extra fields."""
    payload = {"P.SAFE/manifest.safe": b"<a/>", "P.SAFE/MTD_MSIL1C.xml": b"<b>" * 1000, "P.SAFE/x/": b""}
    for compression, zip64 in ((zipfile.ZIP_STORED, False), (zipfile.ZIP_DEFLATED, False), (zipfile.ZIP_DEFLATED, True)):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression) as z:
            for name, data in payload.items():
                with z.open(name, "w", force_zip64=zip64) as f:
                    f.write(data)
        data = buf.getvalue()
        read = lambda o, n: data[o : o + n]  # noqa: E731
        d = zipdir.central_directory(read, len(data))
        root, objects, ignored, dirs = zipdir.product_entries(d)
        zipdir.locate(read, d, objects)
        assert root == "P.SAFE" and set(objects) == {"manifest.safe", "MTD_MSIL1C.xml"} and ignored == []
        assert dirs == ["x/"]
        for key, e in objects.items():
            raw = data[e.ds : e.ds + e.cs]
            got = zipdir.inflate(raw, e) if e.method == 8 else raw
            assert got == payload[f"P.SAFE/{key}"], (compression, zip64, key)


REJECTIONS = [
    ("safe_reject_old_format", "no MTD_MSIL1C.xml or MTD_MSIL2A.xml"),
    ("safe_reject_two_mtd", "both MTD_MSIL1C.xml and MTD_MSIL2A.xml"),
    ("safe_reject_no_manifest", "manifest.safe"),
    ("safe_reject_wrong_root_element", "root element is not Level-1C_User_Product"),
    ("safe_reject_two_granules", "is listed twice"),
    ("safe_reject_no_image_file", "no granule has an IMAGE_FILE"),
    ("safe_reject_no_band_file", "no image file is in IMG_DATA"),
    ("safe_reject_missing_band_file", "B02.jp2 is not in the product"),
    ("safe_reject_mixed_granule_dirs", "two granule directories"),
    ("safe_reject_image_file_form", "is not GRANULE/<g>/<directory>/<file>"),
    ("safe_reject_no_tile_metadata", "MTD_TL.xml is not in the product"),
    ("safe_reject_bad_cs_code", "is not EPSG: and digits"),
    ("safe_reject_no_geoposition", "Geoposition of resolution 60: 0 elements"),
    ("safe_reject_zero_xdim", "XDIM or YDIM is 0"),
    ("safe_reject_infinite_ulx", "not a finite decimal number"),
    ("safe_reject_duplicate_resolution", "two Sizes have the resolution 60"),
    ("safe_reject_size_matches_no_resolution", "matches no resolution"),
    ("safe_reject_size_matches_two_resolutions", "matches several resolution"),
    ("safe_reject_duplicate_band_name", "two band files at 60 m have the band name B01"),
    ("safe_reject_bad_band_name", "is not ASCII letters and digits"),
    ("safe_reject_not_jp2", "not a JP2 file"),
    ("safe_reject_jp2c_not_last", "jp2c box does not end at the end of the file"),
    ("safe_reject_box_beyond_file", "box reaches beyond the file"),
    ("safe_reject_tlm", "TLM marker"),
    ("safe_reject_ppm", "PPM marker"),
    ("safe_reject_sop", "SOP or EPH"),
    ("safe_reject_eph", "SOP or EPH"),
    ("safe_reject_two_tile_parts", "more than one tile-part"),
    ("safe_reject_tile_parts_out_of_order", "not in raster order"),
    ("safe_reject_psot_zero", "Psot 0"),
    ("safe_reject_no_eoc", "beyond the codestream"),
    ("safe_reject_bytes_after_eoc", "does not end with EOC"),
    ("safe_reject_tile_origin", "origin is not 0"),
    ("safe_reject_signed", "signed"),
    ("safe_reject_subsampled", "subsampled"),
    ("safe_reject_precision_17", "17 bits"),
    ("safe_reject_two_components", "2 components"),
    ("safe_reject_mixed_precision", "differ in precision"),
    ("safe_reject_xml_doctype", "MTD_MSIL1C.xml is not well formed"),
    ("safe_reject_xml_not_utf8_metadata", "MTD_MSIL1C.xml is not UTF-8"),
    ("safe_reject_xml_unclosed_tile_metadata", "MTD_TL.xml is not well formed"),
    ("safe_reject_xml_too_deep", "nest more than 256 deep"),
    ("safe_reject_tail_too_long", "empty tiles of a chunk of tile 15 would take more than 4096 bytes"),
    ("safe_reject_tails_over_band_size", "would take 4342 bytes, more than the band file's 3116"),
    ("safe_reject_zip_encrypted.SAFE.zip", "encrypted"),
    ("safe_reject_zip_deflated_band.SAFE.zip", "is compressed: only XML documents"),
    ("safe_reject_zip_deflated_mask.SAFE.zip", "MSK_CLASSI_B00.jp2 is compressed"),
    ("safe_reject_zip_bzip2.SAFE.zip", "compression method 12"),
    ("safe_reject_zip_bad_crc.SAFE.zip", "CRC-32"),
    ("safe_reject_zip_two_roots.SAFE.zip", "two root directories"),
    ("safe_reject_zip_not_safe_root.SAFE.zip", "not under a .SAFE directory"),
    ("safe_reject_zip_duplicate_name.SAFE.zip", "two entries named"),
    ("safe_reject_zip_dotdot.SAFE.zip", "empty, . or .. segment"),
    ("safe_reject_zip_truncated_cd.SAFE.zip", "fewer entries than it says"),
    ("safe_reject_zip_no_eocd.SAFE.zip", "no end of central directory"),
    ("safe_reject_zip_comment_misaligned.SAFE.zip", "no end of central directory"),
    ("safe_reject_zip_overlapping_entries.SAFE.zip", "local headers and data overlap"),
    ("safe_reject_zip_deflated_too_large.SAFE.zip", "larger than 2\\^26 bytes"),
    ("safe_reject_zip_inflated_total.SAFE.zip", "larger than 2\\^27 bytes together"),
]


def test_every_rejection_fixture_is_listed():
    assert {n for n, _ in REJECTIONS} == {p.name for p in FIXTURES.iterdir() if p.name.startswith("safe_reject")}


@pytest.mark.parametrize("name,message", REJECTIONS)
def test_rejects(name, message):
    with pytest.raises(Rejected, match=message):
        run(name)


def test_rejects_empty_packets_past_2_32():
    """An empty tile of more than 2^32 − 15 packets (its Psot would not fit) is rejected, not packed."""
    cs = jp2.Codestream(n=1 << 20, c0=0, siz=bytes(43), rest=b"", width=3 << 19, height=1 << 20,
                        tile_w=1 << 20, tile_h=1 << 20, components=1, precision=8, layers=65535,
                        coding=[(0, [(0, 0)])], tiles=[(0, 14), (14, 14)])
    assert jp2.empty_packets(cs, 3 << 19, 2 << 20, 0, 1 << 20) > 2**32  # the chunk's one empty tile
    with pytest.raises(Rejected, match="would take more than 4096 bytes"):
        jp2.chunk_parts(cs, 1)

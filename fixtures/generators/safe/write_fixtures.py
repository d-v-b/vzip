"""Writes synthetic Sentinel-2 SAFE products to fixtures/safe/: each a
directory `<name>/` (the directory form, a store input), and some also as a
zip file `<name>.SAFE.zip` (the zip form), covering the SAFE profile
(spec/virtualize.md §12) and its convention (spec/virtualize/safe.md).

The products mimic real ones, scaled down. Band files are JPEG 2000 files
written by rasterio's JP2OpenJPEG driver (OpenJPEG), tiled, with PLT markers
and GML-in-JP2 georeferencing, whose main header is then rewritten to the
form Kakadu writes for Sentinel-2 (Rsiz 0, its two COM markers), as the real
band files have it. The images are 110 × 110 at "10 m" in tiles of 32 (an
edge of 14), 55 × 55 at "20 m" in tiles of 20 (an edge of 15), and 19 × 19
at "60 m" in tiles of 6 (an edge of 1, so the corner chunk holds 35 empty
tiles); the true-color images have other tilings, as the real ones do. Pixel
values are seeded gradients with noise, unique per band, so that a misplaced
tile cannot pass. The XML documents are trimmed from real products' (PB 05.10
and 02.12), with the values of the scaled images.

Accepting products: `safe_l1c` (Level-1C, PB 05.10, with the Google Cloud
mirror's quicklook and folder markers, and an empty object), `safe_l2a`
(Level-2A, PB 05.10: every band at 10, 20 and 60 m, AOT, WVP, SCL, TCI at
three tilings), `safe_l1c_pb0207` (GML masks, no offsets),
`safe_l2a_pb0212` (no B01 at 20 m, no offsets), `safe_latin1_xml` (an XML
document that is not UTF-8), `safe_metadata_gaps` (source metadata that is
missing, not numeric, or repeated), `safe_big_header` (an XLBox box and
LBox 0 jp2c, a COC, several precincts). Zip files: `safe_l1c.SAFE.zip` (as
ESA zips: stored, with directory entries, local extra fields that differ
from the central ones), `safe_l2a_deflated_xml.SAFE.zip` (XML deflated) and
`safe_zip64.SAFE.zip` (ZIP64 end records and extra fields).

Rejecting products (`safe_reject_*`), each the smallest product that breaks
one rule, as directories or zip files.

Usage: uv run python fixtures/generators/safe/write_fixtures.py
"""

from __future__ import annotations

import hashlib
import io
import shutil
import struct
import tempfile
import zlib
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin

OUT = Path(__file__).parents[2] / "safe"
ULX, ULY = 499980, 5200020
SIZES = {10: 110, 20: 55, 60: 19}
TILES = {10: 32, 20: 20, 60: 6}
TILE = "T32TNS"
SENSING = "20240103T101329"
GRANULE_L1C = "L1C_T32TNS_A035655_20240103T101328"
GRANULE_L2A = "L2A_T32TNS_A035655_20240103T101328"
DS_L1C = "DS_2BPS_20240103T110304_S20240103T101328"
DS_L2A = "DS_2BPS_20240103T113848_S20240103T101328"
PRODUCT_L1C = "S2B_MSIL1C_20240103T101329_N0510_R022_T32TNS_20240103T110304"
PRODUCT_L2A = "S2B_MSIL2A_20240103T101329_N0510_R022_T32TNS_20240103T113848"

# bandId, physical band, resolution, wavelength min, max, central, physical gain, solar irradiance
SPECTRAL = [
    (0, "B1", 60, "411", "456", "442.3", "3.95638156", "1874.3"),
    (1, "B2", 10, "456", "532", "492.3", "3.81301175", "1959.75"),
    (2, "B3", 10, "536", "582", "559", "4.22592996", "1824.93"),
    (3, "B4", 10, "646", "685", "665", "4.76278246", "1512.79"),
    (4, "B5", 20, "694", "714", "703.8", "5.17211547", "1425.78"),
    (5, "B6", 20, "730", "748", "739.1", "5.08094735", "1291.13"),
    (6, "B7", 20, "766", "794", "779.7", "4.75827839", "1175.57"),
    (7, "B8", 10, "774", "907", "833", "6.81296935", "1041.28"),
    (8, "B8A", 20, "848", "880", "864", "5.75169287", "953.93"),
    (9, "B9", 60, "930", "957", "943.2", "9.35522381", "817.58"),
    (10, "B10", 60, "1339", "1415", "1376.9", "56.80432134", "365.41"),
    (11, "B11", 20, "1538", "1679", "1610.4", "37.26114667", "247.08"),
    (12, "B12", 20, "2065", "2303", "2185.7", "108.56439515", "87.75"),
]
SCENE = ["SC_NODATA", "SC_SATURATED_DEFECTIVE", "SC_DARK_FEATURE_SHADOW", "SC_CLOUD_SHADOW", "SC_VEGETATION",
         "SC_NOT_VEGETATED", "SC_WATER", "SC_UNCLASSIFIED", "SC_CLOUD_MEDIUM_PROBA", "SC_CLOUD_HIGH_PROBA",
         "SC_THIN_CIRRUS", "SC_SNOW_ICE"]
L1C_BANDS = [("B01", 60), ("B02", 10), ("B03", 10), ("B04", 10), ("B05", 20), ("B06", 20), ("B07", 20),
             ("B08", 10), ("B8A", 20), ("B09", 60), ("B10", 60), ("B11", 20), ("B12", 20), ("TCI", 10)]
L2A_BANDS = {
    10: ["B02", "B03", "B04", "B08", "TCI", "AOT", "WVP"],
    20: ["B01", "B02", "B03", "B04", "B05", "B06", "B07", "B8A", "B11", "B12", "TCI", "AOT", "WVP", "SCL"],
    60: ["B01", "B02", "B03", "B04", "B05", "B06", "B07", "B8A", "B09", "B11", "B12", "TCI", "AOT", "WVP", "SCL"],
}
# The true-color images' tiles (scaled from 256 at 10 m in L1C, and 1024, 2048 and 256 in L2A).
TCI_TILES = {("L1C", 10): 16, ("L2A", 10): 32, ("L2A", 20): 40, ("L2A", 60): 16}
KAKADU_COMS = [b"\x00\x01Kakadu-v7.4",
               b"\x00\x01Kdu-Layer-Info: log_2{Delta-D(squared-error)/Delta-L(bytes)}, L(bytes)\n-192.0,  3.8e+06\n"]


# ---------------------------------------------------------------- band files


def pixels(seed: int, bands: int, size: int, bits: int, classes: int | None = None) -> np.ndarray:
    """A gradient with noise, different for every band file."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:size, 0:size]
    out = []
    for c in range(bands):
        if classes is not None:
            v = (x // 3 + y // 5 + seed + c) % classes
        else:
            v = (seed * 977 + c * 131 + 7 * x + 13 * y + rng.integers(0, 4, (size, size))) % (1 << bits)
        out.append(v)
    return np.array(out, dtype=np.uint16 if bits > 8 else np.uint8)


def opj_jp2(data: np.ndarray, r: int, tile: int, bits: int, precincts: str | None = None) -> bytes:
    """A JP2 file written by OpenJPEG through rasterio (GDAL's JP2OpenJPEG driver)."""
    c, h, w = data.shape
    res = min(5, tile.bit_length() - 1)  # GDAL keeps 2^resolutions within the tile
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "b.jp2"
        with rasterio.open(path, "w", driver="JP2OpenJPEG", width=w, height=h, count=c, dtype=data.dtype,
                           crs="EPSG:32632", transform=from_origin(ULX, ULY, r, r), tiled=True, blockxsize=tile,
                           blockysize=tile, QUALITY=100, REVERSIBLE="YES", RESOLUTIONS=res, NBITS=bits,
                           PRECINCTS=precincts or ",".join(["{256,256}"] * res), PLT="YES", CODEBLOCK_WIDTH=64,
                           CODEBLOCK_HEIGHT=64, GMLJP2="YES", GeoJP2="NO", YCC="NO",
                           BLOCKSIZE_STRICT="YES") as dst:
            dst.write(data)
        return path.read_bytes()


def split_jp2(d: bytes) -> tuple[list[tuple[bytes, bytes]], bytes]:
    """([(box type, box bytes)] before jp2c, the codestream)."""
    boxes, o = [], 0
    while o < len(d):
        lbox, tbox = struct.unpack_from(">I4s", d, o)
        length = struct.unpack_from(">Q", d, o + 8)[0] if lbox == 1 else (len(d) - o if lbox == 0 else lbox)
        hl = 16 if lbox == 1 else 8
        if tbox == b"jp2c":
            assert o + length == len(d)
            return boxes, d[o + hl :]
        boxes.append((tbox, d[o : o + length]))
        o += length
    raise AssertionError("no jp2c")


def segments(cs: bytes) -> tuple[list[tuple[int, bytes]], bytes]:
    """The main header's marker segments after SOC [(marker, segment from its marker)], and the rest
    of the codestream from the first SOT."""
    assert cs[:2] == b"\xff\x4f"
    o, out = 2, []
    while True:
        m = struct.unpack_from(">H", cs, o)[0]
        if m == 0xFF90:
            return out, cs[o:]
        length = struct.unpack_from(">H", cs, o + 2)[0]
        out.append((m, cs[o : o + 2 + length]))
        o += 2 + length


def kakadu(cs: bytes, coms=KAKADU_COMS, extra: list[bytes] = ()) -> bytes:
    """The codestream with Kakadu's main header: Rsiz 0, its two COM markers instead of
    OpenJPEG's, and the `extra` segments before them."""
    segs, tail = segments(cs)
    out = bytearray(b"\xff\x4f")
    for m, seg in segs:
        if m == 0xFF51:
            seg = bytearray(seg)
            seg[4:6] = b"\0\0"
        if m != 0xFF64:
            out += seg
    for seg in extra:
        out += seg
    for c in coms:
        out += struct.pack(">HH", 0xFF64, len(c) + 2) + c
    return bytes(out + tail)


def box(tbox: bytes, body: bytes, xl: bool = False) -> bytes:
    if xl:
        return struct.pack(">I4sQ", 1, tbox, 16 + len(body)) + body
    return struct.pack(">I4s", 8 + len(body), tbox) + body


def band_file(data: np.ndarray, r: int, tile: int, bits: int, *, precincts: str | None = None,
              to_end: bool = True, xl_asoc: bool = False, extra_box: bool = False,
              mutate=None) -> bytes:
    """A band file as Sentinel-2's are: JP2 boxes (signature, file type, header,
    GML-in-JP2), then the jp2c box, LBox 0 (to the end) or its length."""
    boxes, cs = split_jp2(opj_jp2(data, r, tile, bits, precincts))
    cs = kakadu(cs)
    if mutate is not None:
        cs = mutate(cs)
    head = b""
    for tbox, b in boxes:
        if tbox == b"rreq":
            continue  # Kakadu's files have none
        if tbox == b"asoc" and xl_asoc:
            b = box(b"asoc", b[8:], xl=True)
        head += b
    if extra_box:
        head += box(b"xml ", b"<?xml version=\"1.0\"?><Kakadu_metadata/>")
    return head + (struct.pack(">I4s", 0, b"jp2c") if to_end else struct.pack(">I4s", 8 + len(cs), b"jp2c")) + cs


# ---------------------------------------------------------------- XML documents


def spectral_xml(response: bool = True, skip: set = frozenset()) -> str:
    out = []
    for i, pb, r, lo, hi, mid, _, _ in SPECTRAL:
        if pb in skip:
            continue
        resp = ("\n          <Spectral_Response>\n            <STEP unit=\"nm\">1</STEP>\n"
                f"            <VALUES>0.0062 0.01042 0.01615 0.02308 0.0277 0.03234</VALUES>\n"
                "          </Spectral_Response>") if response else ""
        out.append(f"""        <Spectral_Information bandId="{i}" physicalBand="{pb}">
          <RESOLUTION>{r}</RESOLUTION>
          <Wavelength>
            <MIN unit="nm">{lo}</MIN>
            <MAX unit="nm">{hi}</MAX>
            <CENTRAL unit="nm">{mid}</CENTRAL>
          </Wavelength>{resp}
        </Spectral_Information>""")
    return "\n".join(out)


def product_xml(level: str, uri: str, baseline: str, granule: str, files: list[str], *, offsets: bool = True,
                gaps: bool = False, extra_granule: str = "", root: str | None = None, prolog: str = "") -> str:
    l2a = level == "L2A"
    root = root or ("Level-2A_User_Product" if l2a else "Level-1C_User_Product")
    xsd = "User_Product_Level-2A.xsd" if l2a else "User_Product_Level-1C.xsd"
    images = "\n".join(f"                        <IMAGE_FILE>{f}</IMAGE_FILE>" for f in files)
    if l2a:
        quant = """            <QUANTIFICATION_VALUES_LIST>
                <BOA_QUANTIFICATION_VALUE unit="none">10000</BOA_QUANTIFICATION_VALUE>
                <AOT_QUANTIFICATION_VALUE unit="none">1000.0</AOT_QUANTIFICATION_VALUE>
                <WVP_QUANTIFICATION_VALUE unit="cm">1000.0</WVP_QUANTIFICATION_VALUE>
            </QUANTIFICATION_VALUES_LIST>"""
        offs = ("            <BOA_ADD_OFFSET_VALUES_LIST>\n" + "\n".join(
            f'        <BOA_ADD_OFFSET band_id="{i}">{"-1000" if not (gaps and i == 3) else "minus one thousand"}</BOA_ADD_OFFSET>'
            for i in range(13)) + "\n      </BOA_ADD_OFFSET_VALUES_LIST>") if offsets else ""
    else:
        quant = '            <QUANTIFICATION_VALUE unit="none">10000</QUANTIFICATION_VALUE>'
        offs = ("            <Radiometric_Offset_List>\n" + "\n".join(
            f'        <RADIO_ADD_OFFSET band_id="{i}">{"-1000" if not (gaps and i == 3) else "n/a"}</RADIO_ADD_OFFSET>'
            for i in range(13)) + "\n      </Radiometric_Offset_List>") if offsets else ""
    irr = "\n".join(f'          <SOLAR_IRRADIANCE bandId="{i}" unit="W/m²/µm">{s}</SOLAR_IRRADIANCE>'
                    for i, *_, s in SPECTRAL)
    if gaps:  # a repeated irradiance makes the band's member absent
        irr += '\n          <SOLAR_IRRADIANCE bandId="1" unit="W/m²/µm">1959.76</SOLAR_IRRADIANCE>'
    gains = "\n".join(f'            <PHYSICAL_GAINS bandId="{i}">{g}</PHYSICAL_GAINS>' for i, *_, g, _ in SPECTRAL)
    scene = ""
    if l2a:
        scene = "            <Scene_Classification_List>\n" + "\n".join(
            f"""                <Scene_Classification_ID>
                    <SCENE_CLASSIFICATION_TEXT>{t}</SCENE_CLASSIFICATION_TEXT>
                    <SCENE_CLASSIFICATION_INDEX>{i}</SCENE_CLASSIFICATION_INDEX>
                </Scene_Classification_ID>""" for i, t in enumerate(SCENE)) + "\n            </Scene_Classification_List>"
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="no"?>
{prolog}<n1:{root} xmlns:n1="https://psd-14.sentinel2.eo.esa.int/PSD/{xsd}" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="https://psd-14.sentinel2.eo.esa.int/PSD/{xsd}">
    <n1:General_Info>
        <Product_Info>
            <PRODUCT_START_TIME>2024-01-03T10:13:29.024Z</PRODUCT_START_TIME>
            <PRODUCT_STOP_TIME>2024-01-03T10:13:29.024Z</PRODUCT_STOP_TIME>
            <PRODUCT_URI>{uri}.SAFE</PRODUCT_URI>
            <PROCESSING_LEVEL>Level-{level[1:]}</PROCESSING_LEVEL>
            <PRODUCT_TYPE>S2MSI{level[1:].replace("C", "C").replace("2A", "2A")}</PRODUCT_TYPE>
            <PROCESSING_BASELINE>{baseline}</PROCESSING_BASELINE>
            <GENERATION_TIME>2024-01-03T11:03:04.000000Z</GENERATION_TIME>
            <Datatake datatakeIdentifier="GS2B_20240103T101329_035655_N{baseline}">
      <SPACECRAFT_NAME>Sentinel-2B</SPACECRAFT_NAME>
      <DATATAKE_TYPE>INS-NOBS</DATATAKE_TYPE>
      <SENSING_ORBIT_NUMBER>22</SENSING_ORBIT_NUMBER>
      <SENSING_ORBIT_DIRECTION>DESCENDING</SENSING_ORBIT_DIRECTION>
    </Datatake>
<Query_Options completeSingleTile="true">
<PRODUCT_FORMAT>SAFE_COMPACT</PRODUCT_FORMAT>
</Query_Options>
<Product_Organisation>
                <Granule_List>
                    <Granule datastripIdentifier="S2B_OPER_MSI_{level}_DS_2BPS" granuleIdentifier="S2B_OPER_MSI_{level}_TL_2BPS_{granule}" imageFormat="JPEG2000">
{images}
                    </Granule>{extra_granule}
                </Granule_List>
            </Product_Organisation>
        </Product_Info>
        <Product_Image_Characteristics>
            <Special_Values>
                <SPECIAL_VALUE_TEXT>NODATA</SPECIAL_VALUE_TEXT>
                <SPECIAL_VALUE_INDEX>0</SPECIAL_VALUE_INDEX>
            </Special_Values>
            <Special_Values>
                <SPECIAL_VALUE_TEXT>SATURATED</SPECIAL_VALUE_TEXT>
                <SPECIAL_VALUE_INDEX>65535</SPECIAL_VALUE_INDEX>
            </Special_Values>
            <Image_Display_Order>
                <RED_CHANNEL>3</RED_CHANNEL>
                <GREEN_CHANNEL>2</GREEN_CHANNEL>
                <BLUE_CHANNEL>1</BLUE_CHANNEL>
            </Image_Display_Order>
{quant}
{offs}
            <Reflectance_Conversion>
        <U>1.03421885250175</U>
        <Solar_Irradiance_List>
{irr}
        </Solar_Irradiance_List>
      </Reflectance_Conversion>
            <Spectral_Information_List>
{spectral_xml(skip={"B3"} if gaps else frozenset())}
      </Spectral_Information_List>
{gains}
            <REFERENCE_BAND>3</REFERENCE_BAND>
{scene}
        </Product_Image_Characteristics>
        <Product_Image_Characteristics_Comment>&lt;synthetic&gt; &amp; scaled down</Product_Image_Characteristics_Comment>
    </n1:General_Info>
    <!-- trimmed: Geometric_Info, Auxiliary_Data_Info, Quality_Indicators_Info -->
    <n1:Geometric_Info>
        <Coordinate_Reference_System>
            <GEO_TABLES version="1">EPSG</GEO_TABLES>
            <HORIZONTAL_CS_TYPE>GEOGRAPHIC</HORIZONTAL_CS_TYPE>
        </Coordinate_Reference_System>
    </n1:Geometric_Info>
</n1:{root}>
"""


def geocoding_xml(sizes: dict[int, int], *, code: str = "EPSG:32632", geo: dict | None = None,
                  extra: str = "") -> str:
    """The Tile_Geocoding element: a Size and a Geoposition per resolution."""
    out = [f'    <Tile_Geocoding metadataLevel="Brief">',
           f"      <HORIZONTAL_CS_NAME>WGS84 / UTM zone 32N</HORIZONTAL_CS_NAME>",
           f"      <HORIZONTAL_CS_CODE>{code}</HORIZONTAL_CS_CODE>"]
    for r, n in sizes.items():
        out.append(f'      <Size resolution="{r}">\n        <NROWS>{n}</NROWS>\n        <NCOLS>{n}</NCOLS>\n      </Size>')
    for r in sizes:
        ulx, uly, xd, yd = (geo or {}).get(r, (ULX, ULY, r, -r))
        if xd is None:
            continue
        out.append(f'      <Geoposition resolution="{r}">\n        <ULX>{ulx}</ULX>\n        <ULY>{uly}</ULY>\n'
                   f"        <XDIM>{xd}</XDIM>\n        <YDIM>{yd}</YDIM>\n      </Geoposition>")
    return "\n".join(out) + extra + "\n    </Tile_Geocoding>"


def tile_xml(level: str, baseline: str, sizes: dict[int, int], *, pad: int = 70000, **kw) -> str:
    """The tile metadata, with the sun angle grids that make the real ones 190–630 KB
    (`pad` bytes of them, so that it is an array)."""
    root = f"Level-{level[1:]}_Tile_ID"
    rng = np.random.default_rng(len(level) + len(baseline))
    rows = []
    total = 0
    while total < pad:
        row = " ".join(f"{v:.4f}" for v in 71 + rng.random(23))
        rows.append(f"            <VALUES>{row}</VALUES>")
        total += len(rows[-1]) + 1
    values = "\n".join(rows)
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<n1:{root} xmlns:n1="https://psd-14.sentinel2.eo.esa.int/PSD/S2_PDI_{root.replace("_Tile_ID", "")}_Tile_Metadata.xsd" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <n1:General_Info>
    <TILE_ID metadataLevel="Brief">S2B_OPER_MSI_{level}_TL_2BPS_20240103T110304_A035655_T32TNS_N{baseline}</TILE_ID>
    <DATASTRIP_ID metadataLevel="Standard">S2B_OPER_MSI_{level}_DS_2BPS_20240103T110304_S20240103T101328_N{baseline}</DATASTRIP_ID>
    <DOWNLINK_PRIORITY metadataLevel="Standard">NOMINAL</DOWNLINK_PRIORITY>
    <SENSING_TIME metadataLevel="Standard">2024-01-03T10:18:03.604815Z</SENSING_TIME>
  </n1:General_Info>
  <n1:Geometric_Info>
{geocoding_xml(sizes, **kw)}
    <Tile_Angles metadataLevel="Standard">
      <Sun_Angles_Grid>
        <Zenith>
          <COL_STEP unit="m">5000</COL_STEP>
          <ROW_STEP unit="m">5000</ROW_STEP>
          <Values_List>
{values}
          </Values_List>
        </Zenith>
      </Sun_Angles_Grid>
    </Tile_Angles>
  </n1:Geometric_Info>
</n1:{root}>
"""


def manifest_xml(level: str, files: dict[str, bytes]) -> str:
    """The SAFE manifest: an XFDU with a data object per file, its size and MD5."""
    objs = []
    for i, (key, data) in enumerate(sorted(files.items())):
        objs.append(f"""		<dataObject ID="obj_{i}">
			<byteStream mimeType="application/octet-stream" size="{len(data)}">
				<fileLocation href="./{key}" locatorType="URL"/>
				<checksum checksumName="MD5">{hashlib.md5(data).hexdigest()}</checksum>
			</byteStream>
		</dataObject>""")
    body = "\n".join(objs)
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<xfdu:XFDU xmlns:gml="http://www.opengis.net/gml" xmlns:safe="http://www.esa.int/safe/sentinel/1.1" xmlns:xfdu="urn:ccsds:schema:xfdu:1" version="esa/safe/sentinel/1.1/sentinel-2/msi/archive_{level.lower()}_user_product">	<!-- ===================================================================      INFORMATION PACKAGE MAP SECTION      =================================================================== -->	<informationPackageMap>		<xfdu:contentUnit textInfo="SENTINEL-2 MSI {level} User Product" unitType="Product_{level}"/>
	</informationPackageMap>
	<dataObjectSection>
{body}
	</dataObjectSection>
</xfdu:XFDU>
"""


def report_xml(name: str, size: int, seed: int) -> str:
    """A quality report of about `size` bytes."""
    rng = np.random.default_rng(seed)
    checks = []
    total = 0
    while total < size:
        c = (f'      <check><inspection creation="2024-01-03T11:00:00Z" item="{name}" name="{name}_{len(checks)}" '
             f'processingStatus="done"/><message contentType="text/plain">Check passed, value '
             f'{rng.integers(0, 10**6)}</message></check>')
        checks.append(c)
        total += len(c) + 1
    joined = "\n".join(checks)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Earth_Explorer_File xmlns="http://gs2.esa.int/DATA_STRUCTURE/olqcReport">
  <Data_Block type="xml">
    <report date="2024-01-03T11:00:00Z" gippVersion="1.0" globalStatus="PASSED">
{joined}
    </report>
  </Data_Block>
</Earth_Explorer_File>
"""


XSD = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" elementFormDefault="qualified">
  <xs:element name="{0}" type="xs:anyType"/>
</xs:schema>
"""
INSPIRE = """<?xml version="1.0" encoding="UTF-8"?>
<gmd:MD_Metadata xmlns:gmd="http://www.isotc211.org/2005/gmd" xmlns:gco="http://www.isotc211.org/2005/gco">
  <gmd:fileIdentifier><gco:CharacterString>{0}</gco:CharacterString></gmd:fileIdentifier>
  <gmd:language><gmd:LanguageCode codeList="http://www.loc.gov/standards/iso639-2/" codeListValue="eng">eng</gmd:LanguageCode></gmd:language>
</gmd:MD_Metadata>
"""


# ---------------------------------------------------------------- products


def mask_jp2(seed: int, comps: int, r: int = 60) -> bytes:
    return band_file(pixels(seed, comps, SIZES[r], 8, classes=2), r, 8 if r == 60 else 16, 8)


def bands_l1c(only=None) -> list[tuple[str, int, int, int, int]]:
    """(name, resolution, components, bits, tile) of the L1C band files."""
    out = []
    for name, r in L1C_BANDS:
        if only is not None and name not in only:
            continue
        tci = name == "TCI"
        out.append((name, r, 3 if tci else 1, 8 if tci else 15, TCI_TILES[("L1C", r)] if tci else TILES[r]))
    return out


def bands_l2a(skip=frozenset()) -> list[tuple[str, int, int, int, int]]:
    out = []
    for r, names in L2A_BANDS.items():
        for name in names:
            if (name, r) in skip:
                continue
            tci, scl = name == "TCI", name == "SCL"
            bits = 8 if tci or scl else 15
            out.append((name, r, 3 if tci else 1, bits, TCI_TILES[("L2A", r)] if tci else TILES[r]))
    return out


def product(level: str, *, baseline: str = "05.10", bands=None, gcs: bool = False, gml_masks: bool = False,
            offsets: bool = True, gaps: bool = False, extras: dict | None = None, mini: bool = False,
            band_kw: dict | None = None) -> dict[str, bytes]:
    """The files of a product, by key."""
    l2a = level == "L2A"
    uri = (PRODUCT_L2A if l2a else PRODUCT_L1C).replace("N0510", f"N{baseline.replace('.', '')}")
    granule, ds = (GRANULE_L2A, DS_L2A) if l2a else (GRANULE_L1C, DS_L1C)
    g = f"GRANULE/{granule}"
    if bands is None:
        bands = bands_l2a() if l2a else bands_l1c()
    files: dict[str, bytes] = {}
    images = []
    for k, (name, r, comps, bits, tile) in enumerate(bands):
        stem = f"{TILE}_{SENSING}_{name}" + (f"_{r}m" if l2a else "")
        f = f"{g}/IMG_DATA/" + (f"R{r}m/" if l2a else "") + stem
        images.append(f)
        seed = 1 + k + (100 if l2a else 0)
        data = pixels(seed, comps, SIZES[r], bits, classes=12 if name == "SCL" else None)
        kw = (band_kw or {}).get(name, {})
        files[f + ".jp2"] = band_file(data, r, tile, bits, **kw)
    sizes = dict(SIZES)
    if not mini:
        qi = f"{g}/QI_DATA"
        if gml_masks:
            for b in ("B01", "B02"):
                for kind in ("CLOUDS", "DETFOO", "DEFECT"):
                    files[f"{qi}/MSK_{kind}_{b}.gml"] = (
                        f'<?xml version="1.0" encoding="UTF-8"?>\n<eop:Mask xmlns:eop="http://www.opengis.net/eop/2.0" '
                        f'gml:id="{kind}_{b}" xmlns:gml="http://www.opengis.net/gml/3.2"><eop:maskMembers/></eop:Mask>\n'
                    ).encode()
        else:
            files[f"{qi}/MSK_CLASSI_B00.jp2"] = mask_jp2(500, 3)
            files[f"{qi}/MSK_DETFOO_B01.jp2"] = mask_jp2(501, 1)
            files[f"{qi}/MSK_QUALIT_B01.jp2"] = mask_jp2(502, 3)
        if l2a:
            files[f"{qi}/MSK_CLDPRB_20m.jp2"] = mask_jp2(503, 1, 20)
            files[f"{qi}/MSK_SNWPRB_60m.jp2"] = mask_jp2(504, 1, 60)
            files[f"{qi}/L2A_QUALITY.xml"] = report_xml("L2A_QUALITY", 5000, 7).encode()
        files[f"{qi}/{TILE}_{SENSING}_PVI.jp2"] = band_file(pixels(505, 3, 32, 8), 320, 16, 8)
        files[f"{qi}/GENERAL_QUALITY.xml"] = report_xml("GENERAL_QUALITY", 4000, 1).encode()
        files[f"{qi}/GEOMETRIC_QUALITY.xml"] = report_xml("GEOMETRIC_QUALITY", 5000, 2).encode()
        files[f"{qi}/FORMAT_CORRECTNESS.xml"] = report_xml("FORMAT_CORRECTNESS", 28000, 3).encode()
        files[f"{qi}/SENSOR_QUALITY.xml"] = report_xml("SENSOR_QUALITY", 4300, 4).encode()
        files[f"{g}/AUX_DATA/AUX_CAMSFO"] = bytes(np.random.default_rng(6).integers(0, 256, 2970, dtype=np.uint8))
        files[f"{g}/AUX_DATA/AUX_ECMWFT"] = bytes(np.random.default_rng(7).integers(0, 256, 1620, dtype=np.uint8))
        dsq = f"DATASTRIP/{ds}/QI_DATA"
        for i, (name, n) in enumerate((("FORMAT_CORRECTNESS", 23000), ("GENERAL_QUALITY", 7700),
                                        ("GEOMETRIC_QUALITY", 8900), ("RADIOMETRIC_QUALITY", 7100),
                                        ("SENSOR_QUALITY", 4700))):
            files[f"{dsq}/{name}.xml"] = report_xml(name, n, 10 + i).encode()
        files[f"DATASTRIP/{ds}/MTD_DS.xml"] = report_xml("MTD_DS", 90000, 20).encode()
        files["INSPIRE.xml"] = INSPIRE.format(uri).encode()
        lv = f"Level-{level[1:]}"
        for x in (f"S2_User_Product_{lv}_Metadata", f"S2_PDI_{lv}_Tile_Metadata", f"S2_PDI_{lv}_Datastrip_Metadata"):
            files[f"rep_info/{x}.xsd"] = XSD.format(x).encode()
        files["HTML/UserProduct_index.html"] = b"<html><body>Sentinel-2 product</body></html>\n"
        files["HTML/UserProduct_index.xsl"] = b'<?xml version="1.0"?>\n<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform"/>\n'
        files["HTML/star_bg.jpg"] = bytes(np.random.default_rng(8).integers(0, 256, 900, dtype=np.uint8))
        files[f"{g}/AUX_DATA/AUX_EMPTY"] = b""
    files[f"{g}/MTD_TL.xml"] = tile_xml(level, baseline, sizes, pad=2000 if mini else 70000).encode()
    mtd = "MTD_MSIL2A.xml" if l2a else "MTD_MSIL1C.xml"
    files[mtd] = product_xml(level, uri, baseline, granule, images, offsets=offsets, gaps=gaps).encode()
    if gcs:
        files[f"{uri}-ql.jpg"] = bytes(np.random.default_rng(9).integers(0, 256, 1100, dtype=np.uint8))
    files.update(extras or {})
    files["manifest.safe"] = manifest_xml(level, {k: v for k, v in files.items() if not k.endswith("$")}).encode()
    if gcs:  # the mirror's folder markers, one per directory
        dirs = {k[:i] for k in files for i in range(len(k)) if k[i] == "/"}
        for d in dirs:
            files[f"{d}_$folder$"] = b"folder"
    return files


def write_dir(name: str, files: dict[str, bytes]) -> None:
    d = OUT / name
    for key, data in files.items():
        p = d / key
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


# ---------------------------------------------------------------- zip files


def dos_time() -> tuple[int, int]:
    return (10 << 11) | (13 << 5) | 14, ((2024 - 1980) << 9) | (1 << 5) | 3


def write_zip(path: Path, root: str, files: dict[str, bytes], *, deflate=lambda k: False, dirs: bool = True,
              zip64: bool = False, method_of=None, flags_of=None, crc_of=None, names=None, cut: int = 0,
              count_extra: int = 0, trailing_comment: bytes = b"", aliases=(), us_of=None) -> None:
    """A zip file of `files` under `root/`, written as ESA writes SAFE zips (stored, a
    directory entry per directory, local extra fields other than the central ones),
    with ZIP64 end records and extra fields when `zip64`. `aliases` are (name, key) pairs: more
    central entries for the local entry of `key`; `us_of(key, us)` gives a central entry's size."""
    entries = []
    if dirs:
        for d in sorted({k[: i + 1] for k in files for i in range(len(k)) if k[i] == "/"}):
            entries.append((f"{root}/{d}", b"", True))
        entries.insert(0, (f"{root}/", b"", True))
    entries += [(f"{root}/{k}", v, False) for k, v in sorted(files.items())]
    if names is not None:
        entries = names(entries)
    buf = io.BytesIO()
    central = []
    t, dt = dos_time()
    for name, data, is_dir in entries:
        key = name.partition("/")[2]
        method = 8 if deflate(key) and data else 0
        if method_of is not None:
            method = method_of(key, method)
        body = zlib.compress(data, 9)[2:-4] if method == 8 else data
        if method == 12:
            import bz2
            body = bz2.compress(data)
        crc = zlib.crc32(data) if crc_of is None else crc_of(key, zlib.crc32(data))
        flags = 0 if flags_of is None else flags_of(key)
        lho = buf.tell()
        local_extra = struct.pack("<HHBI", 0x5455, 5, 1, 1704280409) + struct.pack("<HHBHBI", 0x7875, 11, 1, 4, 0, 0)[:15]
        raw_name = name.encode()
        buf.write(struct.pack("<4sHHHHHIIIHH", b"PK\x03\x04", 20, flags, method, t, dt, crc,
                              0xFFFFFFFF if zip64 else len(body), 0xFFFFFFFF if zip64 else len(data),
                              len(raw_name), len(local_extra) + (20 if zip64 else 0)))
        buf.write(raw_name + local_extra)
        if zip64:
            buf.write(struct.pack("<HHQQ", 1, 16, len(data), len(body)))
        buf.write(body)
        us = len(data) if us_of is None else us_of(key, len(data))
        central.append((raw_name, flags, method, crc, len(body), us, lho, is_dir))
    for alias, key in aliases:
        central.append((f"{root}/{alias}".encode(), *next(c[1:] for c in central if c[0] == f"{root}/{key}".encode())))
    cd_offset = buf.tell()
    for raw_name, flags, method, crc, cs, us, lho, is_dir in central:
        extra = struct.pack("<HHBI", 0x5455, 5, 1, 1704280409)
        f_us, f_cs, f_lho = us, cs, lho
        if zip64:
            extra = struct.pack("<HHQQQ", 1, 24, us, cs, lho) + extra
            f_us = f_cs = f_lho = 0xFFFFFFFF
        buf.write(struct.pack("<4sHHHHHHIIIHHHHHII", b"PK\x01\x02", 0x031E, 20, flags, method, t, dt, crc,
                              f_cs, f_us, len(raw_name), len(extra), 0, 0, 0, (0o40755 if is_dir else 0o100644) << 16,
                              f_lho))
        buf.write(raw_name + extra)
    cd_size = buf.tell() - cd_offset
    count = len(central) + count_extra
    if zip64:
        rec = buf.tell()
        buf.write(struct.pack("<4sQHHIIQQQQ", b"PK\x06\x06", 44, 45, 45, 0, 0, count, count, cd_size, cd_offset))
        buf.write(struct.pack("<4sIQI", b"PK\x06\x07", 0, rec, 1))
        buf.write(struct.pack("<4sHHHHIIH", b"PK\x05\x06", 0, 0, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF,
                              len(trailing_comment)))
    else:
        buf.write(struct.pack("<4sHHHHIIH", b"PK\x05\x06", 0, 0, count, count, cd_size, cd_offset,
                              len(trailing_comment)))
    buf.write(trailing_comment)
    data = buf.getvalue()
    path.write_bytes(data[: len(data) - cut] if cut else data)


# ---------------------------------------------------------------- codestream mutations (rejections)


def _mutate_siz(f):
    def m(cs: bytes) -> bytes:
        segs, tail = segments(cs)
        out = bytearray(b"\xff\x4f")
        for marker, seg in segs:
            out += f(bytearray(seg)) if marker == 0xFF51 else seg
        return bytes(out + tail)
    return m


def _mutate_cod(f):
    def m(cs: bytes) -> bytes:
        segs, tail = segments(cs)
        out = bytearray(b"\xff\x4f")
        for marker, seg in segs:
            out += f(bytearray(seg)) if marker == 0xFF52 else seg
        return bytes(out + tail)
    return m


def _insert(seg: bytes):
    def m(cs: bytes) -> bytes:
        segs, tail = segments(cs)
        out = bytearray(b"\xff\x4f")
        for marker, s in segs:
            out += s
        return bytes(out + seg + tail)
    return m


def _tile_parts(cs: bytes) -> tuple[bytes, list[bytearray]]:
    segs, tail = segments(cs)
    head = b"\xff\x4f" + b"".join(s for _, s in segs)
    parts, o = [], 0
    while tail[o : o + 2] == b"\xff\x90":
        psot = struct.unpack_from(">I", tail, o + 6)[0]
        parts.append(bytearray(tail[o : o + psot]))
        o += psot
    assert tail[o:] == b"\xff\xd9"
    return head, parts


def _mutate_parts(f):
    def m(cs: bytes) -> bytes:
        head, parts = _tile_parts(cs)
        return f(head, parts)
    return m


def _set(at: int, fmt: str, *v):
    def f(seg: bytearray) -> bytearray:
        struct.pack_into(fmt, seg, at, *v)
        return seg
    return f


def _two_components(seg: bytearray) -> bytearray:
    seg = seg + seg[-3:]
    struct.pack_into(">HH", seg, 2, len(seg) - 2, 0)  # Lsiz, Rsiz
    struct.pack_into(">H", seg, 38, 2)
    return seg


def _psot_zero(head, parts):
    struct.pack_into(">I", parts[-1], 6, 0)
    return head + b"".join(parts) + b"\xff\xd9"


def _two_tile_parts(head, parts):
    struct.pack_into(">BB", parts[0], 10, 0, 2)
    return head + b"".join(parts) + b"\xff\xd9"


def _out_of_order(head, parts):
    parts[0], parts[1] = parts[1], parts[0]
    return head + b"".join(parts) + b"\xff\xd9"


MINI_BANDS = [("B01", 60, 1, 15, 6)]


def mini(level: str = "L1C", bands=None, **kw) -> dict[str, bytes]:
    """The smallest product: one band file at 60 m."""
    return product(level, bands=bands or MINI_BANDS, mini=True, **kw)


def mini_band(**kw) -> bytes:
    return band_file(pixels(1, 1, 19, 15), 60, 6, 15, **kw)


B01_KEY = f"GRANULE/{GRANULE_L1C}/IMG_DATA/{TILE}_{SENSING}_B01.jp2"
TL_KEY = f"GRANULE/{GRANULE_L1C}/MTD_TL.xml"


def with_band(**kw) -> dict[str, bytes]:
    files = mini()
    files[B01_KEY] = mini_band(**kw)
    return files


def with_mtd(**kw) -> dict[str, bytes]:
    files = mini()
    files["MTD_MSIL1C.xml"] = product_xml("L1C", PRODUCT_L1C, "05.10", GRANULE_L1C,
                                          [B01_KEY[:-4]], **kw).encode()
    return files


def with_tile(**kw) -> dict[str, bytes]:
    files = mini()
    files[TL_KEY] = tile_xml("L1C", "05.10", kw.pop("sizes", SIZES), pad=2000, **kw).encode()
    return files


def rejections() -> dict[str, dict[str, bytes]]:
    r: dict[str, dict[str, bytes]] = {}
    old = mini()
    old["S2A_OPER_MTD_SAFL1C_PDMC_20160607T154053_R062_V20150627T102531_20150627T102531.xml"] = old.pop("MTD_MSIL1C.xml")
    r["old_format"] = old
    two = mini()
    two["MTD_MSIL2A.xml"] = two["MTD_MSIL1C.xml"].replace(b"Level-1C_User_Product", b"Level-2A_User_Product")
    r["two_mtd"] = two
    nm = mini()
    del nm["manifest.safe"]
    r["no_manifest"] = nm
    r["wrong_root_element"] = with_mtd(root="Level-2A_User_Product")
    r["two_granules"] = with_mtd(extra_granule='\n                    <Granule granuleIdentifier="x"><IMAGE_FILE>'
                                               f"{B01_KEY[:-4]}</IMAGE_FILE></Granule>")
    no_image = mini()
    no_image["MTD_MSIL1C.xml"] = product_xml("L1C", PRODUCT_L1C, "05.10", GRANULE_L1C, []).encode()
    r["no_image_file"] = no_image
    missing = mini()
    missing["MTD_MSIL1C.xml"] = product_xml("L1C", PRODUCT_L1C, "05.10", GRANULE_L1C,
                                            [B01_KEY[:-4], B01_KEY[:-7] + "B02"]).encode()
    r["missing_band_file"] = missing
    mixed = mini()
    other = B01_KEY.replace(GRANULE_L1C, GRANULE_L1C + "X")[:-7] + "B09.jp2"
    mixed[other] = mini_band()
    mixed["MTD_MSIL1C.xml"] = product_xml("L1C", PRODUCT_L1C, "05.10", GRANULE_L1C, [B01_KEY[:-4], other[:-4]]).encode()
    r["mixed_granule_dirs"] = mixed
    image_form = mini()
    image_form["IMG_DATA/B01.jp2"] = mini_band()
    image_form["MTD_MSIL1C.xml"] = product_xml("L1C", PRODUCT_L1C, "05.10", GRANULE_L1C, ["IMG_DATA/B01"]).encode()
    r["image_file_form"] = image_form
    no_tl = mini()
    del no_tl[TL_KEY]
    r["no_tile_metadata"] = no_tl
    r["bad_cs_code"] = with_tile(code="WGS84 / UTM 32N")
    r["no_geoposition"] = with_tile(geo={60: (ULX, ULY, None, None)})
    r["zero_xdim"] = with_tile(geo={60: (ULX, ULY, 0, -60)})
    r["infinite_ulx"] = with_tile(geo={60: ("1e999", ULY, 60, -60)})
    r["duplicate_resolution"] = with_tile(extra='\n      <Size resolution="60">\n        <NROWS>19</NROWS>\n'
                                                '        <NCOLS>19</NCOLS>\n      </Size>')
    r["size_matches_no_resolution"] = with_tile(sizes={10: 110, 20: 55, 60: 20})
    r["size_matches_two_resolutions"] = with_tile(sizes={10: 110, 20: 19, 60: 19})
    dup = mini()
    second = B01_KEY[:-7] + "X_B01.jp2"
    dup[second] = mini_band()
    dup["MTD_MSIL1C.xml"] = product_xml("L1C", PRODUCT_L1C, "05.10", GRANULE_L1C, [B01_KEY[:-4], second[:-4]]).encode()
    r["duplicate_band_name"] = dup
    bad = mini()
    bad_key = B01_KEY[:-7] + "B-1.jp2"
    bad[bad_key] = bad.pop(B01_KEY)
    bad["MTD_MSIL1C.xml"] = product_xml("L1C", PRODUCT_L1C, "05.10", GRANULE_L1C, [bad_key[:-4]]).encode()
    r["bad_band_name"] = bad
    raw = mini()
    raw[B01_KEY] = split_jp2(raw[B01_KEY])[1]
    r["not_jp2"] = raw
    r["jp2c_not_last"] = with_band(to_end=False)
    r["jp2c_not_last"][B01_KEY] += box(b"xml ", b"<after/>")
    beyond = mini()
    beyond[B01_KEY] = beyond[B01_KEY][:12] + struct.pack(">I", 10**6) + beyond[B01_KEY][16:]
    r["box_beyond_file"] = beyond
    r["tlm"] = with_band(mutate=_insert(struct.pack(">HHBB", 0xFF55, 4, 0, 0)))
    r["ppm"] = with_band(mutate=_insert(struct.pack(">HHB", 0xFF60, 3, 0)))
    r["sop"] = with_band(mutate=_mutate_cod(_set(4, ">B", 3)))
    r["eph"] = with_band(mutate=_mutate_cod(_set(4, ">B", 5)))
    r["two_tile_parts"] = with_band(mutate=_mutate_parts(_two_tile_parts))
    r["tile_parts_out_of_order"] = with_band(mutate=_mutate_parts(_out_of_order))
    r["psot_zero"] = with_band(mutate=_mutate_parts(_psot_zero))
    r["no_eoc"] = with_band(mutate=lambda cs: cs[:-2])
    r["bytes_after_eoc"] = with_band(mutate=lambda cs: cs + b"\0\0")
    r["tile_origin"] = with_band(mutate=_mutate_siz(_set(30, ">I", 1)))
    r["signed"] = with_band(mutate=_mutate_siz(_set(40, ">B", 0x8E)))
    r["subsampled"] = with_band(mutate=_mutate_siz(_set(41, ">B", 2)))
    r["precision_17"] = with_band(mutate=_mutate_siz(_set(40, ">B", 16)))
    r["two_components"] = with_band(mutate=_mutate_siz(_two_components))
    rgb = mini(bands=[("TCI", 60, 3, 8, 6)])
    key = [k for k in rgb if k.endswith("TCI.jp2")][0]
    rgb[key] = band_file(pixels(3, 3, 19, 8), 60, 6, 8, mutate=_mutate_siz(_set(43, ">B", 6)))
    r["mixed_precision"] = rgb
    r["xml_doctype"] = with_mtd(prolog="<!DOCTYPE n1:Level-1C_User_Product>\n")
    latin = mini()
    latin["MTD_MSIL1C.xml"] = latin["MTD_MSIL1C.xml"].replace(b"scaled down", b"r\xe9duit")
    r["xml_not_utf8_metadata"] = latin
    unclosed = mini()
    unclosed[TL_KEY] = unclosed[TL_KEY].replace(b"</n1:Level-1C_Tile_ID>", b"")
    r["xml_unclosed_tile_metadata"] = unclosed
    return {f"safe_reject_{k}": v for k, v in r.items()}


def zip_rejections() -> dict[str, dict]:
    """Zip files that the profile rejects (§12.4): name -> write_zip keywords."""
    base = mini()
    root = PRODUCT_L1C + ".SAFE"
    mask = f"GRANULE/{GRANULE_L1C}/QI_DATA/MSK_CLASSI_B00.jp2"
    masked = dict(base)
    masked[mask] = mask_jp2(500, 3)
    return {
        "zip_encrypted": dict(files=base, flags_of=lambda k: 1 if k == "manifest.safe" else 0),
        "zip_deflated_band": dict(files=base, deflate=lambda k: k.endswith(".jp2")),
        "zip_deflated_mask": dict(files=masked, deflate=lambda k: k == mask),
        "zip_bzip2": dict(files=base, method_of=lambda k, m: 12 if k == "MTD_MSIL1C.xml" else m),
        "zip_bad_crc": dict(files=base, deflate=lambda k: k.endswith(".xml"),
                            crc_of=lambda k, c: c ^ 1 if k == "MTD_MSIL1C.xml" else c),
        "zip_two_roots": dict(files=base, names=lambda es: es + [("OTHER.SAFE/extra.txt", b"x", False)]),
        "zip_not_safe_root": dict(files=base, root="S2B_MSIL1C_product"),
        "zip_duplicate_name": dict(files=base, names=lambda es: es + [es[-1]]),
        "zip_dotdot": dict(files=base, names=lambda es: es + [(f"{root}/GRANULE/../x.txt", b"x", False)]),
        "zip_truncated_cd": dict(files=base, count_extra=1),
        "zip_no_eocd": dict(files=base, cut=22),
        "zip_comment_misaligned": dict(files=base, trailing_comment=b"comment", cut=1),
    }


# ---------------------------------------------------------------- appended products
# (written after the others, so that those stay byte for byte as they were)

PSD142 = [("Product_Info", "L2A_Product_Info"), ("Product_Organisation", "L2A_Product_Organisation"),
          ("IMAGE_FILE", "IMAGE_FILE_2A"), ("PRODUCT_URI", "PRODUCT_URI_2A"),
          ("Product_Image_Characteristics", "L2A_Product_Image_Characteristics"),
          ("QUANTIFICATION_VALUES_LIST", "L1C_L2A_Quantification_Values_List"),
          ("BOA_QUANTIFICATION_VALUE", "L2A_BOA_QUANTIFICATION_VALUE"),
          ("AOT_QUANTIFICATION_VALUE", "L2A_AOT_QUANTIFICATION_VALUE"),
          ("WVP_QUANTIFICATION_VALUE", "L2A_WVP_QUANTIFICATION_VALUE"),
          ("Scene_Classification_List", "L2A_Scene_Classification_List"),
          ("Scene_Classification_ID", "L2A_Scene_Classification_ID"),
          ("SCENE_CLASSIFICATION_TEXT", "L2A_SCENE_CLASSIFICATION_TEXT"),
          ("SCENE_CLASSIFICATION_INDEX", "L2A_SCENE_CLASSIFICATION_INDEX")]


def split_granules(xml: str, extra: list[str] = ()) -> str:
    """The product metadata of processing baselines 02.04–02.07: a Granule_List per
    resolution, each with the one granule and that resolution's image files (and `extra`,
    image files outside IMG_DATA, in the first)."""
    head, rest = xml.split("                <Granule_List>\n", 1)
    granule, rest = rest.split("\n", 1)
    images, rest = rest.split("                    </Granule>", 1)
    rest = rest.split("                </Granule_List>\n", 1)[1]
    lines = images.rstrip("\n").split("\n")
    blocks = []
    for i, r in enumerate((60, 20, 10)):
        mine = [x for x in lines if f"/R{r}m/" in x] + ([f"                        <IMAGE_FILE>{f}</IMAGE_FILE>"
                                                          for f in extra] if i == 0 else [])
        blocks.append("                <Granule_List>\n" + granule + "\n" + "\n".join(mine)
                      + "\n                    </Granule>\n                </Granule_List>\n")
    return head + "".join(blocks) + rest


def psd142(xml: str) -> str:
    """The names of PSD 14.2 (Level-2A, processing baselines 02.04–02.06)."""
    for new, old in PSD142:
        xml = xml.replace(f"<{new}>", f"<{old}>").replace(f"</{new}>", f"</{old}>").replace(f"<{new} ", f"<{old} ")
    return xml.replace("<L2A_Product_Info>", "<L2A_Product_Info>\n            <PRODUCT_URI_1C>S2B_MSIL1C.SAFE</PRODUCT_URI_1C>")


def with_markers(files: dict[str, bytes], empty_dirs: list[str]) -> dict[str, bytes]:
    """The Google Cloud mirror's folder markers, one per directory, and one per empty directory."""
    dirs = {k[:i] for k in files for i in range(len(k)) if k[i] == "/"} | set(empty_dirs)
    return {**files, **{f"{d}_$folder$": b"folder" for d in sorted(dirs)}}


def old_l2a(baseline: str, *, psd: bool) -> dict[str, bytes]:
    """A Level-2A product of baseline 02.04–02.07: three granules, and with `psd` the names of
    PSD 14.2 and its image files outside IMG_DATA (a DEM that the product does not hold, and
    a cloud probability that it holds, an other object)."""
    bands = bands_l2a(skip={("B01", 20)} | {(b, 60) for b in L2A_BANDS[60][2:]})
    files = product("L2A", baseline=baseline, offsets=False, bands=bands)
    g = f"GRANULE/{GRANULE_L2A}"
    extra = [f"{g}/AUX_DATA/{TILE}_{SENSING}_DEM_60m", f"{g}/QI_DATA/{TILE}_{SENSING}_CLD_60m"] if psd else []
    if psd:
        files[f"{g}/QI_DATA/{TILE}_{SENSING}_CLD_60m.jp2"] = mask_jp2(506, 1, 60)
    xml = split_granules(files["MTD_MSIL2A.xml"].decode(), extra)
    files["MTD_MSIL2A.xml"] = (psd142(xml) if psd else xml).encode()
    if psd:
        files[f"{g}/MTD_TL.xml"] = files[f"{g}/MTD_TL.xml"].replace(b"TILE_ID ", b"TILE_ID_2A ").replace(
            b"</TILE_ID>", b"</TILE_ID_2A>")
    files["manifest.safe"] = manifest_xml("L2A", {k: v for k, v in files.items() if k != "manifest.safe"}).encode()
    return files


def many_records() -> dict[str, bytes]:
    """65 Special_Values and 65 scene classes: more than the records copied (conventions §6.2, §6.3)."""
    files = mini("L2A", bands=[("B01", 60, 1, 15, 6), ("SCL", 60, 1, 8, 6)])
    xml = files["MTD_MSIL2A.xml"].decode()
    sv = xml.index("            <Special_Values>")
    xml = xml[:sv] + "".join(f"            <Special_Values><SPECIAL_VALUE_TEXT>V{i}</SPECIAL_VALUE_TEXT>"
                             f"<SPECIAL_VALUE_INDEX>{i}</SPECIAL_VALUE_INDEX></Special_Values>\n"
                             for i in range(63)) + xml[sv:]
    sc = xml.index("                <Scene_Classification_ID>")
    xml = xml[:sc] + "".join(f"                <Scene_Classification_ID><SCENE_CLASSIFICATION_TEXT>C{i}"
                             f"</SCENE_CLASSIFICATION_TEXT><SCENE_CLASSIFICATION_INDEX>{i + 12}"
                             f"</SCENE_CLASSIFICATION_INDEX></Scene_Classification_ID>\n" for i in range(53)) + xml[sc:]
    files["MTD_MSIL2A.xml"] = xml.encode()
    return files


def appended_rejections() -> dict[str, dict[str, bytes]]:
    r: dict[str, dict[str, bytes]] = {}
    deep = mini()
    deep["MTD_MSIL1C.xml"] = deep["MTD_MSIL1C.xml"].replace(
        b"</n1:General_Info>", b"<x>" * 300 + b"</x>" * 300 + b"</n1:General_Info>")
    r["xml_too_deep"] = deep
    # Layers multiply the empty packets: 200 make a corner chunk's empty tiles longer than 4096
    # bytes; 40 keep each chunk's within it, but all of them longer than the band file.
    r["tail_too_long"] = with_band(mutate=_mutate_cod(_set(6, ">H", 200)))
    r["tails_over_band_size"] = with_band(mutate=_mutate_cod(_set(6, ">H", 40)))
    outside = mini()
    outside["MTD_MSIL1C.xml"] = product_xml("L1C", PRODUCT_L1C, "05.10", GRANULE_L1C,
                                            [f"GRANULE/{GRANULE_L1C}/QI_DATA/{TILE}_{SENSING}_CLD_60m"]).encode()
    r["no_band_file"] = outside
    return {f"safe_reject_{k}": v for k, v in r.items()}


def appended_zip_rejections() -> dict[str, dict]:
    base = mini()
    xml = lambda k: k.endswith(".xml") or k == "manifest.safe"  # noqa: E731
    return {
        # Two central entries for one local entry: their bytes overlap.
        "zip_overlapping_entries": dict(files=base, aliases=[("copy_of_MTD.xml", "MTD_MSIL1C.xml")]),
        "zip_deflated_too_large": dict(files=base, deflate=xml,
                                       us_of=lambda k, us: (1 << 26) + 1 if k == "MTD_MSIL1C.xml" else us),
        "zip_inflated_total": dict(files=base, deflate=xml, us_of=lambda k, us: 1 << 26 if xml(k) else us),
    }


def write_appended() -> None:
    p0207 = with_markers(old_l2a("02.07", psd=False), ["AUX_DATA"])
    write_dir("safe_l2a_pb0207", p0207)
    root = PRODUCT_L2A.replace("N0510", "N0207") + ".SAFE"
    write_zip(OUT / "safe_l2a_pb0207.SAFE.zip", root, {k: v for k, v in p0207.items() if not k.endswith("$")},
              names=lambda es: es[:1] + [(f"{root}/AUX_DATA/", b"", True)] + es[1:])
    write_dir("safe_l2a_pb0206", old_l2a("02.06", psd=True))
    write_dir("safe_metadata_many_records", many_records())
    for name, files in appended_rejections().items():
        write_dir(name, files)
    for name, kw in appended_zip_rejections().items():
        files = kw.pop("files")
        write_zip(OUT / f"safe_reject_{name}.SAFE.zip", PRODUCT_L1C + ".SAFE", files, **kw)


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    l1c = product("L1C", gcs=True)
    write_dir("safe_l1c", l1c)
    write_zip(OUT / "safe_l1c.SAFE.zip", PRODUCT_L1C + ".SAFE", {k: v for k, v in l1c.items() if not k.endswith("$")})
    l2a = product("L2A", band_kw={"B02": {"precincts": "{16,16},{16,16},{8,8},{256,256},{256,256}"},
                                  "TCI": {"extra_box": True}})
    write_dir("safe_l2a", l2a)
    write_zip(OUT / "safe_l2a_deflated_xml.SAFE.zip", PRODUCT_L2A + ".SAFE", l2a,
              deflate=lambda k: k.endswith((".xml", ".xsd")) or k == "manifest.safe")
    write_dir("safe_l1c_pb0207", product("L1C", baseline="02.07", gml_masks=True, offsets=False,
                                         bands=bands_l1c(only={"B01", "B02", "B05", "TCI"})))
    write_dir("safe_l2a_pb0212", product("L2A", baseline="02.12", offsets=False,
                                         bands=bands_l2a(skip={("B01", 20)} | {(b, 60) for b in L2A_BANDS[60][2:]})))
    latin = mini()
    latin[f"DATASTRIP/{DS_L1C}/QI_DATA/GENERAL_QUALITY.xml"] = (
        b'<?xml version="1.0" encoding="ISO-8859-1"?>\n<report>Qualit\xe9: bonne</report>\n')
    write_dir("safe_latin1_xml", latin)
    write_dir("safe_metadata_gaps", mini(bands=[("B01", 60, 1, 15, 6), ("B02", 10, 1, 15, 32), ("B03", 10, 1, 15, 32),
                                                ("B04", 10, 1, 15, 32)], gaps=True))
    big = mini(bands=[("B01", 60, 1, 15, 6), ("B05", 20, 1, 15, 20)],
               band_kw={"B01": {"xl_asoc": True, "to_end": True}, "B05": {"to_end": False, "extra_box": True}})
    write_dir("safe_big_header", big)
    write_zip(OUT / "safe_zip64.SAFE.zip", PRODUCT_L1C + ".SAFE", big, zip64=True, dirs=False,
              deflate=lambda k: k == "MTD_MSIL1C.xml")
    for name, files in rejections().items():
        write_dir(name, files)
    for name, kw in zip_rejections().items():
        files = kw.pop("files")
        write_zip(OUT / f"safe_reject_{name}.SAFE.zip", kw.pop("root", PRODUCT_L1C + ".SAFE"), files, **kw)
    write_appended()
    n = sum(1 for _ in OUT.iterdir())
    size = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file())
    print(f"wrote {n} fixtures, {size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()

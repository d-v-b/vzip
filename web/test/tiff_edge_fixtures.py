"""Writes TIFFs to web/test/fixtures/ that exercise the edges of the TIFF
profile (VIRTUALIZE.md §3) which tifffile does not write: odd OME-XML,
duplicate tags, SubIFD mismatches, tiles outside the file, and so on.

They are compared between implementations (conformance/virtualize/
compare.py). `edge_reject_*` files must be rejected; the other `edge_*`
files must be accepted.

Usage: uv run python web/test/tiff_edge_fixtures.py
"""

from __future__ import annotations

import struct
from pathlib import Path

OUT = Path(__file__).parent / "fixtures"

SHORT, LONG, ASCII, UNDEFINED = 3, 4, 2, 7
SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 7: 1, 8: 2, 13: 4, 16: 8, 99: 1}
PACK = {1: "B", 2: "B", 3: "H", 4: "I", 7: "B", 8: "h", 13: "I", 16: "Q", 99: "B"}


class Tiff:
    """A little-endian classic TIFF, written front to back."""

    def __init__(self) -> None:
        self.buf = bytearray(b"II*\0\0\0\0\0")

    def blob(self, data: bytes) -> int:
        if len(self.buf) % 2:
            self.buf += b"\0"
        offset = len(self.buf)
        self.buf += data
        return offset

    def ifd(self, entries: list[tuple[int, int, object]]) -> int:
        """Writes an IFD (entries in the order given) and returns its offset.
        A value is a list of ints, or bytes for ASCII/UNDEFINED."""
        packed = []
        for tag, typ, value in entries:
            if isinstance(value, tuple):  # ("at", count, offset): a value stored anywhere
                packed.append(struct.pack("<HHII", tag, typ, value[1], value[2]))
                continue
            raw = bytes(value) if isinstance(value, (bytes, bytearray)) else struct.pack(
                f"<{len(value)}{PACK[typ]}", *value)
            count = len(raw) // SIZE[typ]
            field = raw.ljust(4, b"\0") if len(raw) <= 4 else struct.pack("<I", self.blob(raw))
            packed.append(struct.pack("<HHI", tag, typ, count) + field)
        offset = self.blob(struct.pack("<H", len(packed)) + b"".join(packed) + b"\0\0\0\0")
        self.next_at = offset + 2 + 12 * len(packed)
        return offset

    def chain(self, offsets: list[int]) -> None:
        """Links IFDs into the main chain."""
        struct.pack_into("<I", self.buf, 4, offsets[0])
        for a, b in zip(offsets, offsets[1:]):
            n = struct.unpack_from("<H", self.buf, a)[0]
            struct.pack_into("<I", self.buf, a + 2 + 12 * n, b)

    def write(self, name: str) -> None:
        (OUT / name).write_bytes(bytes(self.buf))


def image(t: Tiff, width=64, height=64, tile=32, *, spp=1, extra=(), description=None,
          subifds=None, skip=(), tile_offsets=None, value=0) -> int:
    """A tiled uint8 image; returns its IFD's offset."""
    planar = (284, SHORT, [2]) in extra
    tiles = -(-width // tile) * -(-height // tile) * (spp if planar else 1)
    size = tile * tile * (1 if planar else spp)
    offsets = tile_offsets or [t.blob(bytes([value + i]) * size) for i in range(tiles)]
    entries = [
        (256, SHORT, [width]), (257, SHORT, [height]), (258, SHORT, [8] * spp), (259, SHORT, [1]),
        (262, SHORT, [2 if spp == 3 else 1]),
    ]
    if description is not None:
        entries.append((270, description[0], description[1]))
    entries += [(277, SHORT, [spp]), (322, SHORT, [tile]), (323, SHORT, [tile]),
                (324, LONG, offsets), (325, LONG, [size] * len(offsets))]
    entries = [e for e in entries if e[0] not in skip] + list(extra)
    if subifds is not None:
        entries.append((330, LONG, subifds))
    entries = sorted(entries, key=lambda e: e[0])
    return t.ifd(entries)


def ome(pixels: str, inner: str = "", *, image_attrs: str = 'Name="edge"', root: str = "OME") -> bytes:
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<{root} xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06">'
            f'<Image ID="Image:0" {image_attrs}><Pixels ID="Pixels:0" Type="uint8" SizeX="64" SizeY="64" '
            f'{pixels}>{inner}</Pixels></Image></{root.split()[0]}>').encode() + b"\0"


def single(name: str, **kw) -> None:
    t = Tiff()
    t.chain([image(t, **kw)])
    t.write(name)


def planes(name: str, xml: bytes, n: int, **kw) -> None:
    t = Tiff()
    t.chain([image(t, description=(ASCII, xml) if i == 0 else None, value=10 * i, **kw) for i in range(n)])
    t.write(name)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    tz = 'SizeZ="2" SizeC="1" SizeT="1" DimensionOrder="XYZCT"'

    # Accepted.
    # A prefixed root, a commented-out Pixels before the real one, an empty
    # name, an invalid and a zero physical size, Å as U+212B, and a TiffData
    # whose UUID names the file itself (twice).
    planes("edge_ome_prefixed.tif", (
        '<?xml version="1.0"?><!-- <Pixels SizeZ="5"> --><ome:OME xmlns:ome="http://www.openmicroscopy.org/Schemas/OME/2016-06">'
        '<ome:Image ID="Image:0" Name=""><![CDATA[<Pixels SizeZ="7">]]>'
        '<ome:Pixels ID="Pixels:0" SizeZ=" 2 " SizeC="1" SizeT="1" DimensionOrder="XYZCT" Type="uint8" '
        'PhysicalSizeX="1,5" PhysicalSizeY="0" PhysicalSizeZ="2.5e-1" PhysicalSizeZUnit="Å">'
        '<ome:TiffData IFD="0" PlaneCount="2"><ome:UUID FileName="edge_ome_prefixed.tif">urn:uuid:1</ome:UUID></ome:TiffData>'
        '<ome:TiffData IFD="1" FirstZ="1"><ome:UUID FileName="edge_ome_prefixed.tif">urn:uuid:1</ome:UUID></ome:TiffData>'
        '</ome:Pixels></ome:Image></ome:OME>').encode() + b"\0", 2)
    # Later mappings win; entities in the name; excess PlaneCount ignored.
    planes("edge_ome_remap.tif", ome(tz, '<TiffData PlaneCount="9"/><TiffData FirstZ="1" IFD="2"/>',
                                     image_attrs='Name="a &amp; b &#x3b1; &foo;"'), 3)
    # ImageDescription of type UNDEFINED: not OME, one plane.
    t = Tiff()
    t.chain([image(t, description=(UNDEFINED, ome(tz))), image(t)])
    t.write("edge_ome_undefined_type.tif")
    # Invalid UTF-8 in the description: not OME.
    planes("edge_ome_invalid_utf8.tif", ome(tz).replace(b'Name="edge"', b'Name="\xff"'), 2)
    # Duplicate tags: the first wins (ImageWidth 64, then 32).
    t = Tiff()
    entries = [(256, SHORT, [64]), (256, SHORT, [32]), (257, SHORT, [64]), (258, SHORT, [8]), (259, SHORT, [1]),
               (322, SHORT, [32]), (323, SHORT, [32]),
               (324, LONG, [t.blob(bytes([i]) * 1024) for i in range(4)]), (325, LONG, [1024] * 4)]
    t.chain([t.ifd(entries)])
    t.write("edge_duplicate_tags.tif")
    # Not OME: a tiled IFD without BitsPerSample is skipped, the next is a level.
    t = Tiff()
    t.chain([image(t), image(t, 48, 48, skip={258}), image(t, 32, 32)])
    t.write("edge_svs_no_bps.tif")
    # Interleaved RGB with a SubIFD level.
    t = Tiff()
    sub = image(t, 32, 32, spp=3, extra=[(284, SHORT, [1])])
    t.chain([image(t, spp=3, extra=[(284, SHORT, [1])], subifds=[sub])])
    t.write("edge_rgb_subifd.tif")

    # Revision 3: the tag scan is one pass, so a "<!--" inside an attribute
    # value is text; the first of duplicate attributes wins; references to
    # U+0000 and surrogates stay as written.
    planes("edge_xml_scan.tif", (
        '<OME><Image Name="a <!-- b &#0; &#xD800; &#x1F600;" Name="second">'
        '<Pixels\tSizeZ="2" SizeZ="5" DimensionOrder="XYZCT"><TiffData/></Pixels></Image></OME>').encode() + b"\0", 2)
    # A self-closing Pixels has no TiffData (the second image's are not its).
    planes("edge_ome_self_closing_pixels.tif", (
        '<OME><Image Name="one"><Pixels SizeZ="1"/></Image>'
        '<Image Name="two"><Pixels SizeZ="2"><TiffData IFD="5"/></Pixels></Image></OME>').encode() + b"\0", 1)
    # UUID texts are compared after removing comments and surrounding whitespace.
    planes("edge_ome_uuid_text.tif", ome(tz, '<TiffData><UUID> urn:uuid:1 </UUID></TiffData>'
                                             '<TiffData FirstZ="1" IFD="1"><UUID>urn:<!-- x -->uuid:1</UUID></TiffData>'), 2)
    # LONG8 and IFD field types in a classic TIFF; a tag outside the table
    # with an unknown field type is ignored.
    t = Tiff()
    sub = image(t, 32, 32)
    offsets = [t.blob(bytes([i]) * 1024) for i in range(4)]
    t.chain([t.ifd([(256, 16, struct.pack("<Q", 64)), (257, SHORT, [64]), (258, SHORT, [8]), (305, 99, b"xxxx"),
                    (322, SHORT, [32]), (323, SHORT, [32]), (324, LONG, offsets), (325, LONG, [1024] * 4),
                    (330, 13, [sub])])])
    t.write("edge_field_types.tif")

    # Rejected.
    planes("edge_reject_multifile.tif", ome(tz, '<TiffData><UUID FileName="a.tif">urn:uuid:1</UUID></TiffData>'
                                                '<TiffData FirstZ="1"><UUID FileName="b.tif">urn:uuid:2</UUID></TiffData>'), 2)
    planes("edge_reject_multifile_uuid.tif", ome(tz, '<TiffData><UUID>urn:uuid:1</UUID></TiffData>'
                                                     '<TiffData FirstZ="1"><UUID>urn:uuid:2</UUID></TiffData>'), 2)
    planes("edge_reject_dimension_order.tif", ome('SizeZ="2" DimensionOrder="XYZZT"'), 2)
    planes("edge_reject_first_z.tif", ome(tz, '<TiffData FirstZ="2"/>'), 2)
    planes("edge_reject_size_zero.tif", ome('SizeZ="0"'), 1)
    planes("edge_reject_size_sign.tif", ome('SizeZ="+2"'), 2)
    planes("edge_reject_unmapped.tif", ome(tz, '<TiffData IFD="0"/>'), 2)
    t = Tiff()
    subs = [image(t, 32, 32)]
    t.chain([image(t, description=(ASCII, ome(tz)), subifds=subs), image(t)])
    t.write("edge_reject_subifd_count.tif")
    single("edge_reject_tile_outside.tif", tile_offsets=[10**6, 8, 8, 8])
    single("edge_reject_tile_count.tif", tile_offsets=[8, 8, 8])
    single("edge_reject_planar3.tif", spp=3, extra=[(284, SHORT, [3])])
    single("edge_reject_sample_format.tif", spp=3, extra=[(284, SHORT, [2]), (339, SHORT, [1, 1, 2])])
    single("edge_reject_no_width.tif", skip={256})
    # Planes of one level with different sizes.
    t = Tiff()
    t.chain([image(t, description=(ASCII, ome(tz))), image(t, 32, 32)])
    t.write("edge_reject_plane_size.tif")
    # A cycle in the main chain.
    t = Tiff()
    a, b = image(t), image(t)
    t.chain([a, b, a])
    t.write("edge_reject_cycle.tif")
    # BigTIFF with a nonzero reserved word.
    big = bytearray((OUT / "rgb_planar_jpeg2000_bigtiff_be.ome.tif").read_bytes())
    big[6:8] = b"\0\1"
    (OUT / "edge_reject_bigtiff_reserved.tif").write_bytes(bytes(big))
    # Revision 3 rejections.
    single("edge_reject_signed_width.tif", skip={256}, extra=[(256, 8, [64])])
    single("edge_reject_scalar_count0.tif", skip={259}, extra=[(259, SHORT, [])])
    single("edge_reject_zero_tile.tif", tile_offsets=[8], extra=[(322, SHORT, [0])], skip={322})
    # A main-chain IFD that is not a plane still has its tags checked.
    t = Tiff()
    t.chain([image(t, description=(ASCII, ome('SizeZ="1"'))), image(t, skip={259}, extra=[(259, 99, b"\0\0")])])
    t.write("edge_reject_unused_ifd_type.tif")
    t = Tiff()
    t.chain([image(t, description=(ASCII, ome('SizeZ="1"'))), image(t, skip={324}, extra=[(324, LONG, ("at", 4, 10**6))])])
    t.write("edge_reject_unused_value_outside.tif")
    # Two IFDs share a SubIFD.
    t = Tiff()
    shared = image(t, 32, 32)
    t.chain([image(t, subifds=[shared]), image(t, subifds=[shared])])
    t.write("edge_reject_shared_subifd.tif")
    # A level-scan candidate with PlanarConfiguration 3.
    t = Tiff()
    t.chain([image(t), image(t, 32, 32, spp=3, extra=[(284, SHORT, [3])])])
    t.write("edge_reject_candidate_planar3.tif")
    planes("edge_reject_nbsp_size.tif", ome('SizeZ="\u00a02"'), 2)
    planes("edge_reject_plane_limit.tif", ome('SizeT="100001"'), 1)
    # Revision 4.
    # "<!-->" does not end the comment it starts; SizeX is not validated.
    planes("edge_xml_comment_marker.tif", (
        '<OME><!--><Image Name="hidden"/>--><Image Name="shown"><Pixels SizeX="abc" SizeZ="1"/></Image></OME>'
    ).encode() + b"\0", 1)
    # A self-closing UUID names the file "" (here, besides x.tif).
    planes("edge_reject_uuid_self_closing.tif", ome(tz, '<TiffData><UUID/></TiffData>'
                                                        '<TiffData FirstZ="1" IFD="1"><UUID FileName="x.tif"/></TiffData>'), 2)
    # A LONG8 value above 2^53 - 1 rejects, even for a tile with no bytes.
    t = Tiff()
    offsets = [t.blob(bytes([i]) * 1024) for i in range(3)] + [2**60]
    t.chain([t.ifd([(256, SHORT, [64]), (257, SHORT, [64]), (258, SHORT, [8]), (322, SHORT, [32]), (323, SHORT, [32]),
                    (324, 16, struct.pack("<4Q", *offsets)), (325, LONG, [1024, 1024, 1024, 0])])])
    t.write("edge_reject_big_offset.tif")
    (OUT / "edge_reject_no_ifds.tif").write_bytes(b"II*\0\0\0\0\0" + b"\0" * 64)
    for p in sorted(OUT.glob("edge_*.tif")):
        print(p.name, p.stat().st_size)


if __name__ == "__main__":
    main()

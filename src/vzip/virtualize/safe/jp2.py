"""JPEG 2000 band files (profiles/safe.md §12.3) and their chunks (§12.6).

A band file is a JP2 file whose codestream has one tile-part per tile, in
raster order. Each tile becomes a standalone codestream: the file's main
header with a SIZ marker of its own, the tile's tile-part, and, at the
array's right and bottom edges, empty tiles that fill the chunk.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Callable

from vzip.virtualize.common import Rejected

MAX_BOXES = 1024
MAX_REST = 1 << 16  # the most bytes of a main header after its SIZ segment (profiles/safe.md §12.3)
MAX_TAIL = 1 << 12  # the most bytes of a chunk's empty tiles and EOC (§12.6)
BLOCK = 1 << 16
SMALL_TILE = 1 << 14  # after a tile-part this small, the next tile-part headers are read in a block
SOC, SIZ, SOT, EOC = 0xFF4F, 0xFF51, 0xFF90, 0xFFD9
COD, COC, QCD = 0xFF52, 0xFF53, 0xFF5C
# The markers a main header may have after SIZ (§12.3).
ALLOWED = {COD, COC, QCD, 0xFF5D, 0xFF5E, 0xFF5F, 0xFF63, 0xFF64}
NAMES = {0xFF55: "TLM", 0xFF57: "PLM", 0xFF60: "PPM", 0xFF50: "CAP", 0xFF59: "CPF"}


class Reader:
    """Reads one band file, whose bytes are `raw(offset, length)` of a source
    from `base` on, `n` bytes long: a block cache for the boxes and the main
    header, and small reads of their own for the tile-part headers, with a block
    when the tile-parts are small."""

    def __init__(self, raw: Callable[[int, int], bytes], base: int, n: int) -> None:
        self.raw, self.base, self.n = raw, base, n
        self.window = (0, b"")  # (start, bytes) of the last block read
        self.requests = 0

    def read(self, offset: int, length: int, ahead: int = BLOCK) -> bytes:
        if offset < 0 or offset + length > self.n:
            raise Rejected(f"a band file read of [{offset}, {offset + length}) is outside its {self.n} bytes")
        start, data = self.window
        if start <= offset and offset + length <= start + len(data):
            return data[offset - start : offset - start + length]
        size = min(self.n - offset, max(length, ahead))
        self.requests += 1
        data = self.raw(self.base + offset, size)
        self.window = (offset, data)
        return data[:length]


@dataclass
class Codestream:
    """A band file's structure (§12.3)."""

    n: int
    c0: int  # where the codestream starts: the JP2 header is [0, c0)
    siz: bytes  # the SIZ segment, from its marker
    rest: bytes  # the main header after the SIZ segment, up to the first SOT
    width: int
    height: int
    tile_w: int
    tile_h: int
    components: int
    precision: int
    layers: int
    # Per component: (decomposition levels N, [(PPx_r, PPy_r) for r = 0..N]).
    coding: list[tuple[int, list[tuple[int, int]]]] = field(default_factory=list)
    tiles: list[tuple[int, int]] = field(default_factory=list)  # (s_k, Psot) per tile

    @property
    def grid(self) -> tuple[int, int]:
        return -(-self.width // self.tile_w), -(-self.height // self.tile_h)


def _u16(b: bytes, at: int) -> int:
    return struct.unpack_from(">H", b, at)[0]


def boxes(r: Reader) -> int:
    """c0, the start of the codestream: the contents of the first jp2c box,
    which must end at the end of the file (§12.3)."""
    n, o = r.n, 0
    for i in range(MAX_BOXES):
        if o + 8 > n:
            raise Rejected("a band file is not a JP2 file: its boxes end without a jp2c box")
        head = r.read(o, 8)
        lbox, tbox = struct.unpack(">I4s", head)
        if i == 0 and (lbox != 12 or tbox != b"jP  " or r.read(o + 8, 4) != b"\r\n\x87\n"):
            raise Rejected("a band file is not a JP2 file: no JPEG 2000 signature box")
        if lbox == 1:
            if o + 16 > n:
                raise Rejected("a JP2 box's XLBox is beyond the file")
            length, hl = struct.unpack(">Q", r.read(o + 8, 8))[0], 16
            if length < 16:
                raise Rejected(f"a JP2 box's XLBox is {length}, less than 16")
        elif lbox == 0:
            length, hl = n - o, 8
        else:
            length, hl = lbox, 8
            if length < 8:
                raise Rejected(f"a JP2 box's LBox is {length}")
        if o + length > n:
            raise Rejected("a JP2 box reaches beyond the file")
        if i == 1 and tbox != b"ftyp":
            raise Rejected("a band file's second box is not ftyp")
        if tbox == b"jp2c":
            if o + length != n:
                raise Rejected("a band file's jp2c box does not end at the end of the file")
            return o + hl
        o += length
    raise Rejected(f"a band file has no jp2c box in its first {MAX_BOXES} boxes")


def _coding(seg: bytes, at: int, scod: int, what: str) -> tuple[int, list[tuple[int, int]]]:
    """(N, [(PPx_r, PPy_r)]) from the SPcod/SPcoc fields at `at` (§12.3)."""
    levels = seg[at]
    if levels > 32:
        raise Rejected(f"{what} has {levels} decomposition levels, more than 32")
    if scod & 1:
        pp = [(b & 0x0F, b >> 4) for b in seg[at + 5 : at + 6 + levels]]
    else:
        pp = [(15, 15)] * (levels + 1)
    return levels, pp


def main_header(r: Reader, c0: int) -> Codestream:
    """The SIZ segment and the main header rest of the codestream at c0 (§12.3)."""
    c1 = r.n
    if c0 + 4 > c1 or _u16(r.read(c0, 2), 0) != SOC:
        raise Rejected("a band file's codestream does not start with SOC")
    if _u16(r.read(c0 + 2, 2), 0) != SIZ:
        raise Rejected("a band file's codestream has no SIZ marker after SOC")
    if c0 + 6 > c1:
        raise Rejected("a band file's SIZ marker is truncated")
    lsiz = _u16(r.read(c0 + 4, 2), 0)
    if c0 + 4 + lsiz > c1 or lsiz < 41:
        raise Rejected("a band file's SIZ segment is truncated")
    siz = r.read(c0 + 2, 2 + lsiz)
    rsiz, xs, ys, xo, yo, xt, yt, xto, yto, csiz = struct.unpack_from(">HIIIIIIIIH", siz, 4)
    if lsiz != 38 + 3 * csiz:
        raise Rejected(f"a band file's Lsiz {lsiz} is not 38 + 3 × Csiz")
    if rsiz & 0xC000:
        raise Rejected("a band file uses JPEG 2000 Part 2 or High Throughput (Rsiz)")
    if xo or yo or xto or yto:
        raise Rejected("a band file's image or tile origin is not 0")
    if not (xs and ys and xt and yt):
        raise Rejected("a band file's image or tile size is 0")
    if csiz not in (1, 3):
        raise Rejected(f"a band file has {csiz} components, not 1 or 3")
    comps = [siz[40 + 3 * c : 43 + 3 * c] for c in range(csiz)]
    if len({c[0] for c in comps}) != 1:
        raise Rejected("a band file's components differ in precision or sign")
    if comps[0][0] & 0x80:
        raise Rejected("a band file's components are signed")
    precision = (comps[0][0] & 0x7F) + 1
    if precision > 16:
        raise Rejected(f"a band file's components have {precision} bits, more than 16")
    if any(c[1] != 1 or c[2] != 1 for c in comps):
        raise Rejected("a band file's components are subsampled")
    pos = c0 + 4 + lsiz
    cod = None
    qcd = 0
    cocs: dict[int, tuple[int, list[tuple[int, int]]]] = {}
    while True:
        if pos + 2 > c1:
            raise Rejected("a band file's main header has no SOT")
        marker = _u16(r.read(pos, 2), 0)
        if marker == SOT:
            break
        if marker not in ALLOWED:
            name = NAMES.get(marker, f"{marker:04X}")
            raise Rejected(f"a band file's main header has a {name} marker")
        if pos + 4 > c1:
            raise Rejected("a band file's main header marker segment is truncated")
        length = _u16(r.read(pos + 2, 2), 0)
        if length < 2 or pos + 2 + length > c1:
            raise Rejected("a band file's main header marker segment is truncated")
        seg = r.read(pos + 4, length - 2)
        if marker == COD:
            if cod is not None:
                raise Rejected("a band file's main header has two COD markers")
            if length < 12:
                raise Rejected("a band file's COD segment is truncated")
            scod, _, layers, _ = struct.unpack_from(">BBHB", seg, 0)
            if scod > 1:
                raise Rejected("a band file's COD uses SOP or EPH markers, or precinct anchors (Scod)")
            if layers < 1:
                raise Rejected("a band file's COD has no layers")
            if length != (13 + seg[5] if scod else 12):
                raise Rejected("a band file's COD segment has the wrong length")
            cod = (layers, _coding(seg, 5, scod, "a band file's COD"))
        elif marker == COC:
            if length < 9:
                raise Rejected("a band file's COC segment is truncated")
            c, scoc = seg[0], seg[1]
            if c >= csiz or c in cocs:
                raise Rejected("a band file's COC names no component, or one already named")
            if scoc > 1:
                raise Rejected("a band file's COC has a bad Scoc")
            if length != (10 + seg[2] if scoc else 9):
                raise Rejected("a band file's COC segment has the wrong length")
            cocs[c] = _coding(seg, 2, scoc, "a band file's COC")
        elif marker == QCD:
            qcd += 1
        pos += 2 + length
    if cod is None or qcd != 1:
        raise Rejected("a band file's main header does not have exactly one COD and one QCD")
    rest_start = c0 + 4 + lsiz
    if pos - rest_start > MAX_REST:
        raise Rejected(f"a band file's main header is more than {MAX_REST} bytes")
    rest = r.read(rest_start, pos - rest_start)
    cs = Codestream(n=r.n, c0=c0, siz=siz, rest=rest, width=xs, height=ys, tile_w=xt, tile_h=yt,
                    components=csiz, precision=precision, layers=cod[0],
                    coding=[cocs.get(c, cod[1]) for c in range(csiz)])
    nx, ny = cs.grid
    if nx * ny > 65535:
        raise Rejected(f"a band file has {nx * ny} tiles, more than 65535")
    cs.tiles = [(pos, 0)]  # s_0; the walk fills in the rest
    return cs


def walk(r: Reader, cs: Codestream) -> None:
    """The tile-parts (§12.3): one per tile, in raster order, then EOC at the end."""
    c1 = r.n
    nx, ny = cs.grid
    s = cs.tiles[0][0]
    tiles = []
    ahead = BLOCK
    for k in range(nx * ny):
        if s + 12 > c1:
            raise Rejected(f"a band file's tile-part {k} is beyond the codestream")
        head = r.read(s, 12, ahead)
        marker, lsot, isot, psot, tpsot, tnsot = struct.unpack(">HHHIBB", head)
        if marker != SOT or lsot != 10:
            raise Rejected(f"a band file has no SOT marker segment for tile {k}")
        if isot != k:
            raise Rejected(f"a band file's tile-parts are not in raster order (tile {isot} where {k} is)")
        if psot < 14:
            raise Rejected(f"a band file's tile-part {k} has Psot {psot}")
        if tpsot != 0 or tnsot != 1:
            raise Rejected(f"a band file's tile {k} has more than one tile-part")
        if s + psot > c1 - 2:
            raise Rejected(f"a band file's tile-part {k} reaches beyond the codestream")
        tiles.append((s, psot))
        ahead = BLOCK if psot <= SMALL_TILE else 12
        s += psot
    if s + 2 != c1 or _u16(r.read(s, 2, 2), 0) != EOC:
        raise Rejected("a band file's codestream does not end with EOC after its last tile-part")
    cs.tiles = tiles


def read_band(raw: Callable[[int, int], bytes], base: int, n: int) -> tuple[Codestream, int]:
    """The structure of the band file of `n` bytes at `base` of a source, and the reads it took."""
    r = Reader(raw, base, n)
    cs = main_header(r, boxes(r))
    walk(r, cs)
    return cs, r.requests


def _nprec(z0: int, z1: int, d: int, e: int) -> int:
    a, b = -(-z0 // (1 << d)), -(-z1 // (1 << d))
    return -(-b // (1 << e)) - (a >> e) if b > a else 0


def empty_packets(cs: Codestream, x0: int, x1: int, y0: int, y1: int) -> int:
    """e_k: the packets of a tile of area [x0, x1) × [y0, y1) (§12.6)."""
    n = 0
    for levels, pp in cs.coding:
        for res in range(levels + 1):
            ppx, ppy = pp[res]
            n += _nprec(x0, x1, levels - res, ppx) * _nprec(y0, y1, levels - res, ppy)
    return cs.layers * n


def _area(cs: Codestream, t: int) -> tuple[int, int, int, int, int, int]:
    """(x0, y0, w, h, a, b) of the chunk of tile t (§12.6)."""
    nx, _ = cs.grid
    T, U = cs.tile_w, cs.tile_h
    x0, y0 = (t % nx) * T, (t // nx) * U
    w, h = min(T, cs.width - x0), min(U, cs.height - y0)
    if x0 + T > 0xFFFFFFFF or y0 + U > 0xFFFFFFFF:
        raise Rejected("a chunk's image area reaches beyond 2^32 − 1")
    return x0, y0, w, h, -(-T // w), -(-U // h)


def tail_packets(cs: Codestream, t: int) -> list[int]:
    """[e_1, …, e_{a×b−1}], the empty packets of the empty tiles of the chunk of tile t,
    once their tail (§12.6), of 2 + Σ (14 + e_k) bytes, is known to be at most MAX_TAIL:
    it is measured before anything is made, and rejected past MAX_TAIL."""
    x0, y0, w, h, a, b = _area(cs, t)
    T, U = cs.tile_w, cs.tile_h
    too_long = f"the empty tiles of a chunk of tile {t} would take more than {MAX_TAIL} bytes"
    if 2 + 14 * (a * b - 1) > MAX_TAIL:
        raise Rejected(too_long)
    out, length = [], 2
    for k in range(1, a * b):
        i, j = k % a, k // a
        e = empty_packets(cs, x0 + i * w, min(x0 + (i + 1) * w, x0 + T), y0 + j * h, min(y0 + (j + 1) * h, y0 + U))
        length += 14 + e
        if length > MAX_TAIL:
            raise Rejected(too_long)
        out.append(e)
    return out


def tail_length(cs: Codestream, t: int) -> int:
    """The length of the chunk of tile t's empty tiles and EOC (§12.6)."""
    return 2 + sum(14 + e for e in tail_packets(cs, t))


def chunk_parts(cs: Codestream, t: int) -> tuple[bytes, bytes, tuple[int, int], bytes, bool]:
    """The chunk of tile t (§12.6): (SOC + SIZ', SOT, (offset, length) of the body in the band
    file, the empty tiles and EOC, whether it has empty tiles)."""
    x0, y0, w, h, a, b = _area(cs, t)
    T, U = cs.tile_w, cs.tile_h
    packets = tail_packets(cs, t)
    siz = bytearray(cs.siz)
    struct.pack_into(">HIIIIIIII", siz, 4, 0, x0 + T, y0 + U, x0, y0, w, h, x0, y0)
    s, psot = cs.tiles[t]
    sot = struct.pack(">HHHIBB", SOT, 10, 0, psot, 0, 1)
    tail = bytearray()
    for k, e in enumerate(packets, 1):
        tail += struct.pack(">HHHIBB", SOT, 10, k, 14 + e, 0, 1) + b"\xff\x93" + bytes(e)
    tail += b"\xff\xd9"
    return b"\xff\x4f" + bytes(siz), sot, (s + 12, psot - 12), bytes(tail), a * b > 1

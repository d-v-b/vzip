"""The CZI IR (round 3): the Rust parser and its schema, the image projection against
today's profile, the compact mirror, the streaming XML values, and round 1's CZI
review findings, each closed and probed."""

import io
import json
import threading
from http.server import ThreadingHTTPServer

import pytest
import vzip_ir

from conftest import FIXTURES, same_hierarchy
from vzip.ir import virtualize as via_ir
from vzip.ir.cmirror import rebuild_from_archive
from vzip.ir import parse
from vzip.ir.planner import HttpTransport
from vzip.policy import Policy
from vzip.virtualize import Rejected
from vzip_reference import virtualize  # the frozen reference (conftest)
from vzip_reference.czi.xml import read_xml_values

CZIS = sorted((FIXTURES / "czi").iterdir())


@pytest.mark.parametrize("path", CZIS, ids=lambda p: p.name)
def test_projects_as_today_and_rebuilds(path, tmp_path):
    data = path.read_bytes()
    url = f"https://data.test/{path.name}"
    try:
        _, today = virtualize(str(path), url)
    except Rejected:
        with pytest.raises(Rejected):
            via_ir(str(path), url)
        return
    fmt, out, ir = via_ir(str(path), url)
    assert fmt == "czi"
    ir.check()
    same_hierarchy(today, out)
    out.write(str(tmp_path / "a.vzip"))
    buf = io.BytesIO()
    rebuild_from_archive(str(tmp_path / "a.vzip"), lambda o, n: data[o:o + n], buf.write)
    assert buf.getvalue() == data


XMLS = [
    "<ImageDocument><Metadata><Scaling><Items><Distance Id='Y'><Value>2e-7</Value></Distance>"
    "<Distance Id='X'><Value>1e-7</Value><Value>9</Value></Distance><Distance Id='X'><Value>5</Value></Distance>"
    "</Items></Scaling><Information><Image><ComponentBitCount>12</ComponentBitCount><Dimensions>"
    "<Channels><Channel Name='a'><Color>#FF00FF00</Color><ComponentBitCount>10</ComponentBitCount></Channel>"
    "<Channel/><Channel Name='c'><!-- x --><Color>#<![CDATA[]]>00FF00</Color></Channel></Channels>"
    "<T><Positions><Interval><Increment> 0.5 </Increment></Interval></Positions></T>"
    "<S><Scenes><Scene Index='1' Name='one'/><Scene Index='1' Name='two'/><Scene Index='0'/></Scenes></S>"
    "</Dimensions></Image></Information><DisplaySetting><Channels><Channel Name='d'><Low>0.1</Low>"
    "<High>0.9</High><Color>#123456</Color></Channel></Channels></DisplaySetting></Metadata></ImageDocument>",
    "<ImageDocument><Metadata></Scaling><Scaling><Items><Distance Id='X'><Value>3</Value></Items>"
    "</Metadata><Metadata><Scaling/></Metadata></ImageDocument>",
    "<Other><Metadata/></Other>",
    "﻿<ImageDocument><Metadata><Information><Image><ComponentBitCount>0000000000000000008</ComponentBitCount>"
    "</Image></Information></Metadata></ImageDocument>",
    "<ImageDocument>" + "<a>" * 50 + "</b>" * 50 + "<Metadata><Information><Image><ComponentBitCount>8"
    "</ComponentBitCount></Image></Information></Metadata></ImageDocument>",
    "<ImageDocument><Metadata><Information><Image><Dimensions><Channels><Channel Name='a &amp; b'>"
    "<Color>#gg0000</Color></Channel></Channels></Dimensions></Image></Information></Metadata></ImageDocument>",
]


def as_dict(v):
    ch = lambda c: {"name": c.name, "color": c.color, "bits": c.bits}  # noqa: E731
    disp = lambda c: {"name": c.name, "color": c.color, "low": c.low, "high": c.high}  # noqa: E731
    return {"px": v.px, "py": v.py, "pz": v.pz, "inc": v.inc, "bits": v.bits, "info": [ch(c) for c in v.info],
            "display": [disp(c) for c in v.display], "scenes": sorted(v.scenes.items())}


def test_streaming_xml_values_are_the_tree_s():
    for xml in XMLS:
        r = json.loads(vzip_ir.czi_xml_values(xml.encode()))
        r["info"] = [{k: c[k] for k in ("name", "color", "bits")} for c in r["info"]]
        r["display"] = [{k: c[k] for k in ("name", "color", "low", "high")} for c in r["display"]]
        r["scenes"] = sorted((i, n) for i, n in r["scenes"])
        assert r == as_dict(read_xml_values(xml.encode())), xml[:60]


def test_bands_have_a_floor():
    """Divisor bands (spec/virtualize/czi.md §4.3) when they hold at least 1 MiB; else bands of the
    most rows within 2^24 bytes, the last of each tile row shorter."""
    cases = [
        # (h, h2, r, w, q, uncompressed) -> (y lengths, rows, per)
        ((4, 4, 3, 4, 1, True), (4, 4, 1)),  # small: no bands
        ((4, 2, 3, 4, 1, True), ([[4, 2], 2], 4, 1)),
        ((4, 2, 1, 4, 1, True), ([2], 4, 1)),
        ((4096, 4096, 3, 8192, 1, True), (2048, 2048, 2)),  # divisor bands of 16 MiB
        ((16777259, 16777259, 1, 1, 1, True), ([1 << 24, 43], 1 << 24, 2)),  # a prime height: the floor
        ((16777259, 16777259, 1, 1, 1, False), (16777259, 16777259, 1)),  # compressed: one chunk
    ]
    for args, (y, rows, per) in cases:
        got = vzip_ir.czi_bands(*args)
        assert (json.loads(got[0]), got[1], got[2]) == (y, rows, per), args


def test_entries_that_share_a_subblock_are_aliases_and_place_it_once(probes):
    _, out, ir = via_ir(str(probes / "czi_dup.czi"), "u", mirror=False)
    subs = [ir.child(0, f"subblocks/{i}") for i in range(1000)]
    assert ir.kind(subs[0]) == 0 and all(ir.kind(s) == 4 and ir.target(s) == subs[0] for s in subs[1:])
    assert list(out.refs) == ["0/0/c/0/0"] and "tiles/zarr.json" not in out.bytes_entries  # today: 1,000 tile arrays


def test_rejects_a_tile_array_past_2_to_the_31(probes):
    with pytest.raises(Rejected, match="more than 2\\^31"):
        via_ir(str(probes / "czi_jxrbig.czi"), "u", mirror=False)


def test_unmatched_end_tags_and_flat_xml_stream(probes):
    for name in ("czi_deep2000.czi", "czi_flat.czi"):
        _, today = virtualize(str(probes / name), "u")
        _, out, _ = via_ir(str(probes / name), "u", mirror=False)
        same_hierarchy(today, out)


def test_attachment_names_keep_their_bytes_after_the_nul(probes):
    _, _, ir = via_ir(str(probes / "czi_attname.czi"), "u", mirror=False)
    d = ir.child(0, "attachment_directory")
    e = ir.value(ir.child(d, "entries/0"))
    assert e["name"] == {"text": "Label", "after": b"\0hidden name" + bytes(80 - 17)}
    assert e["content_file_type"] == {"text": "CZTIMS", "after": b"\0x"}
    e = ir.value(ir.child(d, "entries/1"))
    assert e["name"] == {"text": "Thumb", "after": b"\0" * 6 + b"x" + bytes(80 - 12)}
    a = ir.child(0, "attachments/0")
    assert ir.child(ir.child(a, "data"), "values") is not None  # read as CZTIMS, by its bytes up to the NUL


def test_empty_segments_fold_into_one_run(probes):
    _, _, ir = via_ir(str(probes / "czi_walk0.czi"), "u", mirror=False)
    segs = [i for i in ir.children(0) if ir.name(i).startswith("segments/")]
    assert len(segs) == 1 and ir.run(segs[0])[0] == 10000
    assert len(ir) < 100


class _Ranges:
    """A local server of single ranges (no multipart) over a file."""

    def __init__(self, path):
        from http.server import BaseHTTPRequestHandler

        data = path.read_bytes()
        self.bytes = 0

        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                rng = self.headers.get("Range", "")
                if "," in rng:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                a, b = rng[6:].split("-")
                a, b = int(a), min(int(b) + 1, len(data))
                outer.bytes += b - a
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {a}-{b - 1}/{len(data)}")
                self.send_header("Content-Length", str(b - a))
                self.end_headers()
                self.wfile.write(data[a:b])

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/f"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_the_planner_reads_headers_not_the_whole_file(probes):
    """2,000 subblocks of 4 KiB over a single-range server: the parser asks for their
    headers in batches, and the planner fetches at most its cap times the bytes asked."""
    path = probes / "czi_xt.czi"
    server = _Ranges(path)
    try:
        ir, facts, st = parse("czi", HttpTransport(server.url, Policy(allow_private_hosts=True)), 4)
        # requests cost the batch less than half a second here, so the cap is 4; on a
        # loaded machine they may cost more, and the ceiling of 16 applies
        assert st["packs"] is False and st["cap"] in (4, 16)
        assert st["bytes"] <= st["cap"] * st["asked"] + (1 << 16) * st["batches"]
        assert facts["subblocks"] == 2000 and facts["rounds"] < 12
    finally:
        server.close()

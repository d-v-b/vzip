"""The round-2 ND2 probes (src/vzip/ir/NOTES.md §10): byte-array and record bombs, failed decodes,
repeated names, sparse families, positions without frames, sparse frame times.
Usage: uv run python experiments/ir_round2/gen_nd2_probes.py <out dir>"""
import sys, struct, zlib
sys.path.insert(0, "web/test/nd2")
from pathlib import Path
from write_fixtures import Nd2, lv, level, compressed, attributes, picture, node, experiment
out = Path(sys.argv[1])

def base(frames=1, pad=32):
    f = Nd2()
    f.chunk("ImageAttributesLV!", attributes(2, 2, 1, 16, sequence=frames), name_pad=pad)
    f.chunk("ImageMetadataLV!", experiment(node(1, {"uiCount": frames, "dPeriod": 1.0})), name_pad=pad)
    return f

def frames(f, n, pad=32):
    for i in range(n):
        f.chunk(f"ImageDataSeq|{i}!", struct.pack("<d", i) + bytes(8), name_pad=pad)

def bytearray_rec(name, n):
    return bytes([9]) + bytes([len(name) + 1]) + (name + "\0").encode("utf-16le") + struct.pack("<Q", n) + bytes(n)

# 1. a byte array past the profile's record budget, in the attributes: rejected
f = Nd2()
f.chunk("ImageAttributesLV!", attributes(2, 2, 1, 16, sequence=1)[:-0] + bytearray_rec("Big", (1 << 20) + 1), name_pad=32)
frames(f, 1); f.finish(out / "bomb_bytes_profile.nd2")
# 2. a 64 MiB byte array in a CustomData chunk (past the source metadata's room: kept as bytes)
f = base(); f.chunk("CustomData|Big!", bytearray_rec("Big", 1 << 26), name_pad=32); frames(f, 1); f.finish(out / "bomb_bytes_custom.nd2")
# 3. 300 compressed CustomData chunks of 65000 records each (19.5 M records decoded for the source metadata)
f = base()
rec = bytes([1, 0, 1])
body = level("L", [rec] * 0)
lvl = bytes([11]) + bytes([2]) + "L\0".encode("utf-16-le")
recs = rec * 65000
lvl = lvl + struct.pack("<IQ", 65000, len(lvl) + 12 + len(recs)) + recs + bytes(8 * 65000)
z = compressed(lvl)
for i in range(300):
    f.chunk(f"CustomData|R{i}!", z, name_pad=32)
frames(f, 1); f.finish(out / "bomb_records_300.nd2")
# 4. failed decodes: invalid zlib, a lossy guess, an unknown LV type, a broken XML variant, a stream too short
f = base()
f.chunk("CustomData|BadZlib!", bytes([76, 0]) + bytes(10) + b"not zlib at all", name_pad=32)
f.chunk("CustomData|Lossy!", bytes([1, 3]) + "a\0\0".encode("utf-16-le") + b"\x01", name_pad=32)  # name units then 2 NULs
f.chunk("ThingLV!", bytes([10, 0]) + b"\x00", name_pad=32)
f.chunk("CustomDataVar|Broken!", b"<variant><a runtype='lx_int32' value='1'></variant>", name_pad=32)
f.chunk("CustomData|AcqTimesCache!", b"\x00" * 4, name_pad=32)
frames(f, 2); f.finish(out / "failed_decodes.nd2")
# 5. repeated names: a name twice in the map, a level with repeated members, names that read alike
f = base()
f.chunk("CustomData|Twice!", lv("A", 1), name_pad=32)
f.chunk("CustomData|Twice!", lv("A", 2), name_pad=32)
f.chunk("RepeatLV!", level("", [lv("x", 1), lv("x", 2), lv("y", 3)]), name_pad=32)
f.raw_chunk("CustomData|Café!".encode("utf-8"), lv("a", 1), name_pad=32)
f.raw_chunk("CustomData|CafÃ©!".encode("latin-1"), lv("a", 2), name_pad=32)
frames(f, 2); f.finish(out / "repeated_names.nd2")
# 6. sparse families: CustomDataSeq members at 0, 1 and 10^7, and a frame far beyond N
f = base(frames=2)
for i in (0, 1, 10_000_000):
    f.chunk(f"CustomDataSeq|Seq|{i}!", struct.pack("<d", i), name_pad=32)
frames(f, 2)
f.chunk("ImageDataSeq|99999999!", struct.pack("<d", 9) + bytes(8), name_pad=32)
f.finish(out / "sparse_families.nd2")
# 7. a declared position count with no frames: 60000 positions, components 16
f = Nd2()
f.chunk("ImageAttributesLV!", attributes(2, 2, 16, 16, sequence=0), name_pad=32)
f.chunk("ImageMetadataLV!", experiment(node(2, {"Points": [{}] * 60000})), name_pad=32)
f.finish(out / "positions_no_frames.nd2")
# 8. frame-time chunks: 2^20 x 64 grid, placed frames scattered every 4093rd
f = Nd2()
f.chunk("ImageAttributesLV!", attributes(1, 1, 1, 8, sequence=1), name_pad=32)
f.chunk("ImageMetadataLV!", experiment(node(1, {"uiCount": 1 << 20, "dPeriod": 1.0}, [node(4, {"uiCount": 64, "dZStep": 1.0})])), name_pad=32)
for k in range(0, (1 << 26), 4093 * 64 + 7)[:4000]:
    f.chunk(f"ImageDataSeq|{k}!", struct.pack("<d", k) + b"\x07", name_pad=24)
f.finish(out / "frame_times_sparse.nd2")
print("ok")

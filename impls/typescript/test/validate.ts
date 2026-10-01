// Independent structural checks of the writer requirements (§3.1, §3.2, §3.4, §4, §7.1).
import assert from "node:assert/strict";
import * as zlib from "node:zlib";
import { decodeCdIndex, decodeConcat, decodeRange, encodeCdIndex, encodeConcat, encodeRange, encodeSourceTable, decodeSourceTable } from "../src/proto.ts";

export function validateArchive(b: Buffer): void {
  const commentLen = b.readUInt16LE(b.length - 60 + 20) === 38 && b.readUInt32LE(b.length - 60) === 0x06054b50 ? 38 : 22;
  const eocd = b.length - 22 - commentLen;
  assert.equal(b.readUInt32LE(eocd), 0x06054b50);
  assert.equal(b.readUInt16LE(eocd + 20), commentLen);
  const comment = b.subarray(eocd + 22);
  assert.equal(comment.subarray(0, 6).toString(), "vzip/1");
  let count = b.readUInt16LE(eocd + 10);
  let cdSize = BigInt(b.readUInt32LE(eocd + 12));
  let cdOff = BigInt(b.readUInt32LE(eocd + 16));
  const zip64 = count === 0xffff || cdSize === 0xffffffffn || cdOff === 0xffffffffn;
  if (zip64) {
    assert.equal(b.readUInt32LE(eocd - 20), 0x07064b50);
    assert.equal(b.readUInt32LE(eocd - 20 + 16), 1);
    const z = Number(b.readBigUInt64LE(eocd - 12));
    assert.equal(z, eocd - 76);
    assert.equal(b.readUInt32LE(z), 0x06064b50);
    assert.equal(b.readBigUInt64LE(z + 4), 44n);
    assert.equal(b.readUInt16LE(z + 14), 45);
    count = Number(b.readBigUInt64LE(z + 32));
    cdSize = b.readBigUInt64LE(z + 40);
    cdOff = b.readBigUInt64LE(z + 48);
    assert.ok(count >= 0xffff || cdSize >= 0xffffffffn || cdOff >= 0xffffffffn, "zip64 only where needed");
  }
  // walk CD
  const names: string[] = [];
  const recs: { name: string; start: number; len: number; lho: number; method: number; csize: number; usize: number; extra: Buffer }[] = [];
  let p = Number(cdOff);
  const end = p + Number(cdSize);
  while (p < end) {
    assert.equal(b.readUInt32LE(p), 0x02014b50);
    const flags = b.readUInt16LE(p + 8);
    assert.equal(flags, 0x0800);
    const method = b.readUInt16LE(p + 10);
    const crc = b.readUInt32LE(p + 16);
    const csize = b.readUInt32LE(p + 20);
    const usize = b.readUInt32LE(p + 24);
    const nl = b.readUInt16LE(p + 28);
    const xl = b.readUInt16LE(p + 30);
    assert.equal(b.readUInt16LE(p + 32), 0, "no file comment");
    const lho = b.readUInt32LE(p + 42);
    const name = b.subarray(p + 46, p + 46 + nl).toString("utf8");
    const extra = b.subarray(p + 46 + nl, p + 46 + nl + xl);
    assert.notEqual(lho, 0xffffffff);
    // local header agrees
    assert.equal(b.readUInt32LE(lho), 0x04034b50);
    assert.equal(b.readUInt16LE(lho + 6), 0x0800);
    assert.equal(b.readUInt16LE(lho + 8), method);
    assert.equal(b.readUInt32LE(lho + 14), crc);
    assert.equal(b.readUInt32LE(lho + 18), csize);
    assert.equal(b.readUInt32LE(lho + 22), usize);
    assert.equal(b.readUInt16LE(lho + 26), nl);
    assert.equal(b.readUInt16LE(lho + 28), 0, "local extra length 0");
    const body = b.subarray(lho + 30 + nl, lho + 30 + nl + csize);
    const data = method === 8 ? zlib.inflateRawSync(body) : body;
    assert.equal(data.length, usize);
    assert.equal(zlib.crc32(data), crc);
    // extra blocks
    const ids: number[] = [];
    for (let q = 0; q < extra.length; ) {
      const id = extra.readUInt16LE(q);
      const sz = extra.readUInt16LE(q + 2);
      ids.push(id);
      const d = extra.subarray(q + 4, q + 4 + sz);
      if (id === 0x7a76 || id === 0x7a77) {
        assert.equal(method, 0);
        assert.ok(sz <= 65519);
        // canonical encoding
        if (id === 0x7a76) assert.deepEqual(Buffer.from(encodeRange(decodeRange(d))), d);
        else {
          const parts = decodeConcat(d);
          assert.notEqual(parts.length, 1);
          assert.deepEqual(Buffer.from(encodeConcat(parts)), d);
        }
        assert.ok(body.length === 0 || body.equals(d));
      }
      q += 4 + sz;
    }
    assert.ok(ids.filter((i) => i === 0x7a76 || i === 0x7a77).length <= 1);
    recs.push({ name, start: p - Number(cdOff), len: 46 + nl + xl, lho, method, csize, usize, extra });
    names.push(name);
    p += 46 + nl + xl;
  }
  assert.equal(p, end);
  assert.equal(recs.length, count);
  assert.equal(new Set(names).size, names.length);
  const bodyOff = (r: (typeof recs)[0]) => r.lho + 30 + Buffer.byteLength(r.name);
  const src = recs.find((r) => r.name === "__vz__/sources")!;
  assert.equal(src.method, 8);
  assert.equal(comment.readBigUInt64LE(6), BigInt(bodyOff(src)));
  assert.equal(comment.readBigUInt64LE(14), BigInt(src.csize));
  const srcData = zlib.inflateRawSync(b.subarray(bodyOff(src), bodyOff(src) + src.csize));
  assert.deepEqual(Buffer.from(encodeSourceTable(decodeSourceTable(srcData))), srcData);
  const idxRec = recs.find((r) => r.name === "__vz__/index");
  assert.equal(idxRec !== undefined, commentLen === 38);
  if (idxRec) {
    assert.equal(comment.readBigUInt64LE(22), BigInt(bodyOff(idxRec)));
    assert.equal(comment.readBigUInt64LE(30), BigInt(idxRec.csize));
    const raw = zlib.inflateRawSync(b.subarray(bodyOff(idxRec), bodyOff(idxRec) + idxRec.csize));
    const idx = decodeCdIndex(raw);
    assert.deepEqual(Buffer.from(encodeCdIndex(idx)), raw);
    const body = recs.slice(0, -2);
    assert.deepEqual(recs.slice(-2).map((r) => r.name).sort(), ["__vz__/index", "__vz__/sources"]);
    const sorted = [...body].sort((x, y) => Buffer.compare(Buffer.from(x.name), Buffer.from(y.name)));
    assert.deepEqual(body.map((r) => r.name), sorted.map((r) => r.name));
    // pages partition body records
    let off = 0;
    let ri = 0;
    for (const pg of idx.pages) {
      assert.equal(Number(pg.offset), off);
      assert.ok(pg.length > 0n);
      assert.equal(body[ri].start, off);
      assert.equal(pg.firstKey, body[ri].name);
      off += Number(pg.length);
      while (ri < body.length && body[ri].start < off) ri++;
      assert.equal(ri === body.length ? recs[recs.length - 2].start : body[ri].start, off, "page holds whole records");
    }
    assert.equal(ri, body.length);
    for (const pin of idx.pinned) {
      const r = recs.find((x) => x.name === pin.key)!;
      assert.equal(Number(pin.dataOffset), bodyOff(r));
      assert.equal(Number(pin.csize), r.csize);
      assert.equal(Number(pin.size), r.usize);
      assert.equal(pin.method, r.method);
      assert.ok(bodyOff(idxRec) > bodyOff(r));
    }
  }
}

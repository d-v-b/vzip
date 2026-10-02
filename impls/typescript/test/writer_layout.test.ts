// Header layout of large entries and big offsets (spec §3.1 rules 4 and 7, §3.2),
// checked on the header builders directly so no 4 GiB body is needed.
import { test } from "node:test";
import assert from "node:assert/strict";
import { cdRecord, localHeader, type Built } from "../src/writer.ts";

const ALL = 0xffffffff;

function built(usize: number, csize: number, lho: number, refBlock: Buffer | null = null): Built {
  const name = Buffer.from("k");
  return {
    name, nameStr: "k", method: 0, crc: 0, csize, usize, body: new Uint8Array(0),
    refBlock, pinned: false, isRef: refBlock !== null, lho,
  };
}

const u64s = (...vs: number[]) => {
  const b = Buffer.alloc(8 * vs.length);
  vs.forEach((v, i) => b.writeBigUInt64LE(BigInt(v), 8 * i));
  return b;
};
const block = (data: Buffer) => {
  const h = Buffer.alloc(4);
  h.writeUInt16LE(1, 0);
  h.writeUInt16LE(data.length, 2);
  return Buffer.concat([h, data]);
};

test("local header and central directory record layout for every size/offset combination", () => {
  const big = 2 ** 33 + 7; // well past 4 GiB
  const ref = Buffer.from([0x76, 0x7a, 0, 0]);
  // [usize, csize, lho, refBlock, expected local extra, expected local version,
  //  expected CD (csize, usize, lho) fields, expected CD extra, expected CD version]
  const cases: [number, number, number, Buffer | null, Buffer, number, [number, number, number], Buffer, number][] = [
    [5, 5, 10, null, Buffer.alloc(0), 20, [5, 5, 10], Buffer.alloc(0), 20],
    [ALL - 1, ALL - 1, ALL - 1, null, Buffer.alloc(0), 20, [ALL - 1, ALL - 1, ALL - 1], Buffer.alloc(0), 20],
    [ALL, 7, 0, null, block(u64s(ALL, 7)), 45, [ALL, ALL, 0], block(u64s(ALL, 7)), 45],
    [7, ALL, 0, null, block(u64s(7, ALL)), 45, [ALL, ALL, 0], block(u64s(7, ALL)), 45],
    [big, big - 1, 3, null, block(u64s(big, big - 1)), 45, [ALL, ALL, 3], block(u64s(big, big - 1)), 45],
    [5, 5, ALL, null, Buffer.alloc(0), 20, [5, 5, ALL], block(u64s(ALL)), 45],
    [big, big, big, null, block(u64s(big, big)), 45, [ALL, ALL, ALL], block(u64s(big, big, big)), 45],
    [5, 5, big, ref, Buffer.alloc(0), 20, [5, 5, ALL], Buffer.concat([block(u64s(big)), ref]), 45],
  ];
  for (const [usize, csize, lho, refBlock, lx, lver, [c, u, o], cx, cver] of cases) {
    const b = built(usize, csize, lho, refBlock);
    const what = `usize=${usize} csize=${csize} lho=${lho}`;
    const lh = localHeader(b);
    assert.equal(lh.readUInt16LE(4), lver, what);
    assert.equal(lh.readUInt32LE(18), lx.length ? ALL : csize, what);
    assert.equal(lh.readUInt32LE(22), lx.length ? ALL : usize, what);
    assert.equal(lh.readUInt16LE(28), lx.length, what);
    assert.deepEqual(lh.subarray(30 + 1), lx, what);
    const cd = cdRecord(b);
    assert.equal(cd.readUInt16LE(6), cver, what);
    assert.deepEqual([cd.readUInt32LE(20), cd.readUInt32LE(24), cd.readUInt32LE(42)], [c, u, o], what);
    assert.equal(cd.readUInt16LE(30), cx.length, what);
    assert.deepEqual(cd.subarray(46 + 1), cx, what);
  }
});

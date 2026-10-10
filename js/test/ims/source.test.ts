// The datatype descriptions of the browser IMS virtualizer
// (spec/virtualize/ims.md §5.2); python/tests/test_virtualize_ims.py has the same cases.

import assert from "node:assert/strict";
import { test } from "node:test";
import { ImsError, parseSelection } from "../../src/virtualize/ims/hdf5.ts";
import { canonical } from "../../src/virtualize/ims/source.ts";
import { describeType } from "../../src/virtualize/ims/source.ts";

const INT32 = [0x10, 0x08, 0, 0, 4, 0, 0, 0, 0, 0, 32, 0];
const CASES: [number[], unknown][] = [
  [[0x10, 0, 0, 0, 2, 0, 0, 0, 0, 0, 16, 0], { class: "integer", size: 2, order: "little", signed: false }],
  [[0x10, 0x09, 0, 0, 4, 0, 0, 0, 0, 0, 32, 0], { class: "integer", size: 4, order: "big", signed: true }],
  [[0x10, 0, 0, 0, 2, 0, 0, 0, 4, 0, 12, 0],
    { class: "integer", size: 2, order: "little", signed: false, offset: 4, precision: 12, padding: [0, 0] }],
  [[0x11, 0x20, 0x1f, 0, 4, 0, 0, 0, 0, 0, 32, 0, 23, 8, 0, 23, 127, 0, 0, 0], { class: "float", size: 4, order: "little" }],
  [[0x13, 0x11, 0, 0, 5, 0, 0, 0], { class: "string", size: 5, padding: "null-padded", charset: "utf-8" }],
  [[0x15, 0x03, 0, 0, 3, 0, 0, 0, 0x61, 0x62, 0], { class: "opaque", size: 3, tag: "ab" }],
  [[0x36, 0x02, 0, 0, 8, 0, 0, 0, 0x69, 0, 0, ...INT32, 0x6a, 0, 4, ...INT32], {
    class: "compound", size: 8, members: [
      { name: "i", offset: 0, type: { class: "integer", size: 4, order: "little", signed: true } },
      { name: "j", offset: 4, type: { class: "integer", size: 4, order: "little", signed: true } },
    ],
  }],
  [[0x3a, 0, 0, 0, 24, 0, 0, 0, 2, 2, 0, 0, 0, 3, 0, 0, 0, ...INT32],
    { class: "array", size: 24, shape: [2, 3], base: { class: "integer", size: 4, order: "little", signed: true } }],
  [[0x19, 0x01, 0, 0, 16, 0, 0, 0, 0x10, 0, 0, 0, 1, 0, 0, 0, 0, 0, 8, 0], {
    class: "variable-length", size: 16, base: { class: "integer", size: 1, order: "little", signed: false },
    string: true, padding: "null-terminated", charset: "ascii",
  }],
];

test("describes each datatype class", () => {
  for (const [bytes, expected] of CASES) assert.deepEqual(describeType(new Uint8Array(bytes))[0], expected);
});

test("rejects an unknown datatype message version", () => {
  assert.throws(() => describeType(new Uint8Array([0x60, 0, 0, 0, 1, 0, 0, 0])), ImsError);
});

test("rejects an unknown datatype class", () => {
  assert.throws(() => describeType(new Uint8Array([0x1b, 0, 0, 0, 1, 0, 0, 0])), /unknown datatype class 11/);
});

test("rejects a truncated datatype", () => {
  assert.throws(() => describeType(new Uint8Array([0x10, 0, 0, 0, 2, 0, 0, 0, 0])), /truncated HDF5 structure/);
});

test("writes JSON texts", () => {
  const cases: [unknown, string][] = [
    [null, "null"], [true, "true"], [0, "0"], [-0, "0"], [2, "2"], [1.5, "1.5"], [1e-7, "1e-7"], [0.000001, "0.000001"],
    [1e20, "100000000000000000000"], [1e21, "1e+21"], [-1.25e-300, "-1.25e-300"], [0.1, "0.1"],
    [123456.789, "123456.789"], [2 ** 53, "9007199254740992"], [5e-324, "5e-324"],
    [1.2345678901234567e19, "12345678901234567000"], [-(2 ** 60), "-1152921504606847000"],
    ["a\"\\\n\u0001é", '"a\\"\\\\\\n\\u0001é"'], [[1, "x"], '[1,"x"]'],
    [{ b: 1, a: { é: [], z: null }, B: 2 }, '{"B":2,"a":{"z":null,"é":[]},"b":1}'],
  ];
  for (const [value, text] of cases) assert.equal(canonical(value), text);
});

const u32 = (...v: number[]) => v.flatMap((x) => [x & 0xff, (x >> 8) & 0xff, (x >> 16) & 0xff, (x >>> 24) & 0xff]);
const u64 = (...v: bigint[]) => v.flatMap((x) => Array.from({ length: 8 }, (_, i) => Number((x >> BigInt(8 * i)) & 0xffn)));

test("parses serialized selections", () => {
  const cases: [number[], unknown][] = [
    [u32(3, 1, 0, 0), { select: "all" }],
    [u32(0, 1, 0, 0), { select: "none" }],
    [u32(1, 1, 0, 0, 2, 2, 1, 2, 3, 4), { select: "points", rank: 2, points: [[1, 2], [3, 4]] }],
    [[...u32(1, 2), 2, ...u32(1), 1, 0, 5, 0], { select: "points", rank: 1, points: [[5]] }],
    [u32(2, 1, 0, 0, 1, 1, 2, 7), { select: "hyperslab", rank: 1, blocks: [[[2], [7]]] }],
    [[...u32(2, 2), 1, ...u32(0, 1), ...u64(1n, 2n, 3n, 2n ** 64n - 1n)],
      { select: "hyperslab", rank: 1, start: [1], stride: [2], count: [3], block: ["unlimited"] }],
    [[...u32(2, 3), 0, 8, ...u32(1), ...u64(1n, 2n ** 60n, 2n ** 60n + 1n)],
      { select: "hyperslab", rank: 1, blocks: [[[String(2n ** 60n)], [String(2n ** 60n + 1n)]]] }],
  ];
  for (const [data, selection] of cases) {
    assert.deepEqual(parseSelection(new Uint8Array([...data, 0x72, 0x65, 0x73, 0x74]), 0), [selection, data.length]);
  }
});

test("rejects an unknown selection type", () => {
  assert.throws(() => parseSelection(new Uint8Array([4, 0, 0, 0, 1, 0, 0, 0]), 0), /unknown selection type 4/);
});

test("rejects an unknown selection version", () => {
  assert.throws(() => parseSelection(new Uint8Array([2, 0, 0, 0, 4, 0, 0, 0]), 0), /unsupported selection version 4/);
});

test("rejects an unknown selection encoding size", () => {
  assert.throws(() => parseSelection(new Uint8Array([2, 0, 0, 0, 3, 0, 0, 0, 0, 3, 1, 0, 0, 0]), 0), /invalid selection encoding size 3/);
});

test("rejects unknown selection flags", () => {
  assert.throws(() => parseSelection(new Uint8Array([2, 0, 0, 0, 3, 0, 0, 0, 2, 8, 1, 0, 0, 0]), 0), /unknown selection flags/);
});

test("rejects a selection of rank 0", () => {
  // Points and an irregular hyperslab, whose count no bytes bound.
  for (const data of [[1, 0, 0, 0, 1, 0, 0, 0, ...new Array(8).fill(0), 0, 0, 0, 0, 255, 255, 255, 255],
    [2, 0, 0, 0, 3, 0, 0, 0, 0, 8, 0, 0, 0, 0, 0, 0, 0, 0, 4, 0, 0, 0]]) {
    assert.throws(() => parseSelection(new Uint8Array(data), 0), /a selection of rank 0/);
  }
});

test("rejects a truncated selection", () => {
  assert.throws(() => parseSelection(new Uint8Array([1, 0, 0, 0, 2, 0, 0, 0, 4, 1, 0, 0, 0, 9, 0, 0, 0]), 0), /truncated selection/);
});

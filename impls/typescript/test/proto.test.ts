import { test } from "node:test";
import assert from "node:assert/strict";
import {
  ProtoError, decodeCdIndex, decodeConcat, decodeRange, decodeSourceTable, encodeCdIndex, encodeConcat, encodeRange,
  encodeSourceTable,
} from "../src/proto.ts";

const h = (s: string) => Buffer.from(s.replace(/ /g, ""), "hex");

test("encode/decode round trips and canonical encodings", () => {
  // Source range with defaults omitted.
  assert.deepEqual(Buffer.from(encodeRange({ source: 0, offset: 0n, length: 4n, data: null })), h("20 04"));
  assert.deepEqual(Buffer.from(encodeRange({ source: 2, offset: 300n, length: 1n, data: null })), h("08 02 18 ac02 20 01"));
  // Literal range: field 5 always emitted, even when empty.
  assert.deepEqual(Buffer.from(encodeRange({ source: 0, offset: 0n, length: 0n, data: new Uint8Array() })), h("2a 00"));
  assert.deepEqual(Buffer.from(encodeConcat([])), h(""));
  // Source oneof member always emitted; optional pins emitted when set even if 0; negative int64 is 10 bytes.
  const st = encodeSourceTable([
    { kind: "url", url: "", size: 0n, etag: null, modifiedNotAfter: -1n },
    { kind: "data", data: new Uint8Array(), size: null, etag: null, modifiedNotAfter: null },
  ]);
  assert.deepEqual(Buffer.from(st), h("0a 0f 0a00 2000 30ffffffffffffffffff01 0a 02 1a00"));
  const back = decodeSourceTable(st);
  assert.equal(back[0].kind, "url");
  assert.equal(back[0].modifiedNotAfter, -1n);
  assert.equal(back[0].size, 0n);
  assert.equal(back[1].kind, "data");
  // Decoding rules: unknown fields skipped (all 4 wire types), reserved field 2 skipped, last wins,
  // non-minimal varints accepted, last oneof member wins.
  const r = decodeRange(h("10 05 08 01 08 03 4d 00000000 51 0000000000000000 72 01 ff 20 8480 00 18 07"));
  assert.deepEqual(r, { source: 3, offset: 7n, length: 4n, data: null });
  const s = decodeSourceTable(h("0a 06 0a 01 61 12 01 62"));
  assert.equal(s[0].kind, "key");
  assert.equal(s[0].key, "b");
  const idx = { pages: [{ firstKey: "a", offset: 0n, length: 50n }], pinned: [{ key: "z", dataOffset: 9n, size: 2n, csize: 2n, method: 0 }] };
  assert.deepEqual(decodeCdIndex(encodeCdIndex(idx)), idx);
  assert.deepEqual(decodeConcat(encodeConcat([{ source: 1, offset: 2n, length: 3n, data: null }])), [{ source: 1, offset: 2n, length: 3n, data: null }]);
});

test("malformed: group wire types 3/4 on unknown fields", () => {
  assert.throws(() => decodeRange(h("4b 4c")), ProtoError);
});
test("malformed: wire types 6 and 7", () => {
  assert.throws(() => decodeRange(h("4e")), ProtoError);
  assert.throws(() => decodeRange(h("4f")), ProtoError);
});
test("malformed: field number 0", () => {
  assert.throws(() => decodeRange(h("00 00")), ProtoError);
});
test("malformed: field number above 2^29-1", () => {
  assert.throws(() => decodeRange(h("80 80 80 80 10 00")), ProtoError);
});
test("malformed: truncated varint", () => {
  assert.throws(() => decodeRange(h("18 80")), ProtoError);
});
test("malformed: LEN past end", () => {
  assert.throws(() => decodeRange(h("2a 05 00")), ProtoError);
});
test("malformed: nested LEN past end of enclosing message", () => {
  assert.throws(() => decodeConcat(h("0a 02 2a 05 00 00 00 00 00")), ProtoError);
});
test("malformed: varint longer than 10 bytes", () => {
  assert.throws(() => decodeRange(h("18 80 80 80 80 80 80 80 80 80 80 00")), ProtoError);
});
test("malformed: varint above 2^64-1", () => {
  assert.throws(() => decodeRange(h("18 ff ff ff ff ff ff ff ff ff 02")), ProtoError);
});
test("malformed: known field with wrong wire type", () => {
  assert.throws(() => decodeRange(h("0a 00")), ProtoError);
  assert.throws(() => decodeRange(h("28 00")), ProtoError);
});
test("malformed: uint32 overflow", () => {
  assert.throws(() => decodeRange(h("08 80 80 80 80 10")), ProtoError);
});
test("malformed: invalid UTF-8 string", () => {
  assert.throws(() => decodeSourceTable(h("0a 03 0a 01 ff")), ProtoError);
});

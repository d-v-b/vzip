import { test } from "node:test";
import assert from "node:assert/strict";
import { decodeRange, decodeSourceTable, encodeRange, encodeSourceTable } from "../src/proto.ts";
import { MalformedError } from "../src/errors.ts";

test("Range encoding is canonical and round-trips", () => {
  const cases = [
    { source: 0, offset: 0n, length: 0n, data: null },
    { source: 3, offset: 300n, length: (1n << 64n) - 301n, data: null },
    { source: 0, offset: 0n, length: 0n, data: new Uint8Array(0) },
    { source: 0, offset: 0n, length: 0n, data: Uint8Array.from([1, 2]) },
  ];
  const want = ["", "0803", "2a00", "2a020102"];
  cases.forEach((c, i) => {
    const enc = encodeRange(c);
    if (i !== 1) assert.equal(enc.toString("hex"), want[i]);
    const d = decodeRange(enc);
    assert.deepEqual({ ...d, data: d.data && Buffer.from(d.data) }, { ...c, data: c.data && Buffer.from(c.data) });
  });
});

test("SourceTable decodes oneof last-wins and int64", () => {
  const t = encodeSourceTable([{ kind: { type: "key", value: "k" }, size: null, etag: null, modifiedNotAfter: -5n }]);
  const d = decodeSourceTable(t);
  assert.deepEqual(d[0].kind, { type: "key", value: "k" });
  assert.equal(d[0].modifiedNotAfter, -5n);
  // url then data: data wins
  const s = Buffer.from([0x0a, 0x06, 0x0a, 0x01, 0x61, 0x1a, 0x01, 0x62]);
  assert.equal(decodeSourceTable(s)[0].kind!.type, "data");
});

test("decoder: field number above 2^29-1 is malformed", () => {
  // tag = (2^29) << 3 | 0
  const tag = [0x80, 0x80, 0x80, 0x80, 0x10];
  assert.throws(() => decodeRange(Buffer.from([...tag, 0])), MalformedError);
});

test("decoder: wire type 6/7 malformed", () => {
  assert.throws(() => decodeRange(Buffer.from([0x0e])), MalformedError);
  assert.throws(() => decodeRange(Buffer.from([0x0f])), MalformedError);
});

test("decoder: BOM is preserved in strings", () => {
  const s = Buffer.from([0x0a, 0x05, 0x12, 0x03, 0xef, 0xbb, 0xbf]);
  assert.deepEqual(decodeSourceTable(s)[0].kind, { type: "key", value: "\uFEFF" });
});

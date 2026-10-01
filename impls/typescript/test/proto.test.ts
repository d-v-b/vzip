import { test } from "node:test";
import assert from "node:assert/strict";
import {
  decodeCdIndex,
  decodeConcat,
  decodeRange,
  decodeSourceTable,
  encodeCdIndex,
  encodeConcat,
  encodeRange,
  encodeSourceTable,
} from "../src/proto.ts";
import { MalformedError } from "../src/errors.ts";

const hex = (s: string) => Buffer.from(s.replace(/ /g, ""), "hex");

test("canonical encodings round-trip", () => {
  const cases: [Parameters<typeof encodeRange>[0], string][] = [
    [{ source: 0, offset: 0n, length: 0n }, ""],
    [{ source: 1, offset: 10n, length: 4n }, "0801 180a 2004"],
    [{ source: 0, offset: 0n, length: 0n, data: new Uint8Array() }, "2a00"],
    [{ source: 0, offset: 0n, length: 0n, data: Uint8Array.of(0, 0xff) }, "2a02 00ff"],
    [{ source: 0, offset: (1n << 64n) - 2n, length: 1n }, "18feffffffffffffffff01 2001"],
  ];
  for (const [r, h] of cases) {
    const enc = encodeRange(r);
    assert.equal(Buffer.from(enc).toString("hex"), h.replace(/ /g, ""));
    assert.deepEqual(decodeRange(enc), r);
  }
  const parts = [{ source: 0, offset: 0n, length: 0n }, { source: 0, offset: 0n, length: 0n, data: Uint8Array.of(1) }];
  assert.equal(Buffer.from(encodeConcat(parts)).toString("hex"), "0a00" + "0a032a0101");
  assert.deepEqual(decodeConcat(encodeConcat(parts)), parts);
  assert.equal(Buffer.from(encodeConcat([])).length, 0);

  const sources = [
    { kind: "url" as const, url: "a.bin", size: 0n, etag: '"x"', modifiedNotAfter: -1n },
    { kind: "key" as const, key: "" },
    { kind: "data" as const, data: new Uint8Array() },
  ];
  const st = encodeSourceTable(sources);
  // size 0 is an explicitly set optional: emitted as 2000
  assert.ok(Buffer.from(st).toString("hex").includes("2000"));
  const dec = decodeSourceTable(st);
  assert.equal(dec[0].size, 0n);
  assert.equal(dec[0].modifiedNotAfter, -1n);
  assert.equal(dec[1].kind, "key");
  assert.equal(dec[1].key, "");
  assert.equal(dec[2].kind, "data");

  const idx = { pages: [{ firstKey: "a", offset: 0n, length: 50n }], pinned: [{ key: "k", dataOffset: 7n, size: 1n, csize: 1n, method: 0 }] };
  assert.deepEqual(decodeCdIndex(encodeCdIndex(idx)), idx);

  // decoder leniency: unknown fields of every allowed wire type, reserved field 2,
  // non-minimal varints, repeated fields (last wins), any order
  const lenient = hex("2004 1000 1180 0000 0000 0000 00 1502000000 3a0161 4080 00 0805 0802 180a");
  assert.deepEqual(decodeRange(lenient), { source: 2, offset: 10n, length: 4n });
  // oneof: last member wins
  const s = decodeSourceTable(hex("0a08 0a0161 120162 1a00"));
  assert.equal(s[0].kind, "data");
});

const malformedRange: [string, string][] = [
  ["wire type 3", "0b"],
  ["wire type 4", "0c"],
  ["wire type 6", "0e"],
  ["wire type 7", "0f"],
  ["field number 0", "0001"],
  ["field number > 2^29-1", "80808080 10 00"],
  ["truncated varint", "0880"],
  ["truncated LEN", "2a05 00"],
  ["truncated I64", "11 0000"],
  ["varint longer than 10 bytes", "18 ffffffffffffffffff8100"],
  ["varint above 2^64-1", "18 ffffffffffffffffff02"],
  ["known field with wrong wire type", "0d01000000"],
  ["uint32 overflow", "08 8080808010"],
  ["literal with offset", "1801 2a00"],
  ["offset+length overflow", "18 ffffffffffffffffff01 2001"],
];
for (const [name, h] of malformedRange) {
  test(`malformed Range: ${name}`, () => {
    assert.throws(() => decodeRange(hex(h)), MalformedError);
  });
}

test("malformed SourceTable: invalid UTF-8 string", () => {
  assert.throws(() => decodeSourceTable(hex("0a03 0a01ff")), MalformedError);
});

test("malformed Concat: LEN field past end of embedded message", () => {
  assert.throws(() => decodeConcat(hex("0a02 2a05")), MalformedError);
});

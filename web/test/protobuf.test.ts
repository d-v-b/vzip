import assert from "node:assert/strict";
import { test } from "node:test";
import {
  decodeReference,
  decodeSourceTable,
  encodeConcat,
  encodeRange,
  encodeSourceTable,
  MalformedError,
} from "../src/protobuf.ts";

const hex = (b: Uint8Array) => Buffer.from(b).toString("hex");
const bytes = (h: string) => Uint8Array.from(Buffer.from(h, "hex"));

test("messages encode canonically and decode back", () => {
  const cases: [Parameters<typeof encodeRange>[0], string][] = [
    [{ source: 0, offset: 0n, length: 0n }, ""],
    [{ source: 1, offset: 300n, length: 5n }, "080118ac022005"],
    [{ data: new Uint8Array() }, "2a00"],
    [{ data: bytes("abcd") }, "2a02abcd"],
  ];
  for (const [range, want] of cases) {
    assert.equal(hex(encodeRange(range)), want);
    assert.deepEqual(decodeReference(0x7a76, encodeRange(range), 2), [range]);
  }
  // Every Concat part is emitted, even an empty one (spec §5.1).
  assert.equal(hex(encodeConcat([{ source: 0, offset: 0n, length: 0n }])), "0a00");
  assert.deepEqual(decodeReference(0x7a77, encodeConcat([]), 0), []);

  const sources = [
    { url: "a.bin", size: 0n, etag: '"x"', modifiedNotAfter: -1n },
    { key: "k" },
    { data: bytes("00") },
  ];
  const table = encodeSourceTable(sources);
  // int64 -1 takes 10 bytes.
  assert.ok(hex(table).includes("30ffffffffffffffffff01"));
  assert.deepEqual(decodeSourceTable(table), sources);
});

test("rejects a group wire type", () => {
  assert.throws(() => decodeReference(0x7a76, bytes("0b"), 1), MalformedError);
});

test("rejects a uint32 source overflow", () => {
  assert.throws(() => decodeReference(0x7a76, bytes("0880808080 10".replace(" ", "")), 1), MalformedError);
});

test("rejects a source index past the table", () => {
  assert.throws(() => decodeReference(0x7a76, bytes("0801"), 1), /source 1 >= 1/);
});

test("rejects a literal range with an offset", () => {
  assert.throws(() => decodeReference(0x7a76, bytes("18012a00"), 1), /literal range/);
});

test("rejects a varint longer than 10 bytes", () => {
  assert.throws(() => decodeReference(0x7a76, bytes("18ffffffffffffffffffff01"), 1), /longer than 10/);
});

test("rejects a truncated LEN field", () => {
  assert.throws(() => decodeReference(0x7a76, bytes("2a05ab"), 1), /past the message/);
});

test("rejects an invalid UTF-8 url", () => {
  assert.throws(() => decodeSourceTable(bytes("0a030a01ff")), /UTF-8/);
});

test("rejects a pin on a key source", () => {
  assert.throws(() => decodeSourceTable(bytes("0a05120161 2001".replace(" ", ""))), /pin on a key/);
});

test("rejects a weak etag", () => {
  const weak = encodeSourceTable([{ url: "a", etag: 'W/"x"' }]);
  assert.throws(() => decodeSourceTable(weak), /strong entity tag/);
});

// The DICOM JSON Model translation of the browser virtualizer
// (conventions/dicom/README.md §5); tests/test_virtualize_dicom.py has the same cases.

import assert from "node:assert/strict";
import { test } from "node:test";
import { valueJson } from "../../src/virtualize/dicom/source.ts";

const bytes = (s: string) => Uint8Array.from(s, (c) => c.charCodeAt(0));
const le = (fmt: "H" | "d" | "Q", ...v: (number | bigint)[]) => {
  const size = { H: 2, d: 8, Q: 8 }[fmt];
  const b = new DataView(new ArrayBuffer(size * v.length));
  v.forEach((x, i) => {
    if (fmt === "H") b.setUint16(2 * i, x as number, true);
    if (fmt === "d") b.setFloat64(8 * i, x as number, true);
    if (fmt === "Q") b.setBigUint64(8 * i, x as bigint, true);
  });
  return new Uint8Array(b.buffer);
};

test("each VR's translation", () => {
  const be = new Uint8Array([0, 1, 0xff, 0xff, 1]);
  const cases: [string, Uint8Array, boolean, unknown][] = [
    ["CS", bytes("ORIGINAL\\PRIMARY "), true, { vr: "CS", Value: ["ORIGINAL", "PRIMARY"] }],
    ["UI", bytes("1.2.3\0"), true, { vr: "UI", Value: ["1.2.3"] }],
    ["LO", bytes(" a\\\\b "), true, { vr: "LO", Value: ["a", null, "b"] }],
    ["LT", bytes(" two\\lines "), true, { vr: "LT", Value: [" two\\lines"] }],
    ["PN", bytes("Doe^Jane==ja^ne"), true, { vr: "PN", Value: [{ Alphabetic: "Doe^Jane", Phonetic: "ja^ne" }] }],
    ["DS", bytes("0.50\\1e999\\x "), true, { vr: "DS", Value: [0.5, "1e999", "x"] }],
    ["IS", bytes("+12\\-9007199254740993"), true, { vr: "IS", Value: [12, "-9007199254740993"] }],
    ["SH", bytes("caf\xe9"), true, { vr: "SH", Value: ["café"] }],
    ["AT", le("H", 0x0028, 0x0010, 0x7fe0, 0x0010), true, { vr: "AT", Value: ["00280010", "7FE00010"] }],
    ["US", be, false, { vr: "US", Value: [1, 65535] }],
    ["FD", le("d", 1.5, NaN), true, { vr: "FD", Value: [1.5, "NaN"] }],
    ["UV", le("Q", 2n ** 64n - 1n), true, { vr: "UV", Value: ["18446744073709551615"] }],
    ["OB", new Uint8Array([0, 1]), true, { vr: "OB", InlineBinary: "AAE=" }],
    ["ST", new Uint8Array(), true, { vr: "ST" }],
  ];
  for (const [vr, data, little, expected] of cases) assert.deepEqual(valueJson(vr, data, little), expected, vr);
});

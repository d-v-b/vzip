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
  const be = new Uint8Array([0, 1, 0xff, 0xff]);
  const cases: [string, Uint8Array, boolean, unknown][] = [
    ["CS", bytes("ORIGINAL\\PRIMARY "), true, { vr: "CS", Value: ["ORIGINAL", "PRIMARY"] }],
    ["UI", bytes("1.2.3\0"), true, { vr: "UI", Value: ["1.2.3"] }],
    ["LO", bytes(" a\\\\b "), true, { vr: "LO", Value: ["a", null, "b"] }],
    ["LT", bytes(" two\\lines "), true, { vr: "LT", Value: [" two\\lines"] }],
    ["PN", bytes("Doe^Jane==ja^ne"), true, { vr: "PN", Value: [{ Alphabetic: "Doe^Jane", Phonetic: "ja^ne" }] }],
    ["DS", bytes("0.50\\1e999\\x "), true, { vr: "DS", Value: [0.5, "1e999", "x"] }],
    // A number only when it is the text's decimal value exactly.
    ["DS", bytes("9007199254740993\\9007199254740992\\-0\\1.10E+2\\.1\\0.1000000000000000055511151231257827"), true,
      { vr: "DS", Value: ["9007199254740993", 9007199254740992, -0, 110, 0.1, "0.1000000000000000055511151231257827"] }],
    ["IS", bytes("+12\\-9007199254740993"), true, { vr: "IS", Value: [12, "-9007199254740993"] }],
    // Too long for Python's int(): by its digits without sign and leading zeros.
    ["IS", bytes(`-00042\\${"0".repeat(5000)}12345678901234567\\+${"0".repeat(5000)}\\-${"9".repeat(5000)}`), true,
      { vr: "IS", Value: [-42, "12345678901234567", 0, `-${"9".repeat(5000)}`] }],
    // An exponent of over 5 digits, without its sign and leading zeros: out of range unless the digits are 0.
    ["DS", bytes(`1e${"0".repeat(5000)}1\\-2.5E+${"0".repeat(4999)}1\\1e-111111\\0.0e999999999\\1e99999`), true,
      { vr: "DS", Value: [10, -25, "1e-111111", 0, "1e99999"] }],
    ["SH", bytes("caf\xe9"), true, { vr: "SH", Value: [{ latin1: "café" }] }],
    ["AT", le("H", 0x0028, 0x0010, 0x7fe0, 0x0010), true, { vr: "AT", Value: ["00280010", "7FE00010"] }],
    ["US", be, false, { vr: "US", Value: [1, 65535] }],
    ["US", new Uint8Array([0, 1, 2]), false, { vr: "US", InlineBinary: "AAEC" }], // not whole values: as stored
    ["AT", new Uint8Array([0x28, 0, 0x10, 0, 0x28, 0]), true, { vr: "AT", InlineBinary: "KAAQACgA" }],
    ["FD", le("d", 1.5, NaN), true, { vr: "FD", Value: [1.5, "NaN"] }],
    ["UV", le("Q", 2n ** 64n - 1n), true, { vr: "UV", Value: ["18446744073709551615"] }],
    ["OB", new Uint8Array([0, 1]), true, { vr: "OB", InlineBinary: "AAE=" }],
    ["ST", new Uint8Array(), true, { vr: "ST" }],
  ];
  for (const [vr, data, little, expected] of cases) assert.deepEqual(valueJson(vr, data, little), expected, vr);
  // Under a multibyte character set, the values of LO, PN, SH and UC are their bytes, unsplit.
  const gbk = new Uint8Array([0x81, 0x5c, 0x5e, 0x78]);
  assert.deepEqual(valueJson("PN", gbk, true, true), { vr: "PN", InlineBinary: "gVxeeA==" });
  assert.deepEqual(valueJson("CS", bytes("A\\B"), true, true), { vr: "CS", Value: ["A", "B"] });
});

test("the dictionary's VR for an element whose file does not state it", async () => {
  const { dictionaryVr } = await import("../../src/virtualize/dicom/source.ts");
  const cases: [number, string | undefined][] = [
    [0x00280010, "US"], [0x00081140, "SQ"], [0x60000010, "US"], [0x60020010, "US"], [0x00290010, "LO"],
    [0x00291010, undefined], [0x00280106, undefined], [0x7fe00010, undefined], [0x0ff00100, undefined],
  ];
  for (const [tag, vr] of cases) assert.equal(dictionaryVr(tag), vr, tag.toString(16));
});

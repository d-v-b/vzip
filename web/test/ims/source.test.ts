// The attribute translation of the browser IMS virtualizer
// (conventions/ims/README.md §5); tests/test_virtualize_ims.py has the same cases.

import assert from "node:assert/strict";
import { test } from "node:test";
import { valueJson } from "../../src/virtualize/ims/source.ts";

const bytes = (s: string) => Uint8Array.from(s, (c) => c.charCodeAt(0));
function packed(size: number, le: boolean, set: (v: DataView) => void): Uint8Array {
  const b = new DataView(new ArrayBuffer(size));
  set(b);
  return new Uint8Array(b.buffer);
}

test("each datatype class's translation", () => {
  const cases: [[number, number, number, Uint8Array], unknown][] = [
    [[3, 1, 0, bytes("5.5\0")], "5.5"],
    [[3, 1, 0, bytes("\xb5m\0")], "µm"],
    [[3, 4, 0, bytes("ab\0\0cd\0\0")], ["ab", "cd"]],
    [[0, 2, 8, packed(4, true, (v) => { v.setInt16(0, -1, true); v.setInt16(2, 7, true); })], [-1, 7]],
    [[0, 4, 1, packed(4, false, (v) => v.setUint32(0, 7, false))], [7]],
    [[0, 8, 0, packed(8, true, (v) => v.setBigUint64(0, 2n ** 64n - 1n, true))], ["18446744073709551615"]],
    [[1, 8, 0, packed(16, true, (v) => { v.setFloat64(0, 0.5, true); v.setFloat64(8, Infinity, true); })], [0.5, "Infinity"]],
    [[6, 2, 0, new Uint8Array([1, 2])], { class: 6, size: 2, data: "AQI=" }],
  ];
  for (const [[cls, size, bits, data], expected] of cases) assert.deepEqual(valueJson(cls, size, bits, data), expected);
});

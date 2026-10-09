// Edge cases of the source metadata that once differed from the Python
// implementation (spec/conventions.md §6).

import assert from "node:assert/strict";
import { test } from "node:test";
import { setMember } from "../src/virtualize/common.ts";
import { decodeLV } from "../../conformance/virtualize/reference/ts/nd2/lv.ts";
import { Translator } from "../../conformance/virtualize/reference/ts/tiff/tags.ts";

test("an ND2 name or string keeps a leading byte order mark", async () => {
  const name = new Uint8Array([0xff, 0xfe, 0x61, 0x00, 0x00, 0x00]); // U+FEFF "a" NUL
  const value = new Uint8Array([0xff, 0xfe, 0x62, 0x00, 0x00, 0x00]); // U+FEFF "b" NUL
  const record = new Uint8Array([8, 3, ...name, ...value]);
  const lv = await decodeLV(record);
  assert.deepEqual([...lv.entries()].map(([k, v]) => [k, (v as { value: string }).value]), [["﻿a", "﻿b"]]);
});

test("an NDPI LONG above 2^53 is a decimal string", async () => {
  const t = new Translator(async () => new Uint8Array(), 0, true);
  const ifd = await t.ifd([{ tag: 297, type: 4, count: 1n, inline: new Uint8Array(4), value: [(1n << 54n) + 3n] }], "ifds/0");
  assert.deepEqual((ifd.tags as Record<string, unknown>)["297"], { type: 4, count: 1, value: ["18014398509481987"] });
});

test("a member named __proto__ is a member", () => {
  const o: Record<string, unknown> = {};
  setMember(o, "__proto__", 1);
  assert.equal(JSON.stringify(o), '{"__proto__":1}');
});

test("an ND2 XML variant document as JSON", async () => {
  const { variantJson } = await import("../../conformance/virtualize/reference/ts/nd2/source.ts");
  const enc = (s: string) => new TextEncoder().encode(s);
  const doc = enc('<?xml version="1.0"?><variant version="1.0"><a runtype="CLxListVariant">'
    + '<i runtype="lx_int32" value="-7"/><u runtype="lx_uint64" value="18446744073709551615"/>'
    + '<d runtype="double" value="2.5"/><b runtype="bool" value="true"/><s runtype="CLxStringW" value="x &amp; y"/>'
    + '<bad runtype="lx_int32" value="7.5"/><e runtype="CLxListVariant"></e></a></variant>');
  assert.deepEqual(variantJson(doc), {
    a: { i: -7, u: { int: "18446744073709551615" }, d: 2.5, b: true, s: "x & y", bad: "7.5", e: {} },
  });
  for (const bad of ["<variant><a></b></variant>", "<other/>", "<variant/><variant/>"]) {
    assert.equal(variantJson(enc(bad)), undefined, bad);
  }
  assert.equal(variantJson(new Uint8Array([0xff, ...enc("<variant/>")])), undefined);
});

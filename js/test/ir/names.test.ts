// The root and the names (conventions §8.1) in the browser host: a crafted IR, loaded
// into the core's test module (rust/vzip-ir/target/web-test, built with the test-only
// hooks by `just js::wasm-test`), mirrors when its root is a struct named "" spanning
// the source and its paths are its own; otherwise the mirror is a rejection with the
// checker's message, as Python's and Rust's tests have it.

import assert from "node:assert/strict";
import fs from "node:fs";
import { test } from "node:test";
import { ImageError } from "../../src/virtualize/common.ts";
import { rejection } from "../../src/virtualize/ir/run.ts";
import { type IrExports, put } from "../../src/virtualize/ir/wasm.ts";

const TEST_WASM = new URL("../../../rust/vzip-ir/target/web-test/vzip_ir.wasm", import.meta.url);

type TestExports = IrExports & { vz_test_mirror(json: number, n: number, profile: number): bigint };

async function exports(): Promise<TestExports> {
  const module = await WebAssembly.compile(fs.readFileSync(TEST_WASM));
  return (await WebAssembly.instantiate(module, {})).exports as unknown as TestExports;
}

/** A 30-byte source: the root, `a` (a value of 10 bytes) and `gaps/10` (a gap of 20), with `edit` applied. */
function ir(edit: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    size: 30, kind: [0, 1, 5], parent: [-1, 0, 0], name: ["", "a", "gaps/"], nidx: [-1, -1, 10],
    start: [0, 0, 10], len: [30, 10, 20], runs: [], ...edit,
  };
}

/** The mirror's result: its output's length (> 0), or the rejection. */
async function mirrored(table: Record<string, unknown>): Promise<number | ImageError> {
  const w = await exports();
  const b = new TextEncoder().encode(JSON.stringify(table));
  const r = Number(w.vz_test_mirror(put(w, b), b.length, 1));
  return r < 0 ? rejection(w, r) : r;
}

test("an IR whose root is \"\" spanning the source, and whose paths are its own, mirrors", async () => {
  const r = await mirrored(ir());
  assert.ok(typeof r === "number" && r > 0, String(r));
});

test("a named root is a rejection", async () => {
  const e = await mirrored(ir({ name: ["r", "a", "gaps/"] }));
  assert.ok(e instanceof ImageError);
  assert.equal(e.message, 'the root (element 0) is named "r", not ""');
});

test("a root that does not span the source is a rejection", async () => {
  const e = await mirrored(ir({ len: [0, 10, 20] }));
  assert.ok(e instanceof ImageError);
  assert.equal(e.message, "the root's extent is (0, 0), not (0, 30): the root spans the source");
});

test("an empty full name is a rejection", async () => {
  const e = await mirrored(ir({ name: ["", "", "gaps/"] }));
  assert.ok(e instanceof ImageError);
  assert.equal(e.message, 'element 1 (value "") has an empty full name');
});

test("siblings of one path are a rejection", async () => {
  const e = await mirrored(ir({ name: ["", "a", "a"], nidx: [-1, -1, -1] }));
  assert.ok(e instanceof ImageError);
  assert.equal(e.message, 'element 1 (value "a") and element 2 (gap "a") have one path, "a"');
});

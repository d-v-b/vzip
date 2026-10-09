// The mirror's row budget in the browser host (conventions §8.3): a crafted IR, loaded
// into the core's test module (rust/vzip-ir/target/web-test, built with the test-only
// hooks by `just js::wasm-test`), whose mirror would have more rows than
// 2^22 + floor(size / 4), is a rejection with the exact message, as Python's and
// Rust's tests have it. The shipped module has no such hook.

import assert from "node:assert/strict";
import fs from "node:fs";
import { test } from "node:test";
import { ImageError } from "../../src/virtualize/common.ts";
import { rejection } from "../../src/virtualize/ir/run.ts";
import { type IrExports, put } from "../../src/virtualize/ir/wasm.ts";
import { WASM_URL } from "../../src/virtualize/ir/wasm_node.ts";

const TEST_WASM = new URL("../../../rust/vzip-ir/target/web-test/vzip_ir.wasm", import.meta.url);

type TestExports = IrExports & { vz_test_mirror(json: number, n: number, profile: number): bigint };

async function load(url: URL): Promise<WebAssembly.Instance> {
  const module = await WebAssembly.compile(fs.readFileSync(url));
  return WebAssembly.instantiate(module, {});
}

test("a mirror past the row budget is a rejection with the budget's message", async () => {
  const w = (await load(TEST_WASM)).exports as unknown as TestExports;
  // a 4-byte source: the root, a gap, and a struct standing for 4,200,000 members
  const ir = {
    size: 4, kind: [0, 5, 0], parent: [-1, 0, 0], name: ["", "gaps/", "s"], nidx: [-1, -1, 0],
    start: [0, 0, 0], len: [0, 4, 0], runs: [[2, 4_200_000, 0]],
  };
  const b = new TextEncoder().encode(JSON.stringify(ir));
  const r = Number(w.vz_test_mirror(put(w, b), b.length, 1));
  assert.ok(r < 0);
  const e = rejection(w, r);
  assert.ok(e instanceof ImageError);
  assert.equal(e.message, "budget: the mirror would have more than 4194305 rows");
});

test("the shipped module has no test hook", async () => {
  const m = await WebAssembly.compile(fs.readFileSync(WASM_URL));
  const names = WebAssembly.Module.exports(m).map((x) => x.name);
  assert.ok(names.includes("vz_run_output"));
  assert.ok(!names.some((n) => n.startsWith("vz_test_")), names.join(" "));
});

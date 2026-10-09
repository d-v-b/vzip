// The cutting of contiguous values into chunks (spec/conventions.md §7, "Contiguous
// values"); python/tests/test_virtualize_chunks.py has the same cases.

import assert from "node:assert/strict";
import { test } from "node:test";
import { gridChunks, planGrid, rowChunks } from "../src/virtualize/common.ts";

const seven = async (_o: number, n: number) => new Uint8Array(n).fill(7);
const MAX_CHUNK = 2 ** 24;

test("cuts contiguous values into bounded, balanced chunks", async () => {
  assert.deepEqual(planGrid(100, [3, 4], 2).slice(0, 2), [[3, 4], new Map([["0/0", [[100, 24]]]])]);
  assert.deepEqual(planGrid(100, [], 8).slice(0, 2), [[], new Map([["", [[100, 8]]]])]);
  assert.deepEqual(planGrid(100, [0, 5], 1).slice(0, 2), [[1, 5], new Map()]);
  let [shape, chunks] = planGrid(0, [3, 2 ** 23 + 1], 1);
  assert.deepEqual([shape, [...chunks.keys()]], [[1, 2 ** 23 + 1], ["0/0", "1/0", "2/0"]]);
  [shape, chunks] = planGrid(10, [1, 40_000_000], 1);
  assert.deepEqual([shape, chunks.get("0/2")], [[1, 13_333_334], [[10 + 2 * 13_333_334, 13_333_332], new Uint8Array(2)]]);
  assert.deepEqual(planGrid(0, [599_326, 2048], 1)[0], [8099, 2048]);
  [shape, chunks] = planGrid(0, [599_327, 2048], 1);
  assert.deepEqual([shape, chunks.size], [[6243, 2048], 96]);
  [shape, chunks] = planGrid(0, [5, 2 ** 23], 1);
  assert.deepEqual([shape, chunks.size], [[1, 2 ** 23], 5]);
  [shape, chunks] = planGrid(0, [1009, 100_000], 1);
  assert.deepEqual([shape, chunks.size], [[1, 100_000], 1009]);
  const [copyShape, copied] = await gridChunksNoDeepen(0, [1009, 100_000], 1);
  const last = copied.get("6/0") as Uint8Array;
  assert.deepEqual([copyShape, last.length, last[0], last[139 * 100_000]], [[145, 100_000], 145 * 100_000, 7, 0]);
  [shape, chunks] = planGrid(0, [100, 64, 64], 2, true, 2 ** 17);
  assert.deepEqual([shape, chunks.size], [[10, 64, 64], 10]);
  assert.deepEqual(rowChunks(0, 10, 4, "/0"), [10, new Map([["0/0", [[0, 40]]]])]);
  assert.deepEqual((await gridChunks(0, [5, 2 ** 23], 1, seven))[0], [1, 2 ** 23]);
});

async function gridChunksNoDeepen(offset: number, shape: number[], item: number) {
  const [chunkShape, chunks, large] = planGrid(offset, shape, item, false);
  const out = new Map<string, unknown>(chunks);
  for (const key of large) {
    const [[at, n], pad] = chunks.get(key)! as [[number, number], Uint8Array];
    const copy = new Uint8Array(n + pad.length);
    copy.set(await seven(at, n));
    out.set(key, copy);
  }
  return [chunkShape, out] as const;
}

test("rejects an element over the limit", () => {
  assert.throws(() => planGrid(0, [2], MAX_CHUNK + 1), new RegExp(`element of more than ${MAX_CHUNK} bytes`));
});

test("rejects a padding to copy without a reader", () => {
  assert.throws(() => rowChunks(0, 1009, 100_000), /padding does not fit/);
});

test("rejects a row over 2^24 bytes", () => {
  assert.throws(() => rowChunks(0, 2, MAX_CHUNK + 1), /row of more than 2/);
});

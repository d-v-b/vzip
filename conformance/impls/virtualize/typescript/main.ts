// virtualize <url> <out.json>  (conformance/virtualize/HARNESS.md)

import { writeFileSync } from "node:fs";
import { Output, Reject, Source, reject } from "./lib.ts";
import { virtualizeTiff } from "./tiff.ts";
import { virtualizeNd2 } from "./nd2.ts";

async function run(url: string): Promise<Output> {
  const src = await Source.open(url);
  const out = new Output(src.size);
  if (src.size < 4) reject("file too short");
  const head = await src.read(0, 4);
  const hex = Array.from(head, (b) => b.toString(16).padStart(2, "0")).join("");
  if (["49492a00", "4d4d002a", "49492b00", "4d4d002b"].includes(hex)) await virtualizeTiff(src, out);
  else if (hex === "dacebe0a") await virtualizeNd2(src, out);
  else reject(`unrecognized file signature ${hex}`);
  return out;
}

const [url, outPath] = process.argv.slice(2);
if (url === undefined || outPath === undefined) {
  process.stderr.write("usage: virtualize <url> <out.json>\n");
  process.exit(2);
}
try {
  const out = await run(url);
  const entries: Record<string, unknown> = {};
  for (const [k, v] of out.entries) entries[k] = v;
  writeFileSync(outPath, JSON.stringify({ sources: [url], entries }));
  process.stdout.write(`${out.entries.size} entries\n`);
  process.exit(0);
} catch (e) {
  if (e instanceof Reject) {
    process.stderr.write(`rejected: ${e.message}\n`);
    process.exit(3);
  }
  process.stderr.write(`error: ${(e as Error).stack ?? e}\n`);
  process.exit(1);
}

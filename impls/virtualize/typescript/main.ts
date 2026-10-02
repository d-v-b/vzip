// virtualize <url> <out.json>  (conformance/virtualize/HARNESS.md)

import { writeFileSync } from "node:fs";
import { Output, Reject, Source, reject } from "./io.ts";
import { virtualizeNd2 } from "./nd2.ts";
import { virtualizeTiff } from "./tiff.ts";

async function main(): Promise<number> {
  const [url, outPath] = process.argv.slice(2);
  if (!url || !outPath) {
    process.stderr.write("usage: virtualize <url> <out.json>\n");
    return 2;
  }
  try {
    const src = await Source.open(url);
    const out = new Output(src.size);
    const head = await src.read(0, Math.min(4, src.size));
    const hx = Buffer.from(head).toString("hex");
    if (["49492a00", "4d4d002a", "49492b00", "4d4d002b"].includes(hx)) await virtualizeTiff(src, out);
    else if (hx === "dacebe0a") await virtualizeNd2(src, out);
    else reject(`unrecognized file signature ${hx}`);
    writeFileSync(outPath, JSON.stringify({ sources: [url], entries: out.entries }));
    process.stdout.write(`${Object.keys(out.entries).length} entries\n`);
    return 0;
  } catch (e) {
    if (e instanceof Reject) {
      process.stderr.write(`rejected: ${e.message}\n`);
      return 3;
    }
    process.stderr.write(`failed: ${(e as Error)?.stack ?? e}\n`);
    return 1;
  }
}

process.exitCode = await main();

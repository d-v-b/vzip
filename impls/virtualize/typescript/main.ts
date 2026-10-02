// virtualize <url> <out.json>  (conformance/virtualize/HARNESS.md)

import { writeFileSync } from "node:fs";
import { Reader, Output, Reject } from "./util.ts";
import { virtualizeTiff } from "./tiff.ts";
import { virtualizeNd2, isNd2 } from "./nd2.ts";

async function main(): Promise<number> {
  const args = process.argv.slice(2);
  if (args.length !== 2) {
    process.stderr.write("usage: virtualize <url> <out.json>\n");
    return 2;
  }
  const [url, outPath] = args;
  const r = new Reader(url);
  await r.init();
  const out = new Output();
  try {
    const head = await r.read(0, Math.min(r.size, 16));
    const isTiff = head.length >= 4 && ((head[0] === 0x49 && head[1] === 0x49) || (head[0] === 0x4d && head[1] === 0x4d));
    if (isTiff) await virtualizeTiff(r, url, out);
    else if (isNd2(head)) await virtualizeNd2(r, url, out);
    else throw new Reject("input is neither TIFF nor ND2");
  } catch (e) {
    if (e instanceof Reject) {
      process.stderr.write(`rejected: ${e.message}\n`);
      return 3;
    }
    throw e;
  }
  const entries: Record<string, unknown> = {};
  for (const k of [...out.entries.keys()].sort()) entries[k] = out.entries.get(k);
  writeFileSync(outPath, JSON.stringify({ sources: [url], entries }));
  process.stdout.write(`${out.entries.size} entries, ${r.requests} requests\n`);
  return 0;
}

main().then(
  (code) => process.exit(code),
  (e) => {
    process.stderr.write(`error: ${(e as Error).stack ?? e}\n`);
    process.exit(1);
  },
);

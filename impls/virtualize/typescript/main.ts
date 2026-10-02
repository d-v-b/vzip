// virtualize <url> <out.json>  (conformance/virtualize/HARNESS.md)
import { writeFileSync } from "node:fs";
import { Output, Reject, Source, reject } from "./io.ts";
import { virtualizeTiff } from "./tiff.ts";
import { virtualizeNd2 } from "./nd2.ts";

async function main(): Promise<number> {
  const [url, outPath] = process.argv.slice(2);
  if (!url || !outPath) {
    process.stderr.write("usage: virtualize <url> <out.json>\n");
    return 2;
  }
  const src = new Source(url);
  await src.open();
  try {
    const out = new Output(url, src.size);
    if (src.size < 4) reject("file shorter than 4 bytes");
    const m = await src.read(0, 4);
    const hex = Buffer.from(m).toString("hex");
    if (["49492a00", "4d4d002a", "49492b00", "4d4d002b"].includes(hex)) await virtualizeTiff(src, out);
    else if (hex === "dacebe0a") await virtualizeNd2(src, out);
    else reject(`unrecognized file signature ${hex}`);
    writeFileSync(outPath, out.serialize());
    process.stdout.write(`${Object.keys(out.entries).length} entries, ${src.requests} requests\n`);
    return 0;
  } catch (e) {
    if (e instanceof Reject) {
      process.stderr.write(`rejected: ${e.message}\n`);
      return 3;
    }
    throw e;
  }
}

main().then(
  (code) => process.exit(code),
  (e) => {
    process.stderr.write(`error: ${e instanceof Error ? e.stack : String(e)}\n`);
    process.exit(1);
  },
);

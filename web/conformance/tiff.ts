// Virtualizes a local TIFF (or NDPI) with the browser code, under Node:
//   node web/conformance/tiff.ts <tiff> <out.vzip> <source url>
// Prints the summary as JSON; exits 1 if the TIFF is not supported, 2 on a crash.

import fs from "node:fs";
import { blockReader, TiffError } from "../src/tiff.ts";
import { ImageError, virtualizeImage } from "../src/image.ts";
import { writeVzip } from "../src/writer.ts";

const [tiffPath, outPath, url] = process.argv.slice(2);
const bytes = new Uint8Array(fs.readFileSync(tiffPath));
try {
  const read = blockReader(async (o, n) => bytes.subarray(o, o + n), bytes.length);
  const virtual = await virtualizeImage(url, read, bytes.length);
  fs.writeFileSync(outPath, await writeVzip(virtual));
  console.log(JSON.stringify(virtual.summary));
} catch (e) {
  if (!(e instanceof TiffError || e instanceof ImageError)) {
    console.error(e);
    process.exit(2); // a crash, not a refusal
  }
  console.error(`unsupported: ${e.message}`);
  process.exitCode = 1;
}

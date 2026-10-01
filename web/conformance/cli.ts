// `write` command of the conformance harness (conformance/HARNESS.md) for the
// browser writer, run under Node:
//   node web/conformance/cli.ts write <description.json> <out.vzip>
// Descriptions with a page index are refused: this writer does not write one.

import fs from "node:fs";
import type { Range, Source } from "../src/protobuf.ts";
import { type EntryDesc, InvalidInputError, writeVzip } from "../src/writer.ts";
import { decodeJsonBytes, isNum, isObj, JsonError, parseJson, type JValue } from "./json.ts";

const bad = (m: string): never => {
  throw new InvalidInputError(m);
};

function hexBytes(v: JValue | undefined, what: string): Uint8Array {
  if (typeof v !== "string" || !/^(?:[0-9a-f]{2})*$/.test(v)) {
    bad(`${what} must be a lowercase even-length hex string`);
  }
  return Uint8Array.from(Buffer.from(v as string, "hex"));
}

function int(v: JValue | undefined, what: string, allowNegative = false): bigint {
  if (!isNum(v) || v.int === null) bad(`${what} must be a JSON integer`);
  const n = (v as { int: bigint }).int;
  if (!allowNegative && n < 0n) bad(`${what} must not be negative`);
  return n;
}

function bool(v: JValue | undefined, what: string): boolean {
  if (typeof v !== "boolean") bad(`${what} must be a boolean`);
  return v as boolean;
}

function parseDescription(v: JValue) {
  if (!isObj(v)) bad("description must be an object");
  const d = v as { [k: string]: JValue };
  if ("page_size" in d && d.page_size !== null) {
    int(d.page_size, "page_size");
    bad("page indexes are not supported by this writer");
  }
  const mirror = "mirror" in d ? bool(d.mirror, "mirror") : true;
  let sources: Source[] = [];
  if ("sources" in d) {
    if (!Array.isArray(d.sources)) bad("sources must be an array");
    sources = (d.sources as JValue[]).map((s, i): Source => {
      if (!isObj(s)) bad(`source ${i} must be an object`);
      const o = s as { [k: string]: JValue };
      const src: Source = {};
      if ("url" in o) {
        if (typeof o.url !== "string") bad(`source ${i}: url must be a string`);
        src.url = o.url as string;
      }
      if ("key" in o) {
        if (typeof o.key !== "string") bad(`source ${i}: key must be a string`);
        src.key = o.key as string;
      }
      if ("data" in o) src.data = hexBytes(o.data, `source ${i} data`);
      if ("size" in o) src.size = int(o.size, `source ${i} size`);
      if ("etag" in o) {
        if (typeof o.etag !== "string") bad(`source ${i}: etag must be a string`);
        src.etag = o.etag as string;
      }
      if ("modified_not_after" in o) {
        src.modifiedNotAfter = int(o.modified_not_after, `source ${i} modified_not_after`, true);
      }
      return src;
    });
  }
  let entries: EntryDesc[] = [];
  if ("entries" in d) {
    if (!Array.isArray(d.entries)) bad("entries must be an array");
    entries = (d.entries as JValue[]).map((e, i): EntryDesc => {
      if (!isObj(e)) bad(`entry ${i} must be an object`);
      const o = e as { [k: string]: JValue };
      if (typeof o.key !== "string") bad(`entry ${i}: key must be a string`);
      const key = o.key as string;
      const compress = "compress" in o ? bool(o.compress, `entry ${i} compress`) : false;
      if ("pinned" in o && bool(o.pinned, `entry ${i} pinned`)) {
        bad(`entry ${i}: pinned entries need a page index`);
      }
      if (("bytes" in o) === ("ranges" in o)) bad(`entry ${i}: needs exactly one of bytes, ranges`);
      if ("bytes" in o) return { key, bytes: hexBytes(o.bytes, `entry ${i} bytes`), compress };
      if (compress) bad(`entry ${i}: compress on a reference entry`);
      if (!Array.isArray(o.ranges)) bad(`entry ${i}: ranges must be an array`);
      const ranges = (o.ranges as JValue[]).map((r, j): Range => {
        if (!isObj(r)) bad(`entry ${i} range ${j} must be an object`);
        const ro = r as { [k: string]: JValue };
        const src = ["source", "offset", "length"].some((k) => k in ro);
        if ("data" in ro) {
          if (src) bad(`entry ${i} range ${j}: mixes data with source/offset/length`);
          return { data: hexBytes(ro.data, `entry ${i} range ${j} data`) };
        }
        const source = "source" in ro ? int(ro.source, "source") : 0n;
        if (source > 0xffffffffn) bad(`entry ${i} range ${j}: source exceeds uint32`);
        return {
          source: Number(source),
          offset: "offset" in ro ? int(ro.offset, "offset") : 0n,
          length: "length" in ro ? int(ro.length, "length") : 0n,
        };
      });
      return { key, ranges };
    });
  }
  return { sources, entries, mirror };
}

async function main(argv: string[]): Promise<number> {
  const [cmd, descPath, outPath] = argv;
  if (cmd !== "write" || argv.length !== 3) {
    process.stderr.write("usage: cli.ts write <description.json> <out.vzip>\n");
    return 2;
  }
  try {
    const desc = parseDescription(parseJson(decodeJsonBytes(fs.readFileSync(descPath))));
    fs.writeFileSync(outPath, await writeVzip(desc), { flag: "wx" });
    return 0;
  } catch (e) {
    if (e instanceof InvalidInputError || e instanceof JsonError) {
      process.stderr.write(`invalid description: ${e.message}\n`);
      return 1;
    }
    throw e;
  }
}

process.exitCode = await main(process.argv.slice(2));

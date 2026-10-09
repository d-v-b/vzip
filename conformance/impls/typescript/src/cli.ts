// Conformance harness CLI (HARNESS.md).

import fs from "node:fs";
import { InvalidInputError, VzError } from "./errors.ts";
import { decodeJsonBytes, isNum, isObj, JsonError, parseJson, type JValue } from "./json.ts";
import { Archive, type Request } from "./reader.ts";
import { writeArchive, type WArchive, type WEntry, type WRange, type WSource } from "./writer.ts";

class QueryError extends Error {}

type Query =
  | { op: "classify" | "get_raw"; key: string }
  | { op: "get"; key: string; req: Request }
  | { op: "list"; prefix: string };

function nonNegInt(v: JValue | undefined, what: string): bigint {
  if (!isNum(v) || v.int === null || v.int < 0n) throw new QueryError(`${what} must be a non-negative integer`);
  return v.int;
}

function parseQuery(q: JValue, i: number): Query {
  if (!isObj(q)) throw new QueryError(`query ${i} is not an object`);
  const op = q.op;
  if (op !== "classify" && op !== "get" && op !== "get_raw" && op !== "list")
    throw new QueryError(`query ${i}: unknown op ${JSON.stringify(op)}`);
  if (op !== "get" && "range" in q) throw new QueryError(`query ${i}: range on ${op}`);
  if (op === "list") {
    if (typeof q.prefix !== "string") throw new QueryError(`query ${i}: missing prefix`);
    return { op, prefix: q.prefix };
  }
  if (typeof q.key !== "string") throw new QueryError(`query ${i}: missing key`);
  if (op !== "get") return { op, key: q.key };
  let req: Request = { kind: "whole" };
  if ("range" in q) {
    const r = q.range;
    if (!isObj(r)) throw new QueryError(`query ${i}: range must be an object`);
    const has = (k: string) => k in r;
    const forms = [has("start") || has("end"), has("offset"), has("suffix")].filter(Boolean).length;
    if (forms !== 1) throw new QueryError(`query ${i}: range must have exactly one form`);
    if (has("start") || has("end")) {
      if (!has("start") || !has("end")) throw new QueryError(`query ${i}: partial range`);
      req = { kind: "range", start: nonNegInt(r.start, "start"), end: nonNegInt(r.end, "end") };
    } else if (has("offset")) req = { kind: "offset", start: nonNegInt(r.offset, "offset") };
    else req = { kind: "suffix", count: nonNegInt(r.suffix, "suffix") };
  }
  return { op, key: q.key, req };
}

const hex = (b: Buffer | null) => (b === null ? null : b.toString("hex"));

async function cmdRead(archivePath: string, queriesPath: string): Promise<number> {
  let queries: Query[];
  try {
    const v = parseJson(decodeJsonBytes(fs.readFileSync(queriesPath)));
    if (!Array.isArray(v)) throw new QueryError("queries file is not a JSON array");
    queries = v.map(parseQuery);
  } catch (e) {
    process.stderr.write(`invalid queries file: ${(e as Error).message}\n`);
    return 2;
  }
  let archive: Archive;
  try {
    archive = Archive.open(archivePath);
  } catch (e) {
    const msg = (e as Error).message;
    process.stdout.write(JSON.stringify({ open: { ok: false, class: "archive", error: msg }, results: [] }) + "\n");
    return 0;
  }
  const results: unknown[] = [];
  for (const q of queries) {
    try {
      switch (q.op) {
        case "classify":
          results.push({ ok: true, kind: archive.classify(q.key) });
          break;
        case "get":
          results.push({ ok: true, value: hex(await archive.get(q.key, q.req)) });
          break;
        case "get_raw":
          results.push({ ok: true, value: hex(await archive.raw(q.key)) });
          break;
        case "list":
          results.push({ ok: true, keys: archive.list(q.prefix) });
          break;
      }
    } catch (e) {
      if (e instanceof VzError) results.push({ ok: false, class: e.cls, error: e.message });
      else throw e;
    }
  }
  archive.close();
  process.stdout.write(JSON.stringify({ open: { ok: true }, results }) + "\n");
  return 0;
}

// ---------------------------------------------------------------- write

const bad = (m: string): never => {
  throw new InvalidInputError(m);
};

function hexBytes(v: JValue | undefined, what: string): Buffer {
  if (typeof v !== "string" || !/^(?:[0-9a-f]{2})*$/.test(v)) bad(`${what} must be a lowercase even-length hex string`);
  return Buffer.from(v as string, "hex");
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

function parseDescription(v: JValue): WArchive {
  if (!isObj(v)) bad("description must be an object");
  const d = v as { [k: string]: JValue };
  const out: WArchive = { pageSize: null, mirror: true, sources: [], entries: [] };
  if ("page_size" in d && d.page_size !== null) {
    const n = int(d.page_size, "page_size");
    if (n < 1n) bad("page_size must be at least 1");
    out.pageSize = Number(n);
  }
  if ("mirror" in d) out.mirror = bool(d.mirror, "mirror");
  if ("sources" in d) {
    if (!Array.isArray(d.sources)) bad("sources must be an array");
    out.sources = (d.sources as JValue[]).map((s, i): WSource => {
      if (!isObj(s)) bad(`source ${i} must be an object`);
      const o = s as { [k: string]: JValue };
      const kinds = ["url", "key", "data"].filter((k) => k in o);
      if (kinds.length !== 1) bad(`source ${i} must have exactly one of url, key, data`);
      const pins = ["size", "etag", "modified_not_after"].filter((k) => k in o);
      if (kinds[0] !== "url" && pins.length > 0) bad(`source ${i}: pins are only allowed on url sources`);
      if (kinds[0] === "url") {
        if (typeof o.url !== "string") bad(`source ${i}: url must be a string`);
        const src: WSource = { url: o.url as string };
        if ("size" in o) src.size = int(o.size, `source ${i} size`);
        if ("etag" in o) {
          if (typeof o.etag !== "string") bad(`source ${i}: etag must be a string`);
          src.etag = o.etag as string;
        }
        if ("modified_not_after" in o) src.modifiedNotAfter = int(o.modified_not_after, `source ${i} modified_not_after`, true);
        return src;
      }
      if (kinds[0] === "key") {
        if (typeof o.key !== "string") bad(`source ${i}: key must be a string`);
        return { key: o.key as string };
      }
      return { data: hexBytes(o.data, `source ${i} data`) };
    });
  }
  if ("entries" in d) {
    if (!Array.isArray(d.entries)) bad("entries must be an array");
    out.entries = (d.entries as JValue[]).map((e, i): WEntry => {
      if (!isObj(e)) bad(`entry ${i} must be an object`);
      const o = e as { [k: string]: JValue };
      if (typeof o.key !== "string") bad(`entry ${i}: key must be a string`);
      const ent: WEntry = { key: o.key as string };
      if ("compress" in o) ent.compress = bool(o.compress, `entry ${i} compress`);
      if ("pinned" in o) ent.pinned = bool(o.pinned, `entry ${i} pinned`);
      const hasB = "bytes" in o, hasR = "ranges" in o;
      if (hasB === hasR) bad(`entry ${i}: needs exactly one of bytes, ranges`);
      if (hasB) ent.bytes = hexBytes(o.bytes, `entry ${i} bytes`);
      else {
        if (!Array.isArray(o.ranges)) bad(`entry ${i}: ranges must be an array`);
        ent.ranges = (o.ranges as JValue[]).map((r, j): WRange => {
          if (!isObj(r)) bad(`entry ${i} range ${j} must be an object`);
          const ro = r as { [k: string]: JValue };
          const src = ["source", "offset", "length"].some((k) => k in ro);
          if ("data" in ro) {
            if (src) bad(`entry ${i} range ${j}: mixes data with source/offset/length`);
            return { data: hexBytes(ro.data, `entry ${i} range ${j} data`) };
          }
          return {
            source: "source" in ro ? int(ro.source, "source") : 0n,
            offset: "offset" in ro ? int(ro.offset, "offset") : 0n,
            length: "length" in ro ? int(ro.length, "length") : 0n,
          };
        });
      }
      return ent;
    });
  }
  return out;
}

function cmdWrite(descPath: string, outPath: string): number {
  try {
    const v = parseJson(decodeJsonBytes(fs.readFileSync(descPath)));
    writeArchive(parseDescription(v), outPath);
    return 0;
  } catch (e) {
    if (e instanceof InvalidInputError || e instanceof JsonError || e instanceof TypeError) {
      process.stderr.write(`invalid description: ${e.message}\n`);
      return 1;
    }
    process.stderr.write(`error: ${(e as Error).stack ?? e}\n`);
    return 1;
  }
}

async function main(argv: string[]): Promise<number> {
  const [cmd, a, b] = argv;
  if (cmd === "read" && a !== undefined && b !== undefined && argv.length === 3) return cmdRead(a, b);
  if (cmd === "write" && a !== undefined && b !== undefined && argv.length === 3) return cmdWrite(a, b);
  process.stderr.write("usage: vzip read <archive> <queries.json> | vzip write <description.json> <out.vzip>\n");
  return 2;
}

main(process.argv.slice(2)).then(
  (code) => {
    process.exitCode = code;
  },
  (e) => {
    process.stderr.write(`crash: ${(e as Error).stack ?? e}\n`);
    process.exitCode = 70;
  },
);

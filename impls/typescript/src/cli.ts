// Conformance harness CLI (HARNESS.md): `read` and `write`.
import * as fs from "node:fs";
import { InputError, VzError } from "./errors.ts";
import { JNum, type JValue, parseJson } from "./json.ts";
import type { Range, Source } from "./proto.ts";
import { Archive, type Request } from "./reader.ts";
import { type EntryInput, type WriteInput, writeArchive } from "./writer.ts";

class UsageError extends Error {}

function readJsonFile(p: string): JValue {
  const raw = fs.readFileSync(p);
  const text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(raw);
  return parseJson(text);
}

type Obj = { [k: string]: JValue };

function isObj(v: JValue | undefined): v is Obj {
  return typeof v === "object" && v !== null && !Array.isArray(v) && !(v instanceof JNum);
}

const hex = (b: Uint8Array) => Buffer.from(b.buffer, b.byteOffset, b.byteLength).toString("hex");

// ---------------- read ----------------

function nonNegInt(v: JValue | undefined, what: string, Err: new (m: string) => Error): bigint {
  if (!(v instanceof JNum) || !v.isInteger) throw new Err(`${what} must be an integer`);
  const n = v.toBigInt();
  if (n < 0n) throw new Err(`${what} must be non-negative`);
  return n;
}

interface Query {
  op: "classify" | "get" | "get_raw" | "list";
  key: string;
  req: Request;
}

function parseQuery(q: JValue): Query {
  if (!isObj(q)) throw new UsageError("query is not an object");
  const op = q.op;
  if (op === "list") {
    if (typeof q.prefix !== "string") throw new UsageError("list query needs a string prefix");
    return { op, key: q.prefix, req: { type: "whole" } };
  }
  if (op !== "classify" && op !== "get" && op !== "get_raw") throw new UsageError(`unknown op ${JSON.stringify(op)}`);
  if (typeof q.key !== "string") throw new UsageError("query needs a string key");
  let req: Request = { type: "whole" };
  if (op === "get" && q.range !== undefined) {
    const r = q.range;
    if (!isObj(r)) throw new UsageError("range must be an object");
    const forms = ["start", "offset", "suffix"].filter((k) => k in r);
    const hasEnd = "end" in r;
    if (forms.length !== 1) throw new UsageError("range must have exactly one form");
    if (forms[0] === "start") {
      if (!hasEnd) throw new UsageError("range with start needs end");
      req = { type: "range", start: nonNegInt(r.start, "start", UsageError), end: nonNegInt(r.end, "end", UsageError) };
    } else if (hasEnd) {
      throw new UsageError("range must have exactly one form");
    } else if (forms[0] === "offset") {
      req = { type: "offset", start: nonNegInt(r.offset, "offset", UsageError) };
    } else {
      req = { type: "suffix", count: nonNegInt(r.suffix, "suffix", UsageError) };
    }
  } else if (q.range !== undefined) {
    throw new UsageError(`${op} does not take a range`);
  }
  return { op, key: q.key, req };
}

function errResult(e: unknown): object {
  if (e instanceof VzError) return { ok: false, class: e.cls, error: e.message };
  throw e;
}

async function cmdRead(archivePath: string, queriesPath: string): Promise<void> {
  const qv = readJsonFile(queriesPath);
  if (!Array.isArray(qv)) throw new UsageError("queries file must hold a JSON array");
  const queries = qv.map(parseQuery);
  let archive: Archive;
  try {
    archive = Archive.open(archivePath);
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    process.stdout.write(JSON.stringify({ open: { ok: false, class: "archive", error: msg }, results: [] }) + "\n");
    return;
  }
  const results: object[] = [];
  for (const q of queries) {
    try {
      switch (q.op) {
        case "classify":
          results.push({ ok: true, kind: archive.classify(q.key) });
          break;
        case "get": {
          const v = await archive.get(q.key, q.req);
          results.push({ ok: true, value: v === null ? null : hex(v) });
          break;
        }
        case "get_raw": {
          const v = await archive.raw(q.key);
          results.push({ ok: true, value: v === null ? null : hex(v) });
          break;
        }
        case "list":
          results.push({ ok: true, keys: archive.list(q.key) });
          break;
      }
    } catch (e) {
      results.push(errResult(e));
    }
  }
  archive.close();
  process.stdout.write(JSON.stringify({ open: { ok: true }, results }) + "\n");
}

// ---------------- write ----------------

function hexBytes(v: JValue | undefined, what: string): Uint8Array {
  if (typeof v !== "string" || !/^(?:[0-9a-f]{2})*$/.test(v)) throw new InputError(`${what} must be a lowercase even-length hex string`);
  return Buffer.from(v, "hex");
}

function bool(o: Obj, k: string, dflt: boolean): boolean {
  if (!(k in o)) return dflt;
  const v = o[k];
  if (typeof v !== "boolean") throw new InputError(`${k} must be a boolean`);
  return v;
}

function intOf(v: JValue, what: string): bigint {
  if (!(v instanceof JNum) || !v.isInteger) throw new InputError(`${what} must be an integer`);
  return v.toBigInt();
}

function parseDescription(d: JValue): WriteInput {
  if (!isObj(d)) throw new InputError("description must be an object");
  let pageSize: number | null = null;
  if ("page_size" in d && d.page_size !== null) {
    const n = intOf(d.page_size, "page_size");
    if (n < 1n) throw new InputError("page_size must be at least 1");
    pageSize = n > BigInt(Number.MAX_SAFE_INTEGER) ? Number.MAX_SAFE_INTEGER : Number(n);
  }
  const mirror = bool(d, "mirror", true);
  const sources: Source[] = [];
  if ("sources" in d) {
    if (!Array.isArray(d.sources)) throw new InputError("sources must be an array");
    d.sources.forEach((s, i) => {
      if (!isObj(s)) throw new InputError(`source ${i} must be an object`);
      const kinds = ["url", "key", "data"].filter((k) => k in s);
      if (kinds.length !== 1) throw new InputError(`source ${i} must have exactly one of url, key, data`);
      const src: Source = { kind: null, size: null, etag: null, modifiedNotAfter: null };
      const kk = kinds[0];
      if (kk === "data") src.kind = { type: "data", value: hexBytes(s.data, `source ${i} data`) };
      else {
        const v = s[kk];
        if (typeof v !== "string") throw new InputError(`source ${i} ${kk} must be a string`);
        src.kind = kk === "url" ? { type: "url", value: v } : { type: "key", value: v };
      }
      if ("size" in s) {
        src.size = intOf(s.size, `source ${i} size`);
        if (src.size < 0n) throw new InputError(`source ${i} size must be non-negative`);
      }
      if ("etag" in s) {
        if (typeof s.etag !== "string") throw new InputError(`source ${i} etag must be a string`);
        src.etag = s.etag;
      }
      if ("modified_not_after" in s) src.modifiedNotAfter = intOf(s.modified_not_after, `source ${i} modified_not_after`);
      sources.push(src);
    });
  }
  const entries: EntryInput[] = [];
  if ("entries" in d) {
    if (!Array.isArray(d.entries)) throw new InputError("entries must be an array");
    d.entries.forEach((e, i) => {
      if (!isObj(e)) throw new InputError(`entry ${i} must be an object`);
      if (typeof e.key !== "string") throw new InputError(`entry ${i} needs a string key`);
      const hasB = "bytes" in e;
      const hasR = "ranges" in e;
      if (hasB === hasR) throw new InputError(`entry ${i} must have exactly one of bytes and ranges`);
      const compress = bool(e, "compress", false);
      const pinned = bool(e, "pinned", false);
      if (hasB) {
        entries.push({ key: e.key, bytes: hexBytes(e.bytes, `entry ${i} bytes`), compress, pinned });
        return;
      }
      if (compress) throw new InputError(`entry ${i}: compress is only allowed on bytes entries`);
      if (pinned) throw new InputError(`entry ${i}: only bytes entries may be pinned`);
      if (!Array.isArray(e.ranges)) throw new InputError(`entry ${i} ranges must be an array`);
      const ranges: Range[] = e.ranges.map((r, j) => {
        if (!isObj(r)) throw new InputError(`entry ${i} range ${j} must be an object`);
        const lit = "data" in r;
        const srcFields = ["source", "offset", "length"].filter((k) => k in r);
        if (lit && srcFields.length > 0) throw new InputError(`entry ${i} range ${j} mixes literal and source fields`);
        if (lit) return { source: 0, offset: 0n, length: 0n, data: hexBytes(r.data, `entry ${i} range ${j} data`) };
        const get = (k: string) => {
          if (!(k in r)) return 0n;
          const n = intOf(r[k], `entry ${i} range ${j} ${k}`);
          if (n < 0n) throw new InputError(`entry ${i} range ${j} ${k} must be non-negative`);
          return n;
        };
        const source = get("source");
        if (source > 0xffffffffn) throw new InputError(`entry ${i} range ${j} source exceeds uint32`);
        return { source: Number(source), offset: get("offset"), length: get("length"), data: null };
      });
      entries.push({ key: e.key, ranges });
    });
  }
  return { pageSize, mirror, sources, entries };
}

function cmdWrite(descPath: string, outPath: string): void {
  let input: WriteInput;
  try {
    input = parseDescription(readJsonFile(descPath));
  } catch (e) {
    if (e instanceof SyntaxError) throw new InputError(e.message);
    throw e;
  }
  writeArchive(input, outPath);
}

// ---------------- main ----------------

async function main(argv: string[]): Promise<number> {
  const [cmd, a, b] = argv;
  try {
    if (cmd === "read" && argv.length === 3) {
      await cmdRead(a, b);
      return 0;
    }
    if (cmd === "write" && argv.length === 3) {
      cmdWrite(a, b);
      return 0;
    }
    process.stderr.write("usage: vzip read <archive> <queries.json> | vzip write <description.json> <out.vzip>\n");
    return 2;
  } catch (e) {
    process.stderr.write(`vzip: ${e instanceof Error ? e.message : String(e)}\n`);
    return e instanceof InputError ? 1 : 2;
  }
}

process.exitCode = await main(process.argv.slice(2));

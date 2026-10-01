// Conformance-harness CLI (HARNESS.md): `read` and `write`.
import * as fs from "node:fs";
import { VzipError, WriterInputError } from "./errors.ts";
import type { Range, Source } from "./proto.ts";
import { Archive, type Request } from "./reader.ts";
import { type WriterEntry, writeArchive } from "./writer.ts";

class InvalidInput extends Error {}

/** A JSON integer, kept exactly as its source text. */
class JInt {
  value: bigint;
  constructor(v: bigint) {
    this.value = v;
  }
}
/** A JSON number that is not an integer literal (e.g. 1.0, 1e3). */
class JNonInt {}

/** Parse JSON keeping integers exact and distinguishing `1` from `1.0`. */
export function parseJson(text: string): unknown {
  return JSON.parse(text, function (_k, v, ctx?: { source?: string }) {
    if (typeof v === "number") {
      const src = ctx?.source ?? String(v);
      if (/^-?(0|[1-9][0-9]*)$/.test(src)) return new JInt(BigInt(src));
      return new JNonInt();
    }
    return v;
  });
}

const bad = (m: string): never => {
  throw new InvalidInput(m);
};

function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v) && !(v instanceof JInt) && !(v instanceof JNonInt);
}

function has(o: Record<string, unknown>, k: string): boolean {
  return Object.prototype.hasOwnProperty.call(o, k);
}

function getInt(o: Record<string, unknown>, k: string, what: string, min = 0n): bigint | undefined {
  if (!has(o, k)) return undefined;
  const v = o[k];
  if (!(v instanceof JInt)) bad(`${what}.${k} must be an integer`);
  if ((v as JInt).value < min) bad(`${what}.${k} must be >= ${min}`);
  return (v as JInt).value;
}

function getBool(o: Record<string, unknown>, k: string, what: string, dflt: boolean): boolean {
  if (!has(o, k)) return dflt;
  const v = o[k];
  if (typeof v !== "boolean") bad(`${what}.${k} must be a boolean`);
  return v as boolean;
}

function getStr(o: Record<string, unknown>, k: string, what: string): string | undefined {
  if (!has(o, k)) return undefined;
  const v = o[k];
  if (typeof v !== "string") bad(`${what}.${k} must be a string`);
  return v as string;
}

function getHex(o: Record<string, unknown>, k: string, what: string): Uint8Array | undefined {
  const s = getStr(o, k, what);
  if (s === undefined) return undefined;
  if (!/^([0-9a-f]{2})*$/.test(s)) bad(`${what}.${k} must be lowercase hex of even length`);
  return Buffer.from(s, "hex");
}

function noNulls(v: unknown, path: string): void {
  if (v === null) bad(`${path} is null`);
  if (Array.isArray(v)) v.forEach((x, i) => noNulls(x, `${path}[${i}]`));
  else if (isObj(v)) for (const [k, x] of Object.entries(v)) noNulls(x, `${path}.${k}`);
}

function parseDescription(d: unknown): { sources: Source[]; entries: WriterEntry[]; pageSize: number | null; mirror: boolean } {
  if (!isObj(d)) bad("description must be a JSON object");
  const top = d as Record<string, unknown>;
  for (const [k, v] of Object.entries(top)) if (k !== "page_size") noNulls(v, k);
  let pageSize: number | null = null;
  if (has(top, "page_size") && top.page_size !== null) {
    const v = top.page_size;
    if (!(v instanceof JInt) || v.value < 1n) bad("page_size must be null or an integer >= 1");
    const n = (v as JInt).value;
    pageSize = n > BigInt(Number.MAX_SAFE_INTEGER) ? Number.MAX_SAFE_INTEGER : Number(n);
  }
  const mirror = getBool(top, "mirror", "description", true);
  const srcArr = has(top, "sources") ? top.sources : [];
  const entArr = has(top, "entries") ? top.entries : [];
  if (!Array.isArray(srcArr)) bad("sources must be an array");
  if (!Array.isArray(entArr)) bad("entries must be an array");

  const sources: Source[] = (srcArr as unknown[]).map((s, i) => {
    const what = `sources[${i}]`;
    if (!isObj(s)) bad(`${what} must be an object`);
    const o = s as Record<string, unknown>;
    const kinds = ["url", "key", "data"].filter((k) => has(o, k));
    if (kinds.length !== 1) bad(`${what} must have exactly one of url, key, data`);
    const src: Source = { kind: kinds[0] as Source["kind"] };
    if (src.kind === "url") src.url = getStr(o, "url", what);
    else if (src.kind === "key") src.key = getStr(o, "key", what);
    else src.data = getHex(o, "data", what);
    src.size = getInt(o, "size", what);
    src.etag = getStr(o, "etag", what);
    src.modifiedNotAfter = getInt(o, "modified_not_after", what, -(1n << 63n));
    return src;
  });

  const entries: WriterEntry[] = (entArr as unknown[]).map((e, i) => {
    const what = `entries[${i}]`;
    if (!isObj(e)) bad(`${what} must be an object`);
    const o = e as Record<string, unknown>;
    const key = getStr(o, "key", what);
    if (key === undefined) bad(`${what} has no key`);
    if (has(o, "bytes") === has(o, "ranges")) bad(`${what} must have exactly one of bytes, ranges`);
    const compress = getBool(o, "compress", what, false);
    const pinned = getBool(o, "pinned", what, false);
    if (has(o, "bytes")) {
      if (pinned && pageSize === null) bad(`${what} is pinned but page_size is null`);
      return { key: key!, bytes: getHex(o, "bytes", what)!, compress, pinned };
    }
    if (compress) bad(`${what}: compress is only allowed on bytes entries`);
    if (pinned) bad(`${what}: only bytes entries may be pinned`);
    const ra = o.ranges;
    if (!Array.isArray(ra)) bad(`${what}.ranges must be an array`);
    const ranges: Range[] = (ra as unknown[]).map((r, j) => {
      const rw = `${what}.ranges[${j}]`;
      if (!isObj(r)) bad(`${rw} must be an object`);
      const ro = r as Record<string, unknown>;
      const isLit = has(ro, "data");
      const isSrc = has(ro, "source") || has(ro, "offset") || has(ro, "length");
      if (isLit && isSrc) bad(`${rw} mixes a literal and a source range`);
      if (isLit) return { source: 0, offset: 0n, length: 0n, data: getHex(ro, "data", rw) };
      const source = getInt(ro, "source", rw) ?? 0n;
      return {
        source: source > 0xffffffffn ? 0xffffffff + 1 : Number(source),
        offset: getInt(ro, "offset", rw) ?? 0n,
        length: getInt(ro, "length", rw) ?? 0n,
      };
    });
    return { key: key!, ranges };
  });
  return { sources, entries, pageSize, mirror };
}

function cmdWrite(descPath: string, outPath: string): number {
  let desc;
  try {
    desc = parseDescription(parseJson(fs.readFileSync(descPath, "utf8")));
  } catch (e) {
    if (e instanceof InvalidInput || e instanceof SyntaxError) {
      process.stderr.write(`invalid description: ${e.message}\n`);
      return 2;
    }
    throw e;
  }
  let out: Buffer;
  try {
    out = writeArchive(desc.sources, desc.entries, { pageSize: desc.pageSize, mirror: desc.mirror });
  } catch (e) {
    if (e instanceof WriterInputError) {
      process.stderr.write(`invalid description: ${e.message}\n`);
      return 2;
    }
    throw e;
  }
  fs.writeFileSync(outPath, out, { flag: "wx" });
  return 0;
}

type Query =
  | { op: "classify"; key: string }
  | { op: "get"; key: string; req: Request }
  | { op: "get_raw"; key: string }
  | { op: "list"; prefix: string };

function parseQueries(v: unknown): Query[] {
  if (!Array.isArray(v)) bad("queries must be a JSON array");
  return (v as unknown[]).map((q, i) => {
    const what = `queries[${i}]`;
    if (!isObj(q)) bad(`${what} must be an object`);
    const o = q as Record<string, unknown>;
    const op = o.op;
    if (op === "list") {
      const prefix = getStr(o, "prefix", what);
      if (prefix === undefined) bad(`${what} has no prefix`);
      return { op, prefix: prefix! };
    }
    if (op !== "classify" && op !== "get" && op !== "get_raw") bad(`${what} has unknown op`);
    const key = getStr(o, "key", what);
    if (key === undefined) bad(`${what} has no key`);
    if (op === "get") {
      let req: Request = { type: "whole" };
      if (has(o, "range")) {
        const r = o.range;
        if (!isObj(r)) bad(`${what}.range must be an object`);
        const ro = r as Record<string, unknown>;
        const forms = [has(ro, "start") || has(ro, "end"), has(ro, "offset"), has(ro, "suffix")].filter(Boolean).length;
        if (forms !== 1) bad(`${what}.range must have exactly one form`);
        if (has(ro, "offset")) req = { type: "offset", start: getInt(ro, "offset", what)! };
        else if (has(ro, "suffix")) req = { type: "suffix", count: getInt(ro, "suffix", what)! };
        else {
          const start = getInt(ro, "start", what);
          const end = getInt(ro, "end", what);
          if (start === undefined || end === undefined) bad(`${what}.range needs start and end`);
          req = { type: "range", start: start!, end: end! };
        }
      }
      return { op, key: key!, req };
    }
    if (op === "get_raw" && has(o, "range")) bad(`${what}: get_raw takes no range`);
    return { op, key: key! } as Query;
  });
}

function errorResult(e: unknown): Record<string, unknown> {
  if (e instanceof VzipError) return { ok: false, class: e.errorClass, error: e.message };
  return { ok: false, class: "internal", error: String((e as Error)?.stack ?? e) };
}

async function cmdRead(archivePath: string, queriesPath: string): Promise<number> {
  let queries: Query[];
  try {
    queries = parseQueries(parseJson(fs.readFileSync(queriesPath, "utf8")));
  } catch (e) {
    process.stderr.write(`invalid queries file: ${(e as Error).message}\n`);
    return 2;
  }
  let archive: Archive;
  try {
    archive = await Archive.open(archivePath);
  } catch (e) {
    if (e instanceof VzipError) {
      process.stdout.write(JSON.stringify({ open: { ok: false, class: "archive", error: e.message }, results: [] }) + "\n");
      return 0;
    }
    throw e;
  }
  const results: unknown[] = [];
  for (const q of queries) {
    try {
      switch (q.op) {
        case "classify":
          results.push({ ok: true, kind: await archive.classify(q.key) });
          break;
        case "get": {
          const v = await archive.get(q.key, q.req);
          results.push({ ok: true, value: v === null ? null : v.toString("hex") });
          break;
        }
        case "get_raw": {
          const v = await archive.raw(q.key);
          results.push({ ok: true, value: v === null ? null : v.toString("hex") });
          break;
        }
        case "list":
          results.push({ ok: true, keys: await archive.list(q.prefix) });
          break;
      }
    } catch (e) {
      results.push(errorResult(e));
    }
  }
  await archive.close();
  process.stdout.write(JSON.stringify({ open: { ok: true }, results }) + "\n");
  return 0;
}

async function main(argv: string[]): Promise<number> {
  const [cmd, a, b] = argv;
  if (cmd === "read" && a !== undefined && b !== undefined && argv.length === 3) return cmdRead(a, b);
  if (cmd === "write" && a !== undefined && b !== undefined && argv.length === 3) return cmdWrite(a, b);
  process.stderr.write("usage: vzip read <archive> <queries.json>\n       vzip write <description.json> <out>\n");
  return 2;
}

main(process.argv.slice(2)).then(
  (code) => {
    process.exitCode = code;
  },
  (e) => {
    process.stderr.write(`vzip: internal error: ${(e as Error)?.stack ?? e}\n`);
    process.exitCode = 1;
  },
);

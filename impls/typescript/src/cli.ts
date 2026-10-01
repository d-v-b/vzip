// CLI implementing HARNESS.md: `read` and `write`.

import * as fs from "node:fs";
import { Archive, type Request } from "./reader.ts";
import { VzipError } from "./errors.ts";
import { WriteError, writeArchive, type WriterEntry, type WriterInput } from "./writer.ts";
import type { Range, Source } from "./proto.ts";
import { hex } from "./common.ts";

class InvalidInput extends Error {}

const BAD_NUMBER = Symbol("bad-number");

/** JSON.parse that turns integers into bigint and marks non-integer numbers. */
function parseStrictJson(text: string): unknown {
  return JSON.parse(text, function (this: unknown, _k: string, v: unknown, ctx?: { source?: string }) {
    if (typeof v === "number") {
      const src = ctx?.source;
      if (src === undefined) throw new Error("JSON.parse source access unsupported");
      if (/^-?(0|[1-9][0-9]*)$/.test(src)) return BigInt(src);
      return BAD_NUMBER;
    }
    return v;
  } as any);
}

const bad = (m: string): never => {
  throw new InvalidInput(m);
};
const isObj = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v) && v !== BAD_NUMBER;
const has = (o: Record<string, unknown>, k: string) => Object.prototype.hasOwnProperty.call(o, k);

function getInt(o: Record<string, unknown>, k: string, opts: { min?: bigint; dflt?: bigint } = {}): bigint | undefined {
  if (!has(o, k)) return opts.dflt;
  const v = o[k];
  if (typeof v !== "bigint") bad(`${k} must be an integer`);
  if (opts.min !== undefined && (v as bigint) < opts.min) bad(`${k} must be >= ${opts.min}`);
  return v as bigint;
}
function getBool(o: Record<string, unknown>, k: string, dflt: boolean): boolean {
  if (!has(o, k)) return dflt;
  if (typeof o[k] !== "boolean") bad(`${k} must be a boolean`);
  return o[k] as boolean;
}
function getStr(o: Record<string, unknown>, k: string): string | undefined {
  if (!has(o, k)) return undefined;
  if (typeof o[k] !== "string") bad(`${k} must be a string`);
  return o[k] as string;
}
function getHex(o: Record<string, unknown>, k: string): Uint8Array | undefined {
  const s = getStr(o, k);
  if (s === undefined) return undefined;
  if (!/^(?:[0-9a-f]{2})*$/.test(s)) bad(`${k} must be lowercase hex of even length`);
  return Buffer.from(s, "hex");
}
function getArr(o: Record<string, unknown>, k: string): unknown[] {
  if (!has(o, k)) return [];
  if (!Array.isArray(o[k])) bad(`${k} must be an array`);
  return o[k] as unknown[];
}

function parseDescription(d: unknown): WriterInput {
  if (!isObj(d)) bad("description must be an object");
  const o = d as Record<string, unknown>;
  let pageSize: number | null = null;
  if (has(o, "page_size") && o.page_size !== null) {
    const v = getInt(o, "page_size", { min: 1n })!;
    if (v > BigInt(Number.MAX_SAFE_INTEGER)) bad("page_size too large");
    pageSize = Number(v);
  }
  const mirror = getBool(o, "mirror", true);
  const sources: Source[] = getArr(o, "sources").map((s, i) => {
    if (!isObj(s)) bad(`sources[${i}] must be an object`);
    const so = s as Record<string, unknown>;
    const kinds = ["url", "key", "data"].filter((k) => has(so, k));
    if (kinds.length !== 1) bad(`sources[${i}] must have exactly one of url, key, data`);
    const src: Source = { kind: kinds[0] as Source["kind"], size: null, etag: null, modifiedNotAfter: null };
    if (src.kind === "url") src.url = getStr(so, "url");
    else if (src.kind === "key") src.key = getStr(so, "key");
    else src.data = getHex(so, "data");
    const size = getInt(so, "size", { min: 0n });
    if (size !== undefined) src.size = size;
    const etag = getStr(so, "etag");
    if (etag !== undefined) src.etag = etag;
    const mna = getInt(so, "modified_not_after");
    if (mna !== undefined) src.modifiedNotAfter = mna;
    return src;
  });
  const entries: WriterEntry[] = getArr(o, "entries").map((e, i) => {
    if (!isObj(e)) bad(`entries[${i}] must be an object`);
    const eo = e as Record<string, unknown>;
    const key = getStr(eo, "key");
    if (key === undefined) bad(`entries[${i}] has no key`);
    const hasBytes = has(eo, "bytes");
    const hasRanges = has(eo, "ranges");
    if (hasBytes === hasRanges) bad(`entries[${i}] must have exactly one of bytes and ranges`);
    const compress = getBool(eo, "compress", false);
    const pinned = getBool(eo, "pinned", false);
    if (hasBytes) {
      if (pinned && pageSize === null) bad(`entries[${i}] is pinned but page_size is null`);
      return { key: key!, bytes: getHex(eo, "bytes"), compress, pinned };
    }
    if (compress) bad(`entries[${i}] is a reference entry with compress: true`);
    if (pinned) bad(`entries[${i}] is a reference entry with pinned: true`);
    const ranges: Range[] = getArr(eo, "ranges").map((r, j) => {
      if (!isObj(r)) bad(`entries[${i}].ranges[${j}] must be an object`);
      const ro = r as Record<string, unknown>;
      if (has(ro, "data")) {
        if (has(ro, "source") || has(ro, "offset") || has(ro, "length")) bad(`entries[${i}].ranges[${j}] mixes data with source fields`);
        return { source: 0, offset: 0n, length: 0n, data: getHex(ro, "data")! };
      }
      const source = getInt(ro, "source", { min: 0n, dflt: 0n })!;
      const offset = getInt(ro, "offset", { min: 0n, dflt: 0n })!;
      const length = getInt(ro, "length", { min: 0n, dflt: 0n })!;
      if (source >= BigInt(sources.length)) bad(`entries[${i}].ranges[${j}] names source ${source} but there are ${sources.length}`);
      return { source: Number(source), offset, length, data: null };
    });
    return { key: key!, ranges, compress, pinned };
  });
  return { sources, entries, pageSize, mirror };
}

type Query =
  | { op: "classify"; key: string }
  | { op: "get"; key: string; req: Request }
  | { op: "get_raw"; key: string }
  | { op: "list"; prefix: string };

function parseQueries(q: unknown): Query[] {
  if (!Array.isArray(q)) bad("queries must be an array");
  return (q as unknown[]).map((x, i) => {
    if (!isObj(x)) bad(`query ${i} must be an object`);
    const o = x as Record<string, unknown>;
    const op = o.op;
    if (op === "list") {
      const prefix = getStr(o, "prefix");
      if (prefix === undefined) bad(`query ${i}: list needs a prefix`);
      return { op, prefix: prefix! };
    }
    if (op !== "classify" && op !== "get" && op !== "get_raw") bad(`query ${i}: unknown op ${String(op)}`);
    const key = getStr(o, "key");
    if (key === undefined) bad(`query ${i}: missing key`);
    if (op !== "get") return { op, key: key! } as Query;
    let req: Request = { type: "whole" };
    if (has(o, "range")) {
      const r = o.range;
      if (!isObj(r)) bad(`query ${i}: range must be an object`);
      const ro = r as Record<string, unknown>;
      const forms = [has(ro, "start") || has(ro, "end"), has(ro, "offset"), has(ro, "suffix")].filter(Boolean).length;
      if (forms !== 1) bad(`query ${i}: range must have exactly one form`);
      if (has(ro, "start") || has(ro, "end")) {
        if (!has(ro, "start") || !has(ro, "end")) bad(`query ${i}: range needs start and end`);
        req = { type: "range", start: getInt(ro, "start", { min: 0n })!, end: getInt(ro, "end", { min: 0n })! };
      } else if (has(ro, "offset")) req = { type: "offset", start: getInt(ro, "offset", { min: 0n })! };
      else req = { type: "suffix", count: getInt(ro, "suffix", { min: 0n })! };
    }
    return { op, key: key!, req };
  });
}

async function runQuery(a: Archive, q: Query): Promise<unknown> {
  try {
    switch (q.op) {
      case "classify":
        return { ok: true, kind: a.classify(q.key) };
      case "get": {
        const v = await a.get(q.key, q.req);
        return { ok: true, value: v === null ? null : hex(v) };
      }
      case "get_raw": {
        const v = a.raw(q.key);
        return { ok: true, value: v === null ? null : hex(v) };
      }
      case "list":
        return { ok: true, keys: a.list(q.prefix) };
    }
  } catch (e) {
    if (e instanceof VzipError) return { ok: false, class: e.cls, error: e.message };
    process.stderr.write(`unexpected error: ${(e as Error).stack}\n`);
    return { ok: false, class: "resolution", error: `unexpected: ${(e as Error).message}` };
  }
}

async function cmdRead(archivePath: string, queriesPath: string): Promise<number> {
  let queries: Query[];
  try {
    queries = parseQueries(parseStrictJson(fs.readFileSync(queriesPath, "utf8")));
  } catch (e) {
    process.stderr.write(`invalid queries file: ${(e as Error).message}\n`);
    return 2;
  }
  let a: Archive;
  try {
    a = Archive.open(archivePath);
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    process.stdout.write(JSON.stringify({ open: { ok: false, class: "archive", error: msg }, results: [] }) + "\n");
    return 0;
  }
  const results: unknown[] = [];
  for (const q of queries) results.push(await runQuery(a, q));
  a.close();
  process.stdout.write(JSON.stringify({ open: { ok: true }, results }) + "\n");
  return 0;
}

function cmdWrite(descPath: string, outPath: string): number {
  let input: WriterInput;
  try {
    input = parseDescription(parseStrictJson(fs.readFileSync(descPath, "utf8")));
  } catch (e) {
    process.stderr.write(`invalid description: ${(e as Error).message}\n`);
    return 2;
  }
  try {
    writeArchive(input, outPath);
  } catch (e) {
    if (e instanceof WriteError) {
      process.stderr.write(`invalid description: ${e.message}\n`);
      return 2;
    }
    process.stderr.write(`write failed: ${(e as Error).message}\n`);
    return 1;
  }
  return 0;
}

async function main(argv: string[]): Promise<number> {
  const [cmd, a1, a2] = argv;
  if (cmd === "read" && argv.length === 3) return cmdRead(a1, a2);
  if (cmd === "write" && argv.length === 3) return cmdWrite(a1, a2);
  process.stderr.write("usage: vzip read <archive> <queries.json> | vzip write <description.json> <out.vzip>\n");
  return 2;
}

main(process.argv.slice(2)).then(
  (code) => {
    process.exitCode = code;
  },
  (e) => {
    process.stderr.write(`fatal: ${(e as Error).stack}\n`);
    process.exitCode = 1;
  },
);

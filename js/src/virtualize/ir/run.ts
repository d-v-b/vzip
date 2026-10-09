// Virtualizing an ND2, TIFF or CZI file with the Rust core (rust/vzip-ir): its
// parser, read planner, image projection and source mirror run in WebAssembly;
// this host performs the requests a run asks for (`RangeSource`) and turns its
// output (`Out::encode`) into the writer's archive description. The twin of
// `new_run` and `drive` in python/src/vzip/ir/planner.py.

import type { Range, Source } from "../../protobuf.ts";
import type { ArchiveDesc, EntryDesc } from "../../writer.ts";
import { ImageError } from "../common.ts";
import type { RangeSource, Span } from "./source.ts";
import { ir, type IrExports, out, put } from "./wasm.ts";

export type IrFormat = "nd2" | "tiff" | "czi";
const CODES: Record<IrFormat, number> = { nd2: 0, tiff: 1, czi: 2 };

export interface IrOutput extends ArchiveDesc {
  format: IrFormat;
  /** The projection's summary, with the planner's counts (`planner`) and the IR's size. */
  summary: Record<string, unknown>;
}

const utf8 = new TextEncoder();
const text = (b: Uint8Array) => new TextDecoder().decode(b);

/** The run's rejection (a negative result: minus its message's length) as an ImageError. */
export function rejection(w: IrExports, r: number): ImageError {
  return new ImageError(text(out(w, -r)));
}

/** Virtualizes `source` (the file at `url`) as `format`. A file the core refuses is an
 * ImageError; a source that cannot be read, its own error. */
export async function virtualizeIr(
  format: IrFormat,
  url: string,
  source: RangeSource,
  options: { mirror?: boolean; signal?: AbortSignal } = {},
): Promise<IrOutput> {
  const w = await ir();
  const p = w.vz_run_new(CODES[format], BigInt(source.size), source.remote ? 1 : 0, source.concurrency);
  let live = true;
  try {
    if (!source.multirange) w.vz_run_multirange(p, 0);
    const settings = source.planner;
    if (settings) w.vz_run_settings(p, settings.byteCost ?? -1, BigInt(settings.wholeBelow ?? 1 << 20));
    const head = source.head;
    if (head !== undefined && head.data.length > 0) {
      w.vz_run_seed(p, BigInt(head.offset), put(w, head.data), head.data.length);
      w.vz_run_observe(p, BigInt(head.data.length), head.seconds);
    }
    for (;;) {
      options.signal?.throwIfAborted();
      const r = Number(w.vz_run_poll(p));
      if (r < 0) throw rejection(w, r);
      if (r === 0) break;
      await perform(w, p, source, decodeRequests(out(w, r - 1)), options.signal);
    }
    live = false; // vz_run_output consumes the run
    const u = utf8.encode(url);
    const n = Number(w.vz_run_output(p, put(w, u), u.length, options.mirror === false ? 0 : 1));
    if (n < 0) throw rejection(w, n);
    return { format, ...decodeOutput(out(w, n)) };
  } finally {
    if (live) w.vz_run_free(p);
  }
}

/** The requests of a poll: each one's spans [start, end). */
export function decodeRequests(b: Uint8Array): Span[][] {
  const v = new DataView(b.buffer, b.byteOffset, b.byteLength);
  let at = 0;
  const u32 = () => ((at += 4), v.getUint32(at - 4, true));
  const u64 = () => ((at += 8), Number(v.getBigUint64(at - 8, true)));
  const reqs: Span[][] = [];
  for (let k = u32(); k > 0; k--) {
    const spans: Span[] = [];
    for (let n = u32(); n > 0; n--) spans.push([u64(), u64()]);
    reqs.push(spans);
  }
  return reqs;
}

/** Performs a poll's requests, at most `source.concurrency` at a time, completing each
 * as it arrives. Every request has settled when this returns or throws (the run is
 * not touched after a failure). */
async function perform(w: IrExports, p: number, source: RangeSource, reqs: Span[][], signal?: AbortSignal): Promise<void> {
  const abort = new AbortController();
  const onAbort = () => abort.abort(signal?.reason);
  signal?.addEventListener("abort", onAbort, { once: true });
  let next = 0;
  let failure: { error: unknown } | undefined;
  const worker = async () => {
    while (failure === undefined && next < reqs.length) {
      const k = next++;
      try {
        const t = performance.now();
        const parts = await source.fetch(reqs[k], abort.signal);
        const seconds = (performance.now() - t) / 1000;
        if (failure !== undefined) return;
        let r: number;
        if (parts === null) {
          r = Number(w.vz_run_complete(p, k, 0, 0, 0, seconds));
        } else {
          const n = parts.reduce((s, x) => s + x.length, 0);
          const ptr = w.vz_alloc(n) >>> 0;
          const mem = new Uint8Array(w.memory.buffer, ptr, n);
          let at = 0;
          for (const x of parts) {
            mem.set(x, at);
            at += x.length;
          }
          r = Number(w.vz_run_complete(p, k, 1, ptr, n, seconds));
        }
        if (r < 0) throw rejection(w, r);
      } catch (e) {
        failure ??= { error: e };
        abort.abort();
      }
    }
  };
  const workers = Array.from({ length: Math.max(1, Math.min(source.concurrency, reqs.length)) }, worker);
  await Promise.allSettled(workers);
  signal?.removeEventListener("abort", onAbort);
  if (failure !== undefined) throw failure.error;
}

/** A run's output (`Out::encode`) as the writer's archive description: source 0 the
 * file, then the data sources; the hierarchy's documents stored, and the source
 * metadata node's documents (and those under the other lazy prefixes) and copied
 * bytes compressed, as the Python host writes them. */
export function decodeOutput(b: Uint8Array): ArchiveDesc & { summary: Record<string, unknown> } {
  const v = new DataView(b.buffer, b.byteOffset, b.byteLength);
  let at = 0;
  const u8 = () => v.getUint8(at++);
  const u32 = () => ((at += 4), v.getUint32(at - 4, true));
  const u64 = () => ((at += 8), v.getBigUint64(at - 8, true));
  const bytes = () => {
    const n = u32();
    at += n;
    return b.slice(at - n, at);
  };
  const str = () => text(bytes());
  if (text(b.subarray(0, 4)) !== "VZO1") throw new Error("not an output of the IR core");
  at = 4;
  const url = str();
  const summary = JSON.parse(str()) as Record<string, unknown>;
  const lazy: string[] = [];
  for (let n = u32(); n > 0; n--) lazy.push(str());
  const sources: Source[] = [{ url }];
  for (let n = u32(); n > 0; n--) sources.push({ data: bytes() });
  const entries: EntryDesc[] = [];
  for (let n = u32(); n > 0; n--) {
    const key = str();
    const value = bytes();
    const document = key === "zarr.json" || key.endsWith("/zarr.json");
    const compress = !document || lazy.some((prefix) => key.startsWith(prefix));
    entries.push(compress ? { key, bytes: value, compress } : { key, bytes: value });
  }
  for (let n = u32(); n > 0; n--) {
    const key = str();
    const ranges: Range[] = [];
    for (let m = u32(); m > 0; m--) {
      const tag = u8();
      if (tag === 0) ranges.push({ source: 0, offset: u64(), length: u64() });
      else if (tag === 1) ranges.push({ source: u32(), offset: u64(), length: u64() });
      else if (tag === 2) ranges.push({ data: bytes() });
      else throw new Error(`unknown part kind ${tag} in the IR core's output`);
    }
    entries.push({ key, ranges });
  }
  if (at !== b.length) throw new Error("trailing bytes in the IR core's output");
  return { sources, entries, summary };
}

// The range sources a run reads (the I/O of the read planner, design/ARCHITECTURE.md
// §3.5): bytes the caller can read itself (`readerSource`), and an http(s)
// object read under the reader policy of spec/archive.md §8.7 (`openHttpSource`), the
// twin of `HttpTransport` in python/src/vzip/ir/planner.py.

import { checkAddress, checkPolicy, type Policy, VzipError } from "../../archive.ts";
import { EtagLog, HttpResolutionError } from "../../http.ts";
import { checkHttpUrl } from "../../uri.ts";
import { checkedFetch } from "#net";
import type { ByteReader } from "../common.ts";

/** A span [start, end) of a source. */
export type Span = [start: number, end: number];

/** What a run reads: one request at a time per call of `fetch`, at most `concurrency` at once. */
export interface RangeSource {
  readonly size: number;
  /** Remote sources are planned by a cost model of the server; local ones have fixed costs. */
  readonly remote: boolean;
  readonly concurrency: number;
  /** Whether to try multi-range requests (false: the planner never packs ranges). */
  readonly multirange: boolean;
  /** The bytes the request that opened the source read, and the seconds it took. */
  readonly head?: { offset: number; data: Uint8Array; seconds: number };
  /** One request: each span's bytes, or null for a multi-range request the server did
   * not answer with its ranges (`multipart/byteranges`). */
  fetch(spans: Span[], signal?: AbortSignal): Promise<Uint8Array[] | null>;
  /** Exactly `length` bytes at `offset`. */
  read(offset: number, length: number): Promise<Uint8Array>;
  /** The source's strong ETag, if every response gave the same one (an `etag` pin). */
  etag?(): string | undefined;
  /** The read planner's settings (plan.rs; default: the core's): the seconds of wall
   * time a byte fetched costs (0.1 s per MB), and the size at or below which a remote
   * source is read whole (1 MiB). */
  readonly planner?: { byteCost?: number; wholeBelow?: number };
}

/** A source the caller reads itself (bytes in memory, a local file, a reader it built):
 * not remote, its requests sent as they come, `concurrency` 4. */
export function readerSource(
  read: ByteReader,
  size: number,
  options: { concurrency?: number; etag?: () => string | undefined } = {},
): RangeSource {
  return {
    size,
    remote: false,
    concurrency: options.concurrency ?? 4,
    multirange: false,
    fetch: (spans) => Promise.all(spans.map(([a, b]) => (b > a ? read(a, b - a) : Promise.resolve(new Uint8Array())))),
    read: (o, n) => (n > 0 ? read(o, n) : Promise.resolve(new Uint8Array())),
    ...(options.etag ? { etag: options.etag } : {}),
  };
}

/** Bytes in memory as a source. */
export function bytesSource(bytes: Uint8Array): RangeSource {
  return readerSource(async (o, n) => bytes.subarray(o, o + n), bytes.length);
}

export interface HttpSourceOptions {
  /** The reader policy (spec/archive.md §8.7); private and special hosts are refused without `allowPrivateHosts`. */
  policy?: Policy;
  /** Requests in flight at once (default 6). */
  concurrency?: number;
  /** Whether to try multi-range requests (default: under Node; a browser would send a
   * cross-origin multi-range request only after a CORS preflight). */
  multirange?: boolean;
  /** Attempts of a request that fails with 429, 5xx or a network error (default 6). */
  attempts?: number;
  /** The first retry's delay in seconds, doubled for each next one, with jitter (default 0.5). */
  baseDelay?: number;
  /** Extra request headers (such as a `User-Agent` under Node). */
  headers?: Record<string, string>;
  signal?: AbortSignal;
  /** The read planner's settings (`RangeSource.planner`). */
  planner?: { byteCost?: number; wholeBelow?: number };
}

/** The bytes the request that opens a remote source reads. */
export const OPEN = 1 << 16;
const RETRY = new Set([429, 500, 502, 503, 504]);

class Transient extends Error {
  status: number;
  retryAfter: string | null;
  constructor(status: number, retryAfter: string | null) {
    super(`HTTP ${status}`);
    this.status = status;
    this.retryAfter = retryAfter;
  }
}

const isNode = typeof process !== "undefined" && process.versions?.node !== undefined;

/** An http(s) object, read under the reader policy (spec/archive.md §8.7). */
export class HttpSource implements RangeSource {
  readonly remote = true;
  readonly concurrency: number;
  readonly multirange: boolean;
  readonly planner?: { byteCost?: number; wholeBelow?: number };
  size = 0;
  head?: { offset: number; data: Uint8Array; seconds: number };
  /** Where requests go: the URL, then where its redirects led. */
  at: string;
  /** Retries made, and requests sent. */
  retries = 0;
  requests = 0;
  private readonly etags = new EtagLog();
  private readonly send: (url: string, headers: Record<string, string>, signal?: AbortSignal) => Promise<Response>;
  private readonly refusals = new Set<string>();
  private readonly attempts: number;
  private readonly baseDelay: number;
  private readonly headers: Record<string, string>;
  private readonly signal?: AbortSignal;

  readonly url: string;

  constructor(url: string, options: HttpSourceOptions = {}) {
    this.url = url;
    const policy = options.policy ?? {};
    this.at = url;
    this.concurrency = options.concurrency ?? 6;
    this.multirange = options.multirange ?? isNode;
    if (options.planner) this.planner = options.planner;
    this.attempts = options.attempts ?? 6;
    this.baseDelay = options.baseDelay ?? 0.5;
    this.headers = options.headers ?? {};
    this.signal = options.signal;
    const allow = (u: string) => {
      try {
        checkHttpUrl(u);
      } catch (e) {
        throw new VzipError("resolution", (e as Error).message);
      }
      checkPolicy(policy, url, u);
    };
    // As the archive reader does: under Node, connections are checked (rule 3) unless
    // the policy allows private hosts; in a browser, hosts as written (allow) and
    // Private Network Access.
    const checked = policy.allowPrivateHosts
      ? undefined
      : checkedFetch((u, address) => {
        try {
          checkAddress(policy, u, address);
        } catch (e) {
          this.refusals.add((e as Error).message);
          throw e;
        }
      }, policy);
    this.send = async (u, headers, signal) => {
      const response = checked
        ? await checked(u, { headers, signal }, allow)
        : await fetch(u, { headers, signal, redirect: "follow" });
      if (response.redirected) {
        try {
          allow(response.url);
        } catch (e) {
          await response.body?.cancel();
          throw e;
        }
      }
      return response;
    };
  }

  /** Checks the URL, then reads its first `OPEN` bytes: the size, and the bytes the run is seeded with. */
  async open(): Promise<this> {
    const t = performance.now();
    const response = await this.retry(() => this.get(`bytes=0-${OPEN - 1}`));
    const seconds = (performance.now() - t) / 1000;
    if (response.status === 416) {
      await response.body?.cancel();
      this.size = 0;
      return this;
    }
    if (response.status !== 206) {
      await response.body?.cancel(); // a 200 is the whole object
      throw new HttpResolutionError(`${this.url}: HTTP ${response.status} for a range of its first bytes`);
    }
    const crange = response.headers.get("Content-Range");
    const body = await this.body(response);
    const m = crange?.trim().match(/^bytes (\d+)-(\d+)\/(\d+)$/i);
    if (m && Number(m[1]) === 0 && body.length === Number(m[2]) + 1) {
      this.size = Number(m[3]);
    } else if (crange === null && response.type === "cors") {
      // a cross-origin server that does not expose Content-Range: a short body is the
      // whole object; otherwise the size comes from a HEAD request
      this.size = body.length < OPEN ? body.length : await this.headSize();
    } else {
      throw new HttpResolutionError(`${this.url}: Content-Range ${JSON.stringify(crange)} for ${body.length} bytes of [0, ${OPEN})`);
    }
    if (body.length > this.size) throw new HttpResolutionError(`${this.url}: ${body.length} bytes of a ${this.size}-byte object`);
    this.head = { offset: 0, data: body, seconds };
    return this;
  }

  private async headSize(): Promise<number> {
    const r = await fetch(this.at, { method: "HEAD", signal: this.signal });
    const v = r.headers.get("Content-Length");
    if (r.status !== 200 || v === null || !/^\d+$/.test(v)) {
      throw new HttpResolutionError(`${this.url}: cannot determine the size (HEAD ${r.status})`);
    }
    return Number(v);
  }

  etag(): string | undefined {
    return this.etags.pin();
  }

  /** One GET of a range (redirects followed and checked); a 429 or 5xx is a Transient. */
  private async get(range: string, signal?: AbortSignal): Promise<Response> {
    this.requests++;
    const headers = { ...this.headers, Range: range, "Accept-Encoding": "identity" };
    let response: Response;
    try {
      response = await this.send(this.at, headers, signal ?? this.signal);
    } catch (e) {
      if (e instanceof VzipError || e instanceof HttpResolutionError) throw e;
      if (this.refusals.has((e as Error).message)) throw new VzipError("resolution", (e as Error).message);
      (signal ?? this.signal)?.throwIfAborted();
      // a network error (fetch's TypeError, a Node socket error) is retried; others (a
      // proxy the reader cannot check, a bad redirect) are not
      if (e instanceof TypeError || typeof (e as { code?: unknown }).code === "string") throw e;
      throw new HttpResolutionError(`${this.url}: ${(e as Error).message}`);
    }
    if (response.redirected && response.url) this.at = response.url; // later requests go where the redirects led
    if (RETRY.has(response.status)) {
      await response.body?.cancel();
      throw new Transient(response.status, response.headers.get("Retry-After"));
    }
    if (response.status === 200 || response.status === 206) {
      const encoding = response.headers.get("Content-Encoding");
      if (encoding !== null && encoding.trim().toLowerCase() !== "identity") {
        await response.body?.cancel();
        throw new HttpResolutionError(`${this.url}: response has Content-Encoding ${JSON.stringify(encoding)}`);
      }
      const etag = response.headers.get("ETag");
      if (etag !== null && etag.includes(",")) {
        await response.body?.cancel();
        throw new HttpResolutionError(`${this.url}: more than one ETag field`);
      }
      this.etags.saw(etag);
    }
    return response;
  }

  private async body(response: Response): Promise<Uint8Array> {
    return new Uint8Array(await response.arrayBuffer());
  }

  private async retry<T>(f: () => Promise<T>): Promise<T> {
    for (let attempt = 0; ; attempt++) {
      try {
        return await f();
      } catch (e) {
        if (e instanceof VzipError || e instanceof HttpResolutionError || (e as Error).name === "AbortError") throw e;
        const last = attempt >= this.attempts - 1;
        if (e instanceof Transient) {
          if (last) throw new HttpResolutionError(`${this.url}: HTTP ${e.status} after ${this.attempts} attempts`);
          await this.wait(attempt, e.retryAfter);
        } else {
          if (last) throw new HttpResolutionError(`${this.url}: ${(e as Error).message}${causeOf(e)}`);
          await this.wait(attempt, null);
        }
      }
    }
  }

  private async wait(attempt: number, retryAfter: string | null): Promise<void> {
    let delay = this.baseDelay * 2 ** attempt * (0.5 + Math.random());
    if (retryAfter !== null) {
      const s = retryAfter.trim();
      if (/^\d+$/.test(s)) delay = Math.max(delay, Number(s));
      else {
        const when = Date.parse(s);
        if (!Number.isNaN(when)) delay = Math.max(delay, (when - Date.now()) / 1000);
      }
    }
    this.retries++;
    await new Promise((resolve) => setTimeout(resolve, Math.min(delay, 120) * 1000));
  }

  async read(offset: number, length: number): Promise<Uint8Array> {
    if (length === 0) return new Uint8Array();
    const h = this.head;
    if (h && offset >= h.offset && offset + length <= h.offset + h.data.length) {
      return h.data.subarray(offset - h.offset, offset - h.offset + length);
    }
    return this.single(offset, offset + length);
  }

  private single(a: number, b: number, signal?: AbortSignal): Promise<Uint8Array> {
    return this.retry(async () => {
      const response = await this.get(`bytes=${a}-${b - 1}`, signal);
      if (response.status !== 206) {
        await response.body?.cancel(); // a 200 is the whole object: never read it
        throw new HttpResolutionError(`${this.url}: HTTP ${response.status} for [${a}, ${b})`);
      }
      const crange = response.headers.get("Content-Range");
      const body = await this.body(response);
      const m = crange?.trim().match(/^bytes (\d+)-(\d+)\/(\d+|\*)$/i);
      const hidden = crange === null && response.type === "cors";
      if (body.length !== b - a || (!hidden && (!m || Number(m[1]) !== a || Number(m[2]) !== b - 1))) {
        throw new HttpResolutionError(`${this.url}: HTTP 206 with Content-Range ${JSON.stringify(crange)}, ${body.length} bytes for [${a}, ${b})`);
      }
      return body;
    });
  }

  async fetch(spans: Span[], signal?: AbortSignal): Promise<Uint8Array[] | null> {
    if (spans.length === 1) return [await this.single(spans[0][0], spans[0][1], signal)];
    const range = "bytes=" + spans.map(([a, b]) => `${a}-${b - 1}`).join(",");
    // A multi-range request is tried once: a server that does not answer it with
    // multipart/byteranges, or fails on it, gets single ranges instead (which retry).
    let got: Map<string, Uint8Array> | null;
    try {
      const response = await this.get(range, signal);
      const ctype = response.headers.get("Content-Type") ?? "";
      if (response.status !== 206 || !/multipart\/byteranges/i.test(ctype)) {
        await response.body?.cancel(); // not the ranges (a 200 is the whole object): never read it
        return null;
      }
      got = multipartParts(await this.body(response), ctype);
    } catch (e) {
      if (e instanceof VzipError || e instanceof HttpResolutionError || (e as Error).name === "AbortError") throw e;
      return null;
    }
    if (got === null) return null;
    return spans.map(([a, b]) => {
      const data = got.get(`${a}-${b}`);
      if (data === undefined) throw new HttpResolutionError(`${this.url}: the multipart answer lacks [${a}, ${b})`);
      return data;
    });
  }
}

function causeOf(e: unknown): string {
  const cause = (e as { cause?: { message?: string; code?: string } }).cause;
  return cause ? ` (${cause.code ?? cause.message ?? String(cause)})` : "";
}

/** Opens an http(s) object for a run: checks the URL by the policy, then reads its first bytes. */
export function openHttpSource(url: string, options: HttpSourceOptions = {}): Promise<HttpSource> {
  try {
    checkHttpUrl(url);
  } catch (e) {
    return Promise.reject(new VzipError("resolution", (e as Error).message));
  }
  if (!/^https?:/i.test(url)) return Promise.reject(new VzipError("resolution", `${url}: not an http(s) URL`));
  try {
    checkPolicy(options.policy ?? {}, url, url); // spec/archive.md §8.7, before any request
  } catch (e) {
    return Promise.reject(e);
  }
  return new HttpSource(url, options).open();
}

function indexOf(hay: Uint8Array, needle: Uint8Array, from: number): number {
  outer: for (let i = from; i + needle.length <= hay.length; i++) {
    for (let j = 0; j < needle.length; j++) if (hay[i + j] !== needle[j]) continue outer;
    return i;
  }
  return -1;
}

const latin1 = (b: Uint8Array) => Array.from(b, (c) => String.fromCharCode(c)).join("");

/** The parts of a multipart/byteranges body, by "start-end" (end exclusive). */
export function multipartParts(body: Uint8Array, ctype: string): Map<string, Uint8Array> {
  const m = ctype.match(/boundary=("?)([^";]+)\1/i);
  const out = new Map<string, Uint8Array>();
  if (!m) return out;
  const sep = new TextEncoder().encode(`--${m[2].trim()}`);
  const crlf2 = new TextEncoder().encode("\r\n\r\n");
  let pos = 0;
  for (;;) {
    const k = indexOf(body, sep, pos);
    if (k < 0 || (body[k + sep.length] === 0x2d && body[k + sep.length + 1] === 0x2d)) return out;
    const headEnd = indexOf(body, crlf2, k);
    if (headEnd < 0) return out;
    let start: number | undefined;
    let end = 0;
    for (const line of latin1(body.subarray(k + sep.length, headEnd)).split("\r\n")) {
      const i = line.indexOf(":");
      if (i < 0 || line.slice(0, i).trim().toLowerCase() !== "content-range") continue;
      const r = line.slice(i + 1).trim().match(/^bytes (\d+)-(\d+)\//i);
      if (r) [start, end] = [Number(r[1]), Number(r[2]) + 1];
    }
    if (start === undefined) return out;
    const at = headEnd + 4;
    if (at + end - start > body.length) return out;
    out.set(`${start}-${end}`, body.slice(at, at + end - start));
    pos = at + end - start;
  }
}

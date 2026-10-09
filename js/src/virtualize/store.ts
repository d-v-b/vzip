// Store inputs (spec/virtualize.md §1.4–§1.6): the listing, object reads, the
// strict JSON reader, and the output of a store profile (one url source per
// chunk object). Shared by the N5 (§9), Zarr v2 (§10) and OME-Zarr (§11) profiles.

import { isUriReference } from "../uri.ts";
import { CONVENTION_KEY, declare, type Profile, stringifyJson } from "./common.ts";
import type { ArchiveDesc, EntryDesc } from "../writer.ts";

/** The store input is rejected for a reason no single store profile owns (§1.4–§1.6). */
export class StoreError extends Error {}

/** A resource limit of this implementation was exceeded: a failure, not a rejection (§1.2, §12). */
export class StoreLimitError extends Error {}

/** The listing or an object could not be read: a failure (§1.4). */
export class StoreReadError extends Error {}

const reject = (message: string): never => {
  throw new StoreError(message);
};

export const MAX_SAFE = Number.MAX_SAFE_INTEGER;
export const MAX_DOCUMENT = 1 << 24;
const MAX_DEPTH = 256;
/** The browser implementation's limit on listed objects (§14). */
export const MAX_OBJECTS = 100000;

const SPLIT = /^(?:([^:/?#]+):)?(?:\/\/([^/?#]*))?([^?#]*)(?:\?([^#]*))?(?:#(.*))?$/;
const UNRESERVED = /[A-Za-z0-9\-._~]/;
const PATH_SAFE = /[A-Za-z0-9\-._~!$&'()*+,;=:@/]/;
const utf8 = new TextEncoder();

function pct(s: string, safe: RegExp): string {
  let out = "";
  for (const b of utf8.encode(s)) {
    const c = String.fromCharCode(b);
    out += b < 128 && safe.test(c) ? c : `%${b.toString(16).toUpperCase().padStart(2, "0")}`;
  }
  return out;
}

/** The URL of the object with relative key `key` (§1.4). */
export function objectUrl(storeUrl: string, key: string): string {
  return storeUrl + pct(key, PATH_SAFE);
}

/** `enc` of §1.5. */
export function queryEncode(s: string): string {
  return pct(s, UNRESERVED);
}

/** [scheme, authority, path] of a valid store URL (§1.4), or a rejection. */
export function checkStoreUrl(url: string): [string, string, string] {
  const m = url.match(SPLIT);
  if (!isUriReference(url) || m === null) reject(`${JSON.stringify(url)} is not a valid URI`);
  const [, scheme, authority, path, query, fragment] = m!;
  if (scheme === undefined || !["http", "https"].includes(scheme.toLowerCase())) {
    reject(`store URL ${url} is not http or https`);
  }
  if (authority === undefined || authority.includes("@")) reject(`store URL ${url} has no authority, or has userinfo`);
  if (query !== undefined || fragment !== undefined) reject(`store URL ${url} has a query or a fragment`);
  if (!path.endsWith("/")) reject(`store URL ${url} does not end in /`);
  return [scheme, authority, path];
}

function percentDecode(s: string): Uint8Array {
  const out: number[] = [];
  for (let i = 0; i < s.length; ) {
    if (s[i] === "%") {
      out.push(parseInt(s.slice(i + 1, i + 3), 16));
      i += 3;
    } else {
      out.push(s.charCodeAt(i));
      i += 1;
    }
  }
  return Uint8Array.from(out);
}

/** [endpoint, prefix P] of a store URL (§1.5). */
export function listingEndpoint(url: string): [string, string] {
  const [scheme, authority, path] = checkStoreUrl(url);
  let host = authority;
  if (host.startsWith("[")) host = host.slice(0, host.indexOf("]") + 1);
  else if (host.includes(":")) host = host.slice(0, host.lastIndexOf(":"));
  host = host.toLowerCase();
  const labels = host.split(".");
  const virtual = host.endsWith(".amazonaws.com") && labels.slice(1).some((l) => l === "s3" || l.startsWith("s3-"));
  let endpoint: string;
  let prefixPath: string;
  if (virtual) {
    endpoint = `${scheme}://${authority}/`;
    prefixPath = path.slice(1);
  } else {
    if (path === "/") reject(`path-style store URL ${url} names no bucket`);
    const rest = path.slice(1);
    const i = rest.indexOf("/");
    const bucket = rest.slice(0, i);
    if (bucket === "") reject(`path-style store URL ${url} has an empty bucket`);
    endpoint = `${scheme}://${authority}/${bucket}/`;
    prefixPath = rest.slice(i + 1);
  }
  let prefix = "";
  try {
    prefix = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(percentDecode(prefixPath));
  } catch {
    reject(`the key prefix of ${url} is not UTF-8`);
  }
  return [endpoint, prefix];
}

// ---------------------------------------------------------------- listing bodies (§1.5)

function isChar(c: number): boolean {
  return c === 0x9 || c === 0xa || c === 0xd || (c >= 0x20 && c <= 0xd7ff) || (c >= 0xe000 && c <= 0xfffd) ||
    (c >= 0x10000 && c <= 0x10ffff);
}

const NAME_START = /[A-Za-z_:]/;
const NAME_CHAR = /[A-Za-z0-9_:.-]/;
const WS = new Set([" ", "\t", "\r", "\n"]);
const ENTITIES: Record<string, string> = { lt: "<", gt: ">", amp: "&", quot: '"', apos: "'" };

export interface XmlElement {
  name: string;
  children: XmlElement[];
  text: string[];
  /** The attributes, [name, value] in document order, values with references replaced. */
  attributes: [string, string][];
}

/** The XML subset of §1.5 (the same algorithm as the Python reference); `what` names the
 * document in rejections, and `fail` makes the error. */
class Xml {
  pos = 0;
  depth = 0; // of the element being read
  readonly s: string;
  readonly what: string;
  readonly error: (m: string) => Error;
  constructor(s: string, what = "listing", error: (m: string) => Error = (m) => new StoreError(m)) {
    this.s = s;
    this.what = what;
    this.error = error;
  }

  fail(why: string): never {
    throw this.error(`${this.what} is not well formed: ${why} at ${this.pos}`);
  }
  ws() {
    while (this.pos < this.s.length && WS.has(this.s[this.pos])) this.pos++;
  }
  at(t: string): boolean {
    return this.s.startsWith(t, this.pos);
  }
  chars(t: string) {
    for (const ch of t) {
      const c = ch.codePointAt(0)!;
      if (!isChar(c)) this.fail(`character U+${c.toString(16).toUpperCase().padStart(4, "0")}`);
    }
  }
  comment() {
    const end = this.s.indexOf("--", this.pos + 4);
    if (end < 0 || !this.s.startsWith("-->", end)) this.fail("bad comment");
    this.chars(this.s.slice(this.pos + 4, end));
    this.pos = end + 3;
  }
  misc() {
    for (;;) {
      this.ws();
      if (this.at("<!--")) this.comment();
      else return;
    }
  }
  name(): string {
    const start = this.pos;
    if (start >= this.s.length || !NAME_START.test(this.s[start])) this.fail("expected a name");
    this.pos++;
    while (this.pos < this.s.length && NAME_CHAR.test(this.s[this.pos])) this.pos++;
    return this.s.slice(start, this.pos);
  }
  reference(): string {
    const end = this.s.indexOf(";", this.pos);
    if (end < 0) this.fail("unterminated reference");
    const body = this.s.slice(this.pos + 1, end);
    this.pos = end + 1;
    if (Object.hasOwn(ENTITIES, body)) return ENTITIES[body];
    let v: number;
    if (/^#x[0-9a-fA-F]+$/.test(body)) v = body.length < 12 ? parseInt(body.slice(2), 16) : -1;
    else if (/^#[0-9]+$/.test(body)) v = body.length < 12 ? parseInt(body.slice(1), 10) : -1;
    else this.fail(`unknown reference &${body};`);
    if (!isChar(v!)) this.fail(`reference &${body}; is not a character`);
    return String.fromCodePoint(v!);
  }
  chardata(stop: string): string {
    const start = this.pos;
    while (this.pos < this.s.length && this.s[this.pos] !== "<" && this.s[this.pos] !== "&" &&
      this.s[this.pos] !== stop) this.pos++;
    const t = this.s.slice(start, this.pos);
    this.chars(t);
    return t;
  }
  element(): XmlElement {
    if (!this.at("<")) this.fail("expected an element");
    if (++this.depth > MAX_DEPTH) this.fail(`elements nest more than ${MAX_DEPTH} deep`);
    const el = this.elementBody();
    this.depth--;
    return el;
  }
  elementBody(): XmlElement {
    this.pos++;
    const el: XmlElement = { name: this.name(), children: [], text: [], attributes: [] };
    const s = this.s;
    for (;;) {
      if (this.pos < s.length && WS.has(s[this.pos])) {
        this.ws();
        if (this.pos < s.length && NAME_START.test(s[this.pos])) {
          const name = this.name();
          this.ws();
          if (!this.at("=")) this.fail("expected =");
          this.pos++;
          this.ws();
          const q = this.pos < s.length ? s[this.pos] : "";
          if (q !== "'" && q !== '"') this.fail("expected a quoted value");
          this.pos++;
          const value: string[] = [];
          for (;;) {
            value.push(this.chardata(q));
            if (this.at("&")) value.push(this.reference());
            else if (this.at(q)) {
              this.pos++;
              break;
            } else this.fail("bad attribute value");
          }
          el.attributes.push([name, value.join("")]);
          continue;
        }
      }
      break;
    }
    if (this.at("/>")) {
      this.pos += 2;
      return el;
    }
    if (!this.at(">")) this.fail("expected >");
    this.pos++;
    for (;;) {
      if (this.pos >= s.length) this.fail("unterminated element");
      if (this.at("</")) {
        this.pos += 2;
        if (this.name() !== el.name) this.fail("mismatched end tag");
        this.ws();
        if (!this.at(">")) this.fail("expected >");
        this.pos++;
        return el;
      }
      if (this.at("<!--")) this.comment();
      else if (this.at("<!") || this.at("<?")) this.fail("unsupported markup");
      else if (this.at("<")) el.children.push(this.element());
      else if (this.at("&")) el.text.push(this.reference());
      else {
        const t = this.chardata("<");
        if (t.includes("]]>")) this.fail("]]> in character data");
        el.text.push(t);
      }
    }
  }
  document(): XmlElement {
    this.ws();
    if (this.at("<?xml") && this.pos + 5 < this.s.length && WS.has(this.s[this.pos + 5])) {
      const end = this.s.indexOf("?>", this.pos + 5);
      if (end < 0) this.fail("unterminated XML declaration");
      this.chars(this.s.slice(this.pos, end));
      this.pos = end + 2;
    }
    this.misc();
    const root = this.element();
    this.misc();
    if (this.pos !== this.s.length) this.fail("content after the root element");
    return root;
  }
}

/** The root element of a document in the XML subset of §1.5, or a rejection (naming `what`). */
export function parseXml(s: string, what: string, error: (m: string) => Error): XmlElement {
  return new Xml(s, what, error).document();
}

function text(el: XmlElement): string {
  if (el.children.length) reject(`listing: <${el.name}> has a child element`);
  return el.text.join("");
}

/** The objects listed by one ListObjectsV2 response, and the continuation token if truncated (§1.5). */
export function parseListing(body: Uint8Array, prefix: string): { objects: [string, number][]; token?: string } {
  let s = "";
  try {
    s = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(body);
  } catch {
    reject("listing is not UTF-8");
  }
  const root = new Xml(s).document();
  if (root.name !== "ListBucketResult") reject(`listing root element is <${root.name}>, not <ListBucketResult>`);
  const truncated = root.children.filter((c) => c.name === "IsTruncated");
  if (truncated.length !== 1 || !["true", "false"].includes(text(truncated[0]))) {
    reject("listing needs exactly one IsTruncated of true or false");
  }
  const tokens = root.children.filter((c) => c.name === "NextContinuationToken");
  if (tokens.length > 1) reject("listing has more than one NextContinuationToken");
  const token = tokens.length ? text(tokens[0]) : undefined;
  const objects: [string, number][] = [];
  for (const c of root.children) {
    if (c.name !== "Contents") continue;
    const keys = c.children.filter((x) => x.name === "Key");
    const sizes = c.children.filter((x) => x.name === "Size");
    if (keys.length !== 1 || sizes.length !== 1) reject("listing Contents needs exactly one Key and one Size");
    const key = text(keys[0]);
    const size = text(sizes[0]);
    if (!/^[0-9]+$/.test(size) || size.replace(/^0+/, "").length > 16 || Number(size) > MAX_SAFE) {
      reject(`listing Size ${JSON.stringify(size.slice(0, 40))} is not a size`);
    }
    if (!key.startsWith(prefix)) reject(`listed key ${JSON.stringify(key)} is outside the prefix ${JSON.stringify(prefix)}`);
    objects.push([key, Number(size)]);
  }
  if (text(truncated[0]) === "true") {
    if (!token) reject("truncated listing without a NextContinuationToken");
    // A page that lists nothing could be followed forever.
    if (objects.length === 0) reject("truncated listing without a Contents");
    return { objects, token };
  }
  return { objects };
}

// ---------------------------------------------------------------- JSON (§1.6)

export type Json = null | boolean | number | string | Json[] | { [k: string]: Json };

function checkNumbers(v: unknown): void {
  if (typeof v === "number") {
    if (!Number.isFinite(v)) reject("JSON number is not finite in binary64");
  } else if (Array.isArray(v)) {
    for (const x of v) checkNumbers(x);
  } else if (v !== null && typeof v === "object") {
    for (const x of Object.values(v)) checkNumbers(x);
  }
}

// JSON.rawJSON and JSON.isRawJSON (Node 21+; not yet in TypeScript's lib).
const RawJSON = JSON as unknown as { rawJSON(text: string): object; isRawJSON(v: unknown): boolean };
const INTEGER = /^-?(?:0|[1-9][0-9]*)$/;

/** JSON.parse gave the reviver no source text (an engine without JSON.parse source text
 * access): an integer beyond 2^53 − 1 would silently lose digits, so reading fails. This
 * is a failure of the implementation, not a rejection of the input. */
export class NoSourceText extends Error {
  constructor() {
    super("JSON.parse gives the reviver no source text (needs JSON.parse source text access, as in Node 21+)");
  }
}

/** The JSON.parse reviver that keeps an integer literal beyond 2^53 − 1 exact, as the
 * raw JSON of its digits, so that a copied value keeps every digit (as Python's
 * parser does), and reads the literal `-0` as the integer 0. A literal whose binary64
 * value is infinite stays a number, which §1.6 rejects. */
export function keepIntegers(_key: string, value: unknown, context?: { source?: string }): unknown {
  if (typeof value !== "number") return value;
  if (context?.source === undefined) throw new NoSourceText();
  // A non-integer literal of -0 (such as -0.0) stays -0.0, as Python writes it.
  if (!INTEGER.test(context.source)) return Object.is(value, -0) ? RawJSON.rawJSON("-0.0") : value;
  if (Object.is(value, -0)) return 0;
  if (Math.abs(value) <= MAX_SAFE || !Number.isFinite(value)) return value;
  return RawJSON.rawJSON(context.source);
}

/** Is `v` an integer that the reader kept exact (beyond 2^53 − 1)? */
export function isRawInteger(v: unknown): boolean {
  return v !== null && typeof v === "object" && RawJSON.isRawJSON(v);
}

/** A metadata document by §1.6, or a rejection. */
export function parseJson(data: Uint8Array): Json {
  if (data.length > MAX_DOCUMENT) reject(`JSON document of ${data.length} bytes exceeds ${MAX_DOCUMENT}`);
  if (data[0] === 0xef && data[1] === 0xbb && data[2] === 0xbf) reject("JSON document starts with a byte order mark");
  let s = "";
  try {
    s = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(data);
  } catch {
    reject("JSON document is not UTF-8");
  }
  if (nesting(s) > MAX_DEPTH) reject(`JSON nests more than ${MAX_DEPTH} deep`);
  let v: Json = null;
  try {
    v = JSON.parse(s, keepIntegers);
  } catch (e) {
    if (e instanceof NoSourceText) throw e;
    reject(`invalid JSON: ${(e as Error).message}`);
  }
  checkNumbers(v);
  return v;
}

/** The deepest nesting of brackets in `s`, outside strings: a character loop, since a
 * regular expression overflows V8's stack on strings of millions of characters. After a
 * quote with no closing quote, later quotes are plain characters (as Python's scan reads them). */
export function nesting(s: string): number {
  let depth = 0, deepest = 0, open = true;
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c === 0x22 && open) {
      let j = i + 1;
      while (j < s.length && s.charCodeAt(j) !== 0x22) j += s.charCodeAt(j) === 0x5c ? 2 : 1;
      if (j >= s.length) open = false;
      else i = j;
    } else if (c === 0x5b || c === 0x7b) {
      deepest = Math.max(deepest, ++depth);
    } else if (c === 0x5d || c === 0x7d) {
      depth--;
    }
  }
  return deepest;
}

/** A value for a message (`JSON.stringify` gives undefined for undefined). */
export function show(v: unknown): string {
  return String(JSON.stringify(v));
}

/** Is `v` a JSON number (an integer kept exact included)? */
export function isNumber(v: unknown): boolean {
  return typeof v === "number" || isRawInteger(v);
}

/** A number as the layout reads it (§1.6): its binary64 value. */
export function num(v: unknown): number {
  return isRawInteger(v) ? Number((v as { rawJSON: string }).rawJSON) : (v as number);
}

/** `v` as an integer of §1.6 within [lo, hi], or undefined. */
export function asInt(v: unknown, lo = -MAX_SAFE, hi = MAX_SAFE): number | undefined {
  if (isRawInteger(v)) v = num(v) + 0; // + 0: -0.0 reads as 0
  if (typeof v !== "number" || !Number.isInteger(v) || Math.abs(v) > MAX_SAFE) return undefined;
  return v >= lo && v <= hi ? v : undefined;
}

export function isObject(v: unknown): v is { [k: string]: Json } {
  return v !== null && typeof v === "object" && !Array.isArray(v) && !RawJSON.isRawJSON(v);
}

/** Object.hasOwn, for JSON objects (whose members may include `__proto__`). */
export function has(o: object, k: string): boolean {
  return Object.hasOwn(o, k);
}

/** A copy of `o` without the members `drop`. */
export function without(o: { [k: string]: Json }, drop: string[]): { [k: string]: Json } {
  return Object.fromEntries(Object.entries(o).filter(([k]) => !drop.includes(k)));
}

// ---------------------------------------------------------------- stores (§1.4, §1.5)

/** Code point (UTF-8 byte) order, unlike JavaScript's UTF-16 order. */
export function compareKeys(a: string, b: string): number {
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i++) {
    const x = a.codePointAt(i)!;
    const y = b.codePointAt(i)!;
    if (x !== y) return x < y ? -1 : 1;
    if (x > 0xffff) i++;
  }
  return a.length - b.length;
}

function ignored(rel: string): boolean {
  return rel === "" || rel.endsWith("/") || rel.split("/").some((s) => s === "" || s === "." || s === "..");
}

/** An ignored object whose key is not recorded (§1.4): an empty one whose relative key is empty or ends in /. */
function folderMarker(rel: string, size: number): boolean {
  return size === 0 && (rel === "" || rel.endsWith("/"));
}

export interface Store {
  url: string;
  /** Relative key → size, of the objects that are not ignored (§1.4). */
  objects: Map<string, number>;
  /** The relative keys of the ignored objects that are recorded (§1.4). */
  ignored?: string[];
  /** The relative keys of the empty objects whose keys end in `/` (directories, to the SAFE profile). */
  folders?: string[];
  listed: number;
  requests: number;
  read(key: string): Promise<Uint8Array>;
  /** The bytes [offset, offset + length) of the object `key` (spec/virtualize/safe/profile.md §12.2). */
  readRange?(key: string, offset: number, length: number): Promise<Uint8Array>;
  /** Documents read at a time by `prefetchDocuments`; read one at a time if absent or 1. */
  concurrency?: number;
  /** Bytes read ahead and not yet taken, at most (but always one document). */
  prefetchBytes?: number;
  prefetch?: Prefetch;
  /** Reads kept for a second read of the same key (`readDocument` with `keep`). */
  kept?: Map<string, Promise<Settled>>;
}

/** Concurrent document reads by default: 16. Over HTTP/1.1 a browser opens about 6
 * connections per host and queues the rest, so up to 16 keeps those 6 busy at the cost
 * of a short queue; over HTTP/2 the requests share one connection and 16 run at once.
 * Node (undici) opens a connection per request in flight. */
export const CONCURRENCY = 16;
export const PREFETCH_BYTES = 64 << 20;

type Settled = { ok: true; value: Uint8Array } | { ok: false; error: unknown };

const settle = (p: Promise<Uint8Array>): Promise<Settled> =>
  p.then((value) => ({ ok: true as const, value }), (error) => ({ ok: false as const, error }));

/** Reads the objects of a plan (keys, in the order a profile will read them) ahead of
 * the profile, `concurrency` at a time, holding at most `budget` bytes read or being read
 * and not yet taken (but always one object). Each result, the bytes or the error the read
 * threw, is handed over once, by `take`, when the profile reads that key: so a failed read
 * fails the profile exactly where the sequential read would, and only if it reads that key. */
export class Prefetch {
  #queue: string[];
  #pos = 0;
  #size: Map<string, number>;
  #state = new Map<string, "queued" | "started">();
  #result = new Map<string, Promise<Settled>>();
  #held = 0;
  #running = 0;
  #closed = false;
  #read: (key: string) => Promise<Uint8Array>;
  #concurrency: number;
  #budget: number;
  #wanted?: (key: string) => boolean | undefined;

  /** `wanted(key)` is false for a key the profile will not read after all, and undefined
   * while that is not known yet (asked again after each document the profile reads). */
  constructor(
    read: (key: string) => Promise<Uint8Array>,
    plan: [string, number][],
    concurrency: number,
    budget: number,
    wanted?: (key: string) => boolean | undefined,
  ) {
    this.#read = read;
    this.#concurrency = concurrency;
    this.#budget = budget;
    this.#wanted = wanted;
    this.#queue = plan.map(([k]) => k);
    this.#size = new Map(plan);
    for (const [k] of plan) this.#state.set(k, "queued");
    this.#pump();
  }

  #pump() {
    while (!this.#closed && this.#running < this.#concurrency && this.#pos < this.#queue.length) {
      const key = this.#queue[this.#pos];
      if (this.#state.get(key) !== "queued") { // taken by the profile before it was started
        this.#pos++;
        continue;
      }
      const wanted = this.#wanted === undefined ? true : this.#wanted(key);
      if (wanted === undefined) return;
      if (!wanted) {
        this.#state.delete(key);
        this.#pos++;
        continue;
      }
      const size = this.#size.get(key)!;
      if (this.#held > 0 && this.#held + size > this.#budget) return;
      this.#pos++;
      this.#state.set(key, "started");
      this.#held += size;
      this.#running++;
      const r = settle(this.#read(key));
      this.#result.set(key, r);
      void r.then(() => {
        this.#running--;
        this.#pump();
      });
    }
  }

  /** The result of reading `key`; undefined if it was not planned, was taken already, or
   * was not started (the caller reads it). */
  take(key: string): Promise<Settled> | undefined {
    const state = this.#state.get(key);
    this.#state.delete(key);
    let r: Promise<Settled> | undefined;
    if (state === "started") {
      r = this.#result.get(key)!;
      this.#result.delete(key);
      void r.then(() => {
        this.#held -= this.#size.get(key)!;
        this.#pump();
      });
    }
    this.#pump(); // the profile has read on: `wanted` may now know more
    return r;
  }

  close() {
    this.#closed = true;
  }
}

/** Starts reading the documents `keys`, in that order (the order the profile reads them),
 * `store.concurrency` at a time; once per store. Empty documents and documents §1.6 rejects
 * for their size are not read. */
export function prefetchDocuments(store: Store, keys: Iterable<string>, wanted?: (key: string) => boolean | undefined) {
  if ((store.concurrency ?? 1) <= 1 || store.prefetch !== undefined) return;
  const plan: [string, number][] = [];
  for (const k of new Set(keys)) {
    const size = store.objects.get(k)!;
    if (size > 0 && size <= MAX_DOCUMENT) plan.push([k, size]);
  }
  store.prefetch = new Prefetch((k) => store.read(k), plan, store.concurrency!, store.prefetchBytes ?? PREFETCH_BYTES, wanted);
}

/** Stops reading ahead. */
export function closeStore(store: Store) {
  store.prefetch?.close();
}

/** Adds listed objects to `store`, rejecting a key listed twice (§1.5), and
 * records the keys of the ignored ones that are not folder markers (§1.4). */
export function addListed(store: Store, listed: [string, number][], prefix: string, seen: Set<string>) {
  store.ignored ??= [];
  store.folders ??= [];
  for (const [key, size] of listed) {
    if (seen.has(key)) reject(`key ${JSON.stringify(key)} is listed twice`);
    seen.add(key);
    const rel = key.slice(prefix.length);
    if (!ignored(rel)) store.objects.set(rel, size);
    else if (!folderMarker(rel, size)) store.ignored.push(rel);
    else if (rel && !ignored(rel.slice(0, -1))) store.folders.push(rel);
  }
  store.listed = seen.size;
}

/** The JSON document at `key` (§1.6). With `keep`, its bytes are kept for the next read
 * of `key` (each document is read once). */
export async function readDocument(store: Store, key: string, { keep = false } = {}): Promise<Json> {
  const size = store.objects.get(key)!;
  if (size > MAX_DOCUMENT) reject(`${key}: JSON document of ${size} bytes exceeds ${MAX_DOCUMENT}`);
  if (!size) return parseJson(new Uint8Array(0));
  const kept = (store.kept ??= new Map());
  const r = kept.get(key) ?? store.prefetch?.take(key) ?? settle(store.read(key));
  if (keep) kept.set(key, r);
  else kept.delete(key);
  const s = await r;
  if (!s.ok) throw s.error;
  return parseJson(s.value);
}

export type Fetch = (url: string, init?: RequestInit) => Promise<Response>;

/** A store listed by S3 ListObjectsV2 (§1.5). Fails (StoreLimitError) past `maxObjects`. */
export async function openHttpStore(
  url: string,
  { fetch: f = (u, i) => fetch(u, i), maxObjects = MAX_OBJECTS, concurrency = CONCURRENCY }: {
    fetch?: Fetch;
    maxObjects?: number;
    /** Documents read at a time (CONCURRENCY). */
    concurrency?: number;
  } = {},
): Promise<Store> {
  const [endpoint, prefix] = listingEndpoint(url);
  const store: Store = {
    url,
    objects: new Map(),
    listed: 0,
    requests: 0,
    concurrency,
    async read(key) {
      const size = this.objects.get(key)!;
      if (size === 0) return new Uint8Array(0);
      const u = objectUrl(url, key);
      let r: Response;
      try {
        r = await f(u, { headers: { Range: `bytes=0-${size - 1}` } });
      } catch (e) {
        throw new StoreReadError(`${u}: ${(e as Error).message}`);
      }
      if (r.status !== 200 && r.status !== 206) throw new StoreReadError(`${u}: HTTP ${r.status}`);
      const data = new Uint8Array(await r.arrayBuffer());
      if (data.length !== size) throw new StoreReadError(`${u}: read ${data.length} bytes, the listing says ${size}`);
      return data;
    },
    async readRange(key, offset, length) {
      if (length === 0) return new Uint8Array(0);
      const u = objectUrl(url, key);
      let r: Response;
      try {
        r = await f(u, { headers: { Range: `bytes=${offset}-${offset + length - 1}` } });
      } catch (e) {
        throw new StoreReadError(`${u}: ${(e as Error).message}`);
      }
      if (r.status !== 206 && !(r.status === 200 && offset === 0)) throw new StoreReadError(`${u}: HTTP ${r.status}`);
      const data = new Uint8Array(await r.arrayBuffer());
      if (data.length !== length) throw new StoreReadError(`${u}: read ${data.length} bytes at ${offset}, not ${length}`);
      return data;
    },
  };
  const seen = new Set<string>();
  const tokens = new Set<string>();
  let token: string | undefined;
  for (;;) {
    let q = `?list-type=2&prefix=${queryEncode(prefix)}`;
    if (token !== undefined) q += `&continuation-token=${queryEncode(token)}`;
    store.requests++;
    let r: Response;
    try {
      r = await f(endpoint + q);
    } catch (e) {
      throw new StoreReadError(`${endpoint + q}: ${(e as Error).message}`);
    }
    if (r.status === 408 || r.status === 429 || r.status >= 500) {
      throw new StoreReadError(`${endpoint + q}: HTTP ${r.status}`);
    }
    if (r.status !== 200) reject(`the store has no listing: ${endpoint + q} answered HTTP ${r.status}`);
    const listing = parseListing(new Uint8Array(await r.arrayBuffer()), prefix);
    addListed(store, listing.objects, prefix, seen);
    if (store.listed > maxObjects) throw new StoreLimitError(`the store lists more than ${maxObjects} objects`);
    token = listing.token;
    if (token === undefined) break;
    // The listing would repeat itself (§1.5).
    if (tokens.has(token)) reject(`the listing gives the continuation token ${JSON.stringify(token.slice(0, 80))} again`);
    tokens.add(token);
  }
  return store;
}

/** The store profile that the root keys select (§1.4). */
export function chooseProfile(store: Store): "n5" | "zarr2" | "safe" {
  if (store.objects.has(".zarray") || store.objects.has(".zgroup")) return "zarr2";
  if (store.objects.has("attributes.json")) return "n5";
  if (store.objects.has("manifest.safe")) return "safe";
  return reject(
    "not an N5, Zarr v2 or SAFE store: the root has no .zarray, .zgroup, attributes.json or manifest.safe",
  );
}

// ---------------------------------------------------------------- hierarchy and output

export function depth(path: string): number {
  return path === "" ? 0 : path.split("/").length;
}

export function join(parent: string, name: string): string {
  return parent === "" ? name : `${parent}/${name}`;
}

export function parentOf(path: string): string {
  const i = path.lastIndexOf("/");
  return i < 0 ? "" : path.slice(0, i);
}

/** Paths in classification order: by number of segments, then by key (spec/virtualize/n5.md §2, spec/virtualize/zarr2.md §2). */
export function byDepth(paths: Iterable<string>): string[] {
  return [...paths].sort((a, b) => depth(a) - depth(b) || compareKeys(a, b));
}

/** Paths (the root `""` included), each with a value, as a tree of their segments:
 * finding a key's nearest ancestor among them takes time linear in the key's
 * length, without rebuilding any prefix. */
export class PathTrie<T> {
  children = new Map<string, PathTrie<T>>();
  value: T | undefined = undefined;

  add(path: string, value: T): void {
    let node: PathTrie<T> = this;
    if (path !== "") {
      for (const s of path.split("/")) {
        let child = node.children.get(s);
        if (child === undefined) node.children.set(s, (child = new PathTrie<T>()));
        node = child;
      }
    }
    node.value = value;
  }

  /** [value, j] of the nearest proper ancestor of the key with segments `segs`
   * that has a value, at the path `segs.slice(0, j)`; or undefined. */
  nearest(segs: string[]): [T, number] | undefined {
    let node: PathTrie<T> | undefined = this;
    let best: [T, number] | undefined;
    if (this.value !== undefined && !(segs.length === 1 && segs[0] === "")) best = [this.value, 0];
    for (let j = 0; j < segs.length - 1; j++) {
      node = node.children.get(segs[j]);
      if (node === undefined) break;
      if (node.value !== undefined) best = [node.value, j + 1];
    }
    return best;
  }
}

/** Is `path` a proper descendant of one of the paths of `arrays`? */
export function insideArray(path: string, arrays: PathTrie<true>): boolean {
  return arrays.nearest(path.split("/")) !== undefined;
}

/** The implicit groups: proper ancestors of nodes that are not nodes. Each walk
 * stops at a path already seen, so the time is that of the paths it adds. */
export function implicitGroups(nodes: Iterable<string>): Set<string> {
  const all = new Set(nodes);
  const out = new Set<string>();
  for (const path of all) {
    let i = path.length;
    while (i > 0) {
      i = path.lastIndexOf("/", i - 1);
      const p = i > 0 ? path.slice(0, i) : "";
      if (all.has(p) || out.has(p)) break;
      out.add(p);
      i = Math.max(i, 0);
    }
  }
  return out;
}

/** Is `s` a decimal integer without leading zeros below `limit` (spec/virtualize/n5.md §3.1, spec/virtualize/zarr2.md §3)? */
export function canonicalIndex(s: string, limit: number): boolean {
  return /^(0|[1-9][0-9]{0,16})$/.test(s) && Number(s) < limit;
}

/** ceil(shape / chunks), exactly (a binary64 quotient can round down past an integer). */
export function grid(shape: number[], chunks: number[]): number[] {
  return shape.map((s, i) => Number((BigInt(s) + BigInt(chunks[i]) - 1n) / BigInt(chunks[i])));
}

/** The chunk objects (§1.4): each object's nearest ancestor array decides. Sorted by key. */
export function findChunks(objects: Map<string, number>, arrays: Map<string, (rest: string) => boolean>): [string, number][] {
  const trie = new PathTrie<(rest: string) => boolean>();
  for (const [path, test] of arrays) trie.add(path, test);
  const out: [string, number][] = [];
  for (const [key, size] of objects) {
    const segs = key.split("/");
    const found = trie.nearest(segs);
    if (found !== undefined && found[0](segs.slice(found[1]).join("/"))) out.push([key, size]);
  }
  return out.sort((a, b) => compareKeys(a[0], b[0]));
}

export const docKey = (path: string) => join(path, "zarr.json");

/** Declares the profile's convention on the root node of `docs` (conventions §2). */
/** Declares the profile's convention (conventions §2). Each document's `attributes`
 * holds, until then, the attributes copied from the source; they move into the
 * property `vzip_virtualized` (as its member named after the profile), and the
 * node's attributes are the member `ome` that `omes` gives for its path, if
 * any, and the convention's members. The root always declares the convention;
 * any other node only when it has copied attributes. */
export function declareNodes(
  docs: Map<string, Json>,
  profile: Profile,
  url: string,
  omes: Map<string, Json> = new Map(),
): void {
  for (const [key, doc] of docs) {
    const path = key.slice(0, key.length - "zarr.json".length).replace(/\/$/, "");
    const node = doc as { [k: string]: Json };
    const target: { [k: string]: Json } = omes.has(path) ? { ome: omes.get(path)! } : {};
    const attributes = declare(target, profile, path === "" ? url : undefined,
      node.attributes as { [k: string]: Json }) as { [k: string]: Json };
    docs.set(key, { ...node, attributes });
  }
}

export interface StoreResult {
  docs: Map<string, Json>;
  /** (key, size) of the chunk entries, sorted, sizes > 0. */
  chunks: [string, number][];
  /** The source key of each entry whose key is not its object's (the objects under vzip_source/objects/). */
  origins?: Map<string, string>;
}

export const SOURCE_GROUP = "vzip_source";
export const OBJECTS = "vzip_source/objects/";
/** The empty objects' keys under a root array (spec/virtualize/zarr2.md §5). */
export const EMPTY_KEY = "vzip_source/empty.json";
// A last segment that a Zarr reader takes for a node's document, followed by any number of `~`.
const NODE_NAME = /^(?:zarr\.json|\.zarray|\.zgroup)~*$/;

/** The hierarchy's key of the other object `key` (spec/virtualize/zarr2.md §5,
 * spec/virtualize/n5.md §6): `vzip_source/objects/<key>`, with `~` appended to a last
 * segment that is `zarr.json`, `.zarray` or `.zgroup` followed by any number of `~`, so
 * that no Zarr reader opens a node there, and the key maps back one to one. */
export function objectKey(key: string): string {
  return OBJECTS + key + (NODE_NAME.test(key.slice(key.lastIndexOf("/") + 1)) ? "~" : "");
}

/** A store node's source metadata S (spec/virtualize/zarr2.md §4, spec/virtualize/n5.md §5,
 * spec/virtualize/ome-zarr.md §8): `{"attributes": A, "metadata": M, "unversioned": U}`,
 * each member only when it is not empty. */
export function sourceMetadata(
  attributes: { [k: string]: Json },
  metadata: { [k: string]: Json },
  unversioned: string[] = [],
): { [k: string]: Json } {
  const s: { [k: string]: Json } = {};
  if (Object.keys(attributes).length) s.attributes = attributes;
  if (Object.keys(metadata).length) s.metadata = metadata;
  if (unversioned.length) s.unversioned = unversioned;
  return s;
}

/** Adds every nonempty object of the store that is not in `used` (the node documents
 * the hierarchy represents, the nonempty chunk objects, the objects referenced under
 * their own key, and those the convention leaves out), whole, at its `objectKey`, lists
 * the keys of the empty ones (empty chunk objects included) and the recorded `ignored`
 * keys (§1.4), and adds the group `vzip_source` when there is one
 * (spec/virtualize/zarr2.md §5, spec/virtualize/n5.md §6). Returns how many objects
 * are kept. A hierarchy with a node at `vzip_source` or below it then rejects the input
 * (`nodes`: every node path). */
export function keepObjects(
  result: StoreResult,
  objects: Map<string, number>,
  used: Set<string>,
  nodes: Iterable<string>,
  fail: (m: string) => never,
  ignoredKeys: string[] = [],
): number {
  const empty = [...objects].filter(([k, n]) => n === 0 && !used.has(k)).map(([k]) => k).sort(compareKeys);
  const kept = [...objects].filter(([k, n]) => n > 0 && !used.has(k));
  const ignored = [...ignoredKeys].sort(compareKeys);
  if (kept.length === 0 && empty.length === 0 && ignored.length === 0) return 0;
  const own = empty.length || ignored.length
    ? { ...(empty.length ? { empty } : {}), ...(ignored.length ? { ignored } : {}) }
    : undefined;
  const root = result.docs.get(docKey(""))! as { node_type?: string; attributes: Record<string, any> };
  if (root.node_type !== "array") {
    for (const p of nodes) {
      if (p === SOURCE_GROUP || p.startsWith(SOURCE_GROUP + "/")) {
        fail(`the node ${p} is where the store's other objects go (${OBJECTS}...)`);
      }
    }
    // The keys of empty and ignored objects are the source metadata of vzip_source.
    const profile = root.attributes[CONVENTION_KEY].profile as Profile;
    result.docs.set(docKey(SOURCE_GROUP), { zarr_format: 3, node_type: "group", attributes: declare({}, profile, undefined, own) as Json });
  } else if (own) {
    // An array has no children: under a root array, the keys are plain keys of the archive.
    result.docs.set(EMPTY_KEY, own);
  }
  result.origins ??= new Map();
  for (const [k] of kept) result.origins.set(objectKey(k), k);
  result.chunks = [...result.chunks, ...kept.map(([k, n]): [string, number] => [objectKey(k), n])]
    .sort((a, b) => compareKeys(a[0], b[0]));
  return kept.length;
}

/** The archive of a store profile's output (§1.4): one url source per chunk entry. */
export function storeArchive(url: string, { docs, chunks, origins }: StoreResult): ArchiveDesc {
  for (const key of [...docs.keys(), ...chunks.map(([k]) => k)]) {
    if (utf8.encode(key).length > 65535) reject("an output key is longer than 65535 bytes");
    if (key.startsWith("__vz__/")) reject(`output key ${JSON.stringify(key)} is in the reserved __vz__/ space`);
  }
  const entries: EntryDesc[] = chunks.map(([key, size], i) => ({
    key, ranges: [{ source: i, offset: 0n, length: BigInt(size) }],
  }));
  for (const [key, doc] of docs) {
    // vzip_source's documents (the empty objects' keys) are read only when asked for: compressed.
    const compress = key.startsWith(SOURCE_GROUP + "/");
    entries.push({ key, bytes: utf8.encode(stringifyJson(doc)), ...(compress ? { compress } : {}) });
  }
  // Each source pins its object's listed size (§1.4).
  return { sources: chunks.map(([key, size]) => ({ url: objectUrl(url, origins?.get(key) ?? key), size: BigInt(size) })), entries };
}

export const GROUP_IMPLICIT = (): Json => ({ zarr_format: 3, node_type: "group", attributes: {} });

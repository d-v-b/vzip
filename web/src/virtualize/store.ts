// Store inputs (VIRTUALIZE.md §1.4–§1.6): the listing, object reads, the
// strict JSON reader, and the output of a store profile (one url source per
// chunk object). Shared by the N5 (§9), Zarr v2 (§10) and OME-Zarr (§11) profiles.

import { isUriReference } from "../uri.ts";
import { declare, type Profile } from "./common.ts";
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
/** The browser implementation's limit on listed objects (§12). */
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
}

/** The XML subset of §1.5 (the same algorithm as the Python reference). */
class Xml {
  pos = 0;
  readonly s: string;
  constructor(s: string) {
    this.s = s;
  }

  fail(why: string): never {
    throw new StoreError(`listing is not well formed: ${why} at ${this.pos}`);
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
    this.pos++;
    const el: XmlElement = { name: this.name(), children: [], text: [] };
    const s = this.s;
    for (;;) {
      if (this.pos < s.length && WS.has(s[this.pos])) {
        this.ws();
        if (this.pos < s.length && NAME_START.test(s[this.pos])) {
          this.name();
          this.ws();
          if (!this.at("=")) this.fail("expected =");
          this.pos++;
          this.ws();
          const q = this.pos < s.length ? s[this.pos] : "";
          if (q !== "'" && q !== '"') this.fail("expected a quoted value");
          this.pos++;
          for (;;) {
            this.chardata(q);
            if (this.at("&")) this.reference();
            else if (this.at(q)) {
              this.pos++;
              break;
            } else this.fail("bad attribute value");
          }
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
  let depth = 0;
  let deepest = 0;
  for (const m of s.matchAll(/"(?:[^"\\]|\\[\s\S])*"|[[\]{}]/g)) {
    const t = m[0];
    if (t === "[" || t === "{") deepest = Math.max(deepest, ++depth);
    else if (t === "]" || t === "}") depth--;
  }
  if (deepest > MAX_DEPTH) reject(`JSON nests more than ${MAX_DEPTH} deep`);
  let v: Json = null;
  try {
    v = JSON.parse(s);
  } catch (e) {
    reject(`invalid JSON: ${(e as Error).message}`);
  }
  checkNumbers(v);
  return v;
}

/** A value for a message (`JSON.stringify` gives undefined for undefined). */
export function show(v: unknown): string {
  return String(JSON.stringify(v));
}

export function isNumber(v: unknown): v is number {
  return typeof v === "number";
}

/** `v` as an integer of §1.6 within [lo, hi], or undefined. */
export function asInt(v: unknown, lo = -MAX_SAFE, hi = MAX_SAFE): number | undefined {
  if (typeof v !== "number" || !Number.isInteger(v) || Math.abs(v) > MAX_SAFE) return undefined;
  return v >= lo && v <= hi ? v : undefined;
}

export function isObject(v: unknown): v is { [k: string]: Json } {
  return v !== null && typeof v === "object" && !Array.isArray(v);
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

export interface Store {
  url: string;
  /** Relative key → size, of the objects that are not ignored (§1.4). */
  objects: Map<string, number>;
  listed: number;
  requests: number;
  read(key: string): Promise<Uint8Array>;
}

/** Adds listed objects to `store`, rejecting a key listed twice (§1.5). */
export function addListed(store: Store, listed: [string, number][], prefix: string, seen: Set<string>) {
  for (const [key, size] of listed) {
    if (seen.has(key)) reject(`key ${JSON.stringify(key)} is listed twice`);
    seen.add(key);
    const rel = key.slice(prefix.length);
    if (!ignored(rel)) store.objects.set(rel, size);
  }
  store.listed = seen.size;
}

/** The JSON document at `key` (§1.6). */
export async function readDocument(store: Store, key: string): Promise<Json> {
  const size = store.objects.get(key)!;
  if (size > MAX_DOCUMENT) reject(`${key}: JSON document of ${size} bytes exceeds ${MAX_DOCUMENT}`);
  return parseJson(size ? await store.read(key) : new Uint8Array(0));
}

export type Fetch = (url: string, init?: RequestInit) => Promise<Response>;

/** A store listed by S3 ListObjectsV2 (§1.5). Fails (StoreLimitError) past `maxObjects`. */
export async function openHttpStore(
  url: string,
  { fetch: f = (u, i) => fetch(u, i), maxObjects = MAX_OBJECTS }: { fetch?: Fetch; maxObjects?: number } = {},
): Promise<Store> {
  const [endpoint, prefix] = listingEndpoint(url);
  const store: Store = {
    url,
    objects: new Map(),
    listed: 0,
    requests: 0,
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
  };
  const seen = new Set<string>();
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
  }
  return store;
}

/** The store profile that the root keys select (§1.4). */
export function chooseProfile(store: Store): "n5" | "zarr2" {
  if (store.objects.has(".zarray") || store.objects.has(".zgroup")) return "zarr2";
  if (store.objects.has("attributes.json")) return "n5";
  return reject("not an N5 or Zarr v2 store: the root has no .zarray, .zgroup or attributes.json");
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

/** Paths in classification order: by number of segments, then by key (conventions/n5/README.md §2, conventions/zarr2/README.md §2). */
export function byDepth(paths: Iterable<string>): string[] {
  return [...paths].sort((a, b) => depth(a) - depth(b) || compareKeys(a, b));
}

/** Is `path` a proper descendant of one of `arrays`? */
export function insideArray(path: string, arrays: Set<string>): boolean {
  let p = path;
  while (p !== "") {
    p = parentOf(p);
    if (arrays.has(p)) return true;
  }
  return false;
}

/** The implicit groups: proper ancestors of nodes that are not nodes. */
export function implicitGroups(nodes: Iterable<string>): Set<string> {
  const all = new Set(nodes);
  const out = new Set<string>();
  for (const path of all) {
    let p = path;
    while (p !== "") {
      p = parentOf(p);
      if (!all.has(p)) out.add(p);
    }
  }
  return out;
}

/** Is `s` a decimal integer without leading zeros below `limit` (conventions/n5/README.md §3.1, conventions/zarr2/README.md §3)? */
export function canonicalIndex(s: string, limit: number): boolean {
  return /^(0|[1-9][0-9]{0,16})$/.test(s) && Number(s) < limit;
}

/** ceil(shape / chunks), exactly (a binary64 quotient can round down past an integer). */
export function grid(shape: number[], chunks: number[]): number[] {
  return shape.map((s, i) => Number((BigInt(s) + BigInt(chunks[i]) - 1n) / BigInt(chunks[i])));
}

/** The chunk objects (§1.4): each object's nearest ancestor array decides. Sorted by key. */
export function findChunks(objects: Map<string, number>, arrays: Map<string, (rest: string) => boolean>): [string, number][] {
  const out: [string, number][] = [];
  for (const [key, size] of objects) {
    const segs = key.split("/");
    for (let j = segs.length - 1; j >= 0; j--) {
      const test = arrays.get(segs.slice(0, j).join("/"));
      if (test !== undefined) {
        if (test(segs.slice(j).join("/"))) out.push([key, size]);
        break;
      }
    }
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
}

/** The archive of a store profile's output (§1.4): one url source per chunk entry. */
export function storeArchive(url: string, { docs, chunks }: StoreResult): ArchiveDesc {
  for (const key of [...docs.keys(), ...chunks.map(([k]) => k)]) {
    if (utf8.encode(key).length > 65535) reject("an output key is longer than 65535 bytes");
    if (key.startsWith("__vz__/")) reject(`output key ${JSON.stringify(key)} is in the reserved __vz__/ space`);
  }
  const entries: EntryDesc[] = chunks.map(([key, size], i) => ({
    key, ranges: [{ source: i, offset: 0n, length: BigInt(size) }],
  }));
  for (const [key, doc] of docs) entries.push({ key, bytes: utf8.encode(JSON.stringify(doc, null, 2)) });
  return { sources: chunks.map(([key]) => ({ url: objectUrl(url, key) })), entries };
}

export const GROUP_IMPLICIT = (): Json => ({ zarr_format: 3, node_type: "group", attributes: {} });

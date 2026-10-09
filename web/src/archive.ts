// Reading a vzip archive held in memory (SPEC.md §3, §4, §8).

import { crc32c, inflateRaw, InflateLimitError } from "./deflate.ts";
import { checkedFetch } from "#net";
import { type CheckedFetch, HttpResolutionError, type ReadOptions, readHttpRange } from "./http.ts";
import {
  decodeReference,
  decodeTable,
  MalformedError,
  rangeSize,
  type Range,
  type Source,
} from "./protobuf.ts";
import { getScheme, resolveReference } from "./uri.ts";
import { CONCAT_ID, RANGE_ID, SOURCES_KEY } from "./writer.ts";

export type ErrorClass =
  | "archive"
  | "entry"
  | "body"
  | "payload"
  | "resolution"
  | "request";

export class VzipError extends Error {
  errorClass: ErrorClass;
  constructor(errorClass: ErrorClass, message: string) {
    super(`vzip ${errorClass} error: ${message}`);
    this.errorClass = errorClass;
  }
}

export interface Entry {
  key: string;
  method: number;
  csize: number;
  size: number;
  bodyOffset: number;
  reference?: { headerId: number; payload: Uint8Array };
  /** Set if the record has an entry error (spec §4.1, §3.2). */
  error?: string;
}

/**
 * Reads bytes `[start, end)` of an http(s) object (replaceable in tests).
 * `size` is the object's size, if the response gives it; `hidden` says that
 * the response hid the headers that would (a cross-origin browser read).
 */
export type RangeFetcher = (
  url: string,
  start: number,
  end: number,
  pins: Pick<Source, "size" | "etag" | "modifiedNotAfter">,
  options?: ReadOptions,
) => Promise<{ data: Uint8Array; size: number | undefined; hidden?: boolean }>;

/**
 * What the reader may resolve, and its limits (spec §8.7). Every member is
 * optional; `DEFAULT_POLICY` gives the defaults.
 *
 * - `schemes`: schemes allowed besides those rule 1 allows by default
 *   (`http` and `https` for every archive, `file` for a local one), such as
 *   `s3`. It may not name `file`: see `allowFilesFromRemoteArchives`.
 * - `prefixes`: if given, a URL is allowed if and only if it matches one of
 *   them (rule 2), whatever its scheme. Rule 3 still applies.
 * - `allowFilesFromRemoteArchives`: UNSAFE. Lets an archive opened from a
 *   URL name `file:` sources. (This reader reads only http(s) sources, so a
 *   `file:` source it allows still fails as an unsupported scheme.)
 * - `allowPrivateHosts`: UNSAFE. Lets the archive reach loopback, private,
 *   link-local and other special hosts (rule 3), which every archive, local
 *   or remote, is otherwise refused. In a browser this reader sees host
 *   names as written, not the addresses they resolve to, and Private
 *   Network Access covers the rest; under Node it checks the addresses it
 *   connects to (`#net`).
 * - `allowUncheckedProxy`: UNSAFE. Under Node, lets requests go through the
 *   proxy that `NODE_USE_ENV_PROXY` and `HTTP_PROXY` / `HTTPS_PROXY` ask
 *   for while rule 3 applies; the reader then checks only what the name
 *   resolves to locally. Without it, such a request is refused.
 * - `maxSources`, `maxReads`, `maxFormatEntry`: rules 4 to 6.
 * - `unverifiablePins`: skip pins whose headers a cross-origin response hides
 *   (rule 7). Range checksums are still checked.
 */
export interface Policy {
  schemes?: string[];
  prefixes?: string[];
  allowFilesFromRemoteArchives?: boolean;
  allowPrivateHosts?: boolean;
  allowUncheckedProxy?: boolean;
  maxSources?: number;
  maxReads?: number;
  maxFormatEntry?: number;
  unverifiablePins?: boolean;
}

export const DEFAULT_POLICY: Required<Omit<Policy, "prefixes">> & Pick<Policy, "prefixes"> = {
  schemes: [],
  allowFilesFromRemoteArchives: false,
  allowPrivateHosts: false,
  allowUncheckedProxy: false,
  maxSources: 1 << 22,
  maxReads: 1024,
  maxFormatEntry: 256 << 20,
  unverifiablePins: false,
};

/** The scheme of an absolute URL, lowercase, with https counted as http (§8.7 rule 1). */
function schemeOf(url: string): string {
  const i = url.indexOf(":");
  const s = i < 0 ? "" : url.slice(0, i).toLowerCase();
  return s === "https" ? "http" : s;
}

/** Does `url` match `prefix` (spec §8.7 rule 2)? */
export function matchesPrefix(url: string, prefix: string): boolean {
  if (!url.startsWith(prefix)) return false;
  const rest = url.slice(prefix.length);
  if (!(prefix.endsWith("/") || rest === "" || "/?#".includes(rest[0]))) return false;
  const path = url.split(/[?#]/, 1)[0];
  return !/%(2[eEfF]|5[cC])/.test(path);
}

/** The class of an IP address (spec §8.7 rule 3). */
export type AddressClass = "loopback" | "private" | "link-local" | "special" | "public";

function parseIPv4(s: string): bigint | undefined {
  const m = s.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
  if (!m) return undefined;
  const parts = m.slice(1).map(Number);
  if (parts.some((p) => p > 255)) return undefined;
  return parts.reduce((a, p) => (a << 8n) | BigInt(p), 0n);
}

function parseIPv6(s: string): bigint | undefined {
  let text = s.split("%", 1)[0];
  const tail: number[] = [];
  const dotted = text.match(/^(.*:)(\d+\.\d+\.\d+\.\d+)$/);
  if (dotted) {
    const v4 = parseIPv4(dotted[2]);
    if (v4 === undefined) return undefined;
    tail.push(Number(v4 >> 16n), Number(v4 & 0xffffn));
    text = dotted[1].endsWith("::") ? dotted[1] : dotted[1].slice(0, -1);
  }
  const halves = text.split("::");
  if (halves.length > 2) return undefined;
  const groups = (h: string) => (h === "" ? [] : h.split(":"));
  const head = groups(halves[0]);
  const rest = halves.length === 2 ? groups(halves[1]) : [];
  if (![...head, ...rest].every((g) => /^[0-9a-fA-F]{1,4}$/.test(g))) return undefined;
  const n = head.length + rest.length + tail.length;
  if (halves.length === 1 ? n !== 8 : n > 7) return undefined;
  const words = [
    ...head.map((g) => parseInt(g, 16)),
    ...Array(8 - n).fill(0),
    ...rest.map((g) => parseInt(g, 16)),
    ...tail,
  ];
  return words.reduce((a, w) => (a << 16n) | BigInt(w), 0n);
}

function nets(version: 4 | 6, list: string[]): [bigint, number][] {
  return list.map((n) => {
    const [addr, len] = n.split("/");
    return [(version === 4 ? parseIPv4(addr) : parseIPv6(addr))!, Number(len)];
  });
}

const CLASSES: [AddressClass, [bigint, number][], [bigint, number][]][] = [
  ["loopback", nets(4, ["127.0.0.0/8"]), nets(6, ["::1/128"])],
  ["private", nets(4, ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]), nets(6, ["fc00::/7"])],
  ["link-local", nets(4, ["169.254.0.0/16"]), nets(6, ["fe80::/10"])],
  [
    "special",
    nets(4, [
      "0.0.0.0/8", "100.64.0.0/10", "192.0.0.0/24", "192.0.2.0/24", "198.18.0.0/15",
      "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/3",
    ]),
    nets(6, [
      "::/8", "64:ff9b:1::/48", "100::/63", "2001::/23", "2001:db8::/32", "3fff::/20",
      "5f00::/16", "fec0::/10", "ff00::/8",
    ]),
  ],
];

const within = (a: bigint, bits: number, [net, len]: [bigint, number]) =>
  a >> BigInt(bits - len) === net >> BigInt(bits - len);

function classOf(a: bigint, version: 4 | 6): AddressClass {
  if (version === 6) {
    // IPv4 embedded in IPv6 (mapped, NAT64, 6to4) has the IPv4 address's class.
    if (a >> 32n === 0xffffn || a >> 32n === 0x64ff9b0000000000000000n) return classOf(a & 0xffffffffn, 4);
    if (a >> 112n === 0x2002n) return classOf((a >> 80n) & 0xffffffffn, 4);
  }
  for (const [c, v4, v6] of CLASSES) {
    if ((version === 4 ? v4 : v6).some((n) => within(a, version === 4 ? 32 : 128, n))) return c;
  }
  return "public";
}

/** The class of an IP address in text form (spec §8.7 rule 3), or undefined if it is not one. */
export function addressClass(address: string): AddressClass | undefined {
  const v4 = parseIPv4(address);
  if (v4 !== undefined) return classOf(v4, 4);
  const v6 = address.includes(":") ? parseIPv6(address) : undefined;
  return v6 === undefined ? undefined : classOf(v6, 6);
}

/**
 * The class of a URL's host as written (spec §8.7 rule 3): an IP literal's
 * class (the URL parser normalizes decimal, octal and hexadecimal IPv4
 * forms), loopback for `localhost` and names under `.localhost`, and
 * undefined for any other name.
 */
function hostClass(url: string): AddressClass | undefined {
  let host: string;
  try {
    host = new URL(url).hostname.toLowerCase().replace(/\.$/, "");
  } catch {
    return undefined;
  }
  if (host === "localhost" || host.endsWith(".localhost")) return "loopback";
  if (host.startsWith("[") && host.endsWith("]")) host = host.slice(1, -1);
  return addressClass(host);
}

/** Throws unless `policy` is well formed: `schemes` may not name `file` (§8.7 rule 1). */
export function validatePolicy(policy: Policy): void {
  if ((policy.schemes ?? []).some((s) => s.toLowerCase() === "file")) {
    throw new TypeError(
      "Policy.schemes may not name 'file': a local archive reads files already, and a remote " +
        "one needs the unsafe allowFilesFromRemoteArchives: true",
    );
  }
}

/** Throws a resolution error unless `policy` allows `url` for an archive at `base` (§8.7). */
export function checkPolicy(policy: Policy, base: string, url: string): void {
  validatePolicy(policy);
  const local = schemeOf(base) === "file";
  const scheme = schemeOf(url);
  if (policy.prefixes !== undefined) {
    if (!policy.prefixes.some((p) => matchesPrefix(url, p))) {
      throw new VzipError("resolution", `${url}: not under an allowed URL prefix (spec §8.7)`);
    }
  } else if (scheme === "file") {
    if (!local && !policy.allowFilesFromRemoteArchives) {
      throw new VzipError("resolution", `${url}: an archive opened from a URL may not read local files (spec §8.7)`);
    }
  } else if (scheme !== "http" && !(policy.schemes ?? []).some((s) => schemeOf(`${s}:`) === scheme)) {
    throw new VzipError("resolution", `${url}: the scheme '${scheme}' is not allowed (spec §8.7)`);
  }
  if (scheme !== "http") return;
  // Rule 3, on the host as written; checkAddress applies it to resolved addresses.
  const c = hostClass(url);
  if (c !== undefined) checkClass(policy, url, c);
}

function checkClass(policy: Policy, url: string, c: AddressClass, address?: string): void {
  if (c === "public" || policy.allowPrivateHosts) return;
  const at = address === undefined ? "" : ` (at ${address})`;
  throw new VzipError(
    "resolution",
    `${url}: the host${at} is a ${c} address, which the reader does not reach without allowPrivateHosts: true (spec §8.7)`,
  );
}

/** Throws a resolution error unless rule 3 of §8.7 lets the reader connect to `address` for `url`. */
export function checkAddress(policy: Policy, url: string, address: string): void {
  const c = addressClass(address);
  if (c === undefined) throw new VzipError("resolution", `${url}: ${address} is not an IP address`);
  checkClass(policy, url, c, address);
}

/**
 * Reads of the same url source at most this far apart are combined into one
 * request (spec §6.2 allows it). A Concat of many short ranges, such as one
 * per image row, would otherwise cost one request per range.
 */
export const MERGE_GAP = 1 << 16;

const U16_ALL = 0xffff;
const U32_ALL = 0xffffffff;
const strictUtf8 = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });

function safe(v: bigint, what: string): number {
  if (v > BigInt(Number.MAX_SAFE_INTEGER)) {
    throw new VzipError("archive", `${what} is too large`);
  }
  return Number(v);
}

function parseExtra(extra: Uint8Array, view: DataView, base: number) {
  const blocks: { id: number; data: Uint8Array }[] = [];
  let at = 0;
  while (at < extra.length) {
    if (at + 4 > extra.length) return undefined;
    const id = view.getUint16(base + at, true);
    const n = view.getUint16(base + at + 2, true);
    if (at + 4 + n > extra.length) return undefined;
    blocks.push({ id, data: extra.subarray(at + 4, at + 4 + n) });
    at += 4 + n;
  }
  return blocks;
}

export class Archive {
  bytes: Uint8Array;
  baseUrl: string;
  sources: Source[];
  entries: Map<string, Entry>;
  /** The spec revision the writer followed, if the archive records one (spec §1.3). */
  revision: number | undefined;
  policy: typeof DEFAULT_POLICY;
  private fetchRange: RangeFetcher;
  /** Sends this archive's requests, checking addresses (rule 3), where the platform allows. */
  private netFetch: CheckedFetch | undefined;

  private constructor(
    bytes: Uint8Array,
    baseUrl: string,
    sources: Source[],
    entries: Map<string, Entry>,
    fetchRange: RangeFetcher,
    policy: typeof DEFAULT_POLICY,
    revision: number | undefined,
  ) {
    this.bytes = bytes;
    this.baseUrl = baseUrl;
    this.sources = sources;
    this.entries = entries;
    this.fetchRange = fetchRange;
    this.policy = policy;
    this.revision = revision;
    this.netFetch = policy.allowPrivateHosts
      ? undefined
      : checkedFetch((url, address) => checkAddress(policy, url, address), policy);
  }

  /**
   * Opens an archive (spec §8.1); `baseUrl` resolves relative `url` sources.
   * `policy` (spec §8.7) defaults to `DEFAULT_POLICY`, which allows http(s)
   * sources on public hosts, and for a local archive also `file` sources and
   * loopback and private hosts.
   */
  static async open(
    bytes: Uint8Array,
    baseUrl: string,
    fetchRange: RangeFetcher = (url, start, end, pins, options) =>
      readHttpRange(url, start, end, pins, undefined, options),
    policy: Policy = {},
  ): Promise<Archive> {
    validatePolicy(policy);
    const rules = { ...DEFAULT_POLICY, ...policy };
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const n = bytes.length;
    let eocd = -1;
    for (const commentLength of [38, 22]) {
      const at = n - 22 - commentLength;
      if (
        at >= 0 &&
        view.getUint32(at, true) === 0x06054b50 &&
        view.getUint16(at + 20, true) === commentLength
      ) {
        eocd = at;
        break;
      }
    }
    if (eocd < 0) throw new VzipError("archive", "not a vzip archive");
    const comment = bytes.subarray(eocd + 22);
    const magic = new TextDecoder().decode(comment.subarray(0, 6));
    if (!magic.startsWith("vzip/")) {
      throw new VzipError("archive", "not a vzip archive");
    }
    if (magic !== "vzip/0") {
      throw new VzipError("archive", `unsupported version ${magic}`);
    }
    let count = BigInt(view.getUint16(eocd + 10, true));
    let cdSize = BigInt(view.getUint32(eocd + 12, true));
    let cdOffset = BigInt(view.getUint32(eocd + 16, true));
    if (
      view.getUint16(eocd + 8, true) === U16_ALL ||
      count === BigInt(U16_ALL) ||
      cdSize === BigInt(U32_ALL) ||
      cdOffset === BigInt(U32_ALL)
    ) {
      const loc = eocd - 20;
      if (loc < 0 || view.getUint32(loc, true) !== 0x07064b50) {
        throw new VzipError("archive", "missing zip64 locator");
      }
      const at = safe(view.getBigUint64(loc + 8, true), "zip64 record offset");
      if (
        at + 56 > n ||
        view.getUint32(at, true) !== 0x06064b50 ||
        view.getBigUint64(at + 4, true) !== 44n
      ) {
        throw new VzipError("archive", "invalid zip64 end of central directory");
      }
      count = view.getBigUint64(at + 32, true);
      cdSize = view.getBigUint64(at + 40, true);
      cdOffset = view.getBigUint64(at + 48, true);
    }
    void count; // spec §3.2: entry counts are not used beyond this point
    const cdStart = safe(cdOffset, "central directory offset");
    const cdEnd = cdStart + safe(cdSize, "central directory size");
    if (cdEnd > n) throw new VzipError("archive", "central directory outside the file");

    const entries = new Map<string, Entry>();
    let at = cdStart;
    while (at < cdEnd) {
      if (at + 46 > cdEnd || view.getUint32(at, true) !== 0x02014b50) {
        throw new VzipError("archive", `bad central directory record at ${at}`);
      }
      const flags = view.getUint16(at + 8, true);
      const method = view.getUint16(at + 10, true);
      const csize = view.getUint32(at + 20, true);
      const size = view.getUint32(at + 24, true);
      const nameLen = view.getUint16(at + 28, true);
      const extraLen = view.getUint16(at + 30, true);
      const commentLen = view.getUint16(at + 32, true);
      let offset: number | bigint = view.getUint32(at + 42, true);
      const end = at + 46 + nameLen + extraLen + commentLen;
      if (end > cdEnd) throw new VzipError("archive", "truncated central directory");
      const nameBytes = bytes.subarray(at + 46, at + 46 + nameLen);
      const extraStart = at + 46 + nameLen;
      const extra = bytes.subarray(extraStart, extraStart + extraLen);
      at = end;
      let key: string;
      try {
        key = strictUtf8.decode(nameBytes);
      } catch {
        continue; // names no key (spec §3.3)
      }
      if (key === "") continue;
      if (entries.has(key)) throw new VzipError("archive", `duplicate key ${key}`);
      const entry: Entry = { key, method, csize, size, bodyOffset: 0 };
      entries.set(key, entry);
      const blocks = parseExtra(extra, view, extraStart);
      const refs = blocks?.filter((b) => b.id === RANGE_ID || b.id === CONCAT_ID) ?? [];
      const zip64 = blocks?.filter((b) => b.id === 0x0001) ?? [];
      if (blocks === undefined) entry.error = "unparseable extra field";
      else if (refs.length > 1) entry.error = "more than one reference block";
      else if (zip64.length > 1) entry.error = "more than one zip64 block";
      else if (csize === U32_ALL || size === U32_ALL) entry.error = "size field is 0xFFFFFFFF";
      else if (offset === U32_ALL) {
        if (zip64.length === 0 || zip64[0].data.length < 8) {
          entry.error = "offset needs a zip64 block";
        } else {
          offset = new DataView(
            zip64[0].data.buffer,
            zip64[0].data.byteOffset,
          ).getBigUint64(0, true);
        }
      }
      if (entry.error === undefined && method !== 0 && method !== 8) {
        entry.error = `compression method ${method}`;
      } else if (entry.error === undefined && flags & 1) {
        entry.error = "encrypted";
      }
      if (entry.error === undefined && refs.length === 1) {
        if (method !== 0) entry.error = "reference entry with method 8";
        entry.reference = { headerId: refs[0].id, payload: refs[0].data };
      }
      entry.bodyOffset =
        typeof offset === "bigint"
          ? Number(offset) + 30 + nameLen
          : offset + 30 + nameLen;
    }

    const cview = new DataView(comment.buffer, comment.byteOffset, comment.byteLength);
    const sOffset = safe(cview.getBigUint64(6, true), "sources offset");
    const sSize = safe(cview.getBigUint64(14, true), "sources size");
    if (sOffset + sSize > n) throw new VzipError("archive", "source table outside the file");
    let table: { sources: Source[]; revision?: number };
    try {
      table = decodeTable(
        await inflateRaw(bytes.subarray(sOffset, sOffset + sSize), rules.maxFormatEntry),
      );
    } catch (e) {
      const why = e instanceof InflateLimitError
        ? `${e.message}; this reader allows ${rules.maxFormatEntry} (spec §8.7)`
        : (e as Error).message;
      throw new VzipError("archive", `source table: ${why}`);
    }
    if (table.sources.length > rules.maxSources) {
      throw new VzipError(
        "archive",
        `the source table has ${table.sources.length} sources; this reader allows ${rules.maxSources}`,
      );
    }
    return new Archive(bytes, baseUrl, table.sources, entries, fetchRange, rules, table.revision);
  }

  /** The visible entry for `key`, or undefined (spec §8.2). */
  lookup(key: string): Entry | undefined {
    if (key.startsWith("__vz__/")) return undefined;
    const entry = this.entries.get(key);
    if (entry?.error !== undefined) {
      throw new VzipError("entry", `${JSON.stringify(key)}: ${entry.error}`);
    }
    return entry;
  }

  /** Visible keys, in UTF-8 order. */
  keys(): string[] {
    return [...this.entries.keys()]
      .filter((k) => !k.startsWith("__vz__/"))
      .sort((a, b) => compareUtf8(a, b));
  }

  ranges(entry: Entry): Range[] {
    try {
      return decodeReference(
        entry.reference!.headerId,
        entry.reference!.payload,
        this.sources.length,
      );
    } catch (e) {
      if (!(e instanceof MalformedError)) throw e;
      throw new VzipError("payload", `${JSON.stringify(entry.key)}: ${e.message}`);
    }
  }

  /** Size of an entry's value. */
  size(entry: Entry): number {
    if (entry.reference === undefined) return entry.size;
    const total = this.ranges(entry).reduce((n, r) => n + rangeSize(r), 0n);
    if (total > BigInt(Number.MAX_SAFE_INTEGER)) {
      throw new VzipError("request", "value is too large for this reader");
    }
    return Number(total);
  }

  private async body(entry: Entry): Promise<Uint8Array> {
    const end = entry.bodyOffset + entry.csize;
    if (end > this.bytes.length) {
      throw new VzipError("body", `${entry.key}: body outside the file`);
    }
    const stored = this.bytes.subarray(entry.bodyOffset, end);
    let body = stored;
    if (entry.method === 8) {
      try {
        body = await inflateRaw(stored, entry.size); // spec §8.7 rule 6
      } catch (e) {
        throw new VzipError("body", `${entry.key}: ${(e as Error).message}`);
      }
    } else if (entry.csize !== entry.size) {
      throw new VzipError("body", `${entry.key}: STORED sizes differ`);
    }
    if (body.length !== entry.size) {
      throw new VzipError("body", `${entry.key}: inflates to ${body.length} bytes`);
    }
    return body;
  }

  private async source(r: Extract<Range, { source: number }>): Promise<Uint8Array> {
    const s = this.sources[r.source];
    const start = r.offset;
    const end = r.offset + r.length;
    const fail = (m: string): never => {
      throw new VzipError("resolution", `source ${r.source}: ${m}`);
    };
    if (s.data !== undefined) {
      if (end > BigInt(s.data.length)) fail("range past the end of the data");
      return s.data.subarray(Number(start), Number(end));
    }
    if (s.key !== undefined) {
      const target = this.entries.get(s.key);
      if (target === undefined || target.reference !== undefined || target.error) {
        fail(`key ${JSON.stringify(s.key)} is not a bytes entry`);
      }
      const value = await this.body(target!);
      if (end > BigInt(value.length)) fail("range past the end of the key's value");
      return value.subarray(Number(start), Number(end));
    }
    let url: string;
    try {
      url = resolveReference(this.baseUrl, s.url!);
    } catch (e) {
      return fail((e as Error).message);
    }
    checkPolicy(this.policy, this.baseUrl, url); // spec §8.7
    const scheme = getScheme(url);
    if (scheme !== "http" && scheme !== "https") fail(`unsupported scheme in ${url}`);
    try {
      const { data, size, hidden } = await this.fetchRange(
        url,
        safe(start, "offset"),
        safe(end, "end"),
        s,
        {
          allow: (u) => checkPolicy(this.policy, this.baseUrl, u),
          unverifiablePins: this.policy.unverifiablePins,
          ...(this.netFetch ? { fetch: this.netFetch } : {}),
        },
      );
      const unseen = size === undefined && hidden === true && this.policy.unverifiablePins;
      if (s.size !== undefined && !unseen && BigInt(size ?? -1) !== s.size) {
        fail(
          size === undefined
            ? `${url}: the archive pins the size ${s.size}, which the response does not give`
            : `${url}: the source is ${size} bytes, but the archive pins ${s.size}: the source ` +
                "changed since the archive was written",
        );
      }
      return data;
    } catch (e) {
      if (e instanceof HttpResolutionError) fail(e.message);
      throw e;
    }
  }

  /** Bytes `[start, end)` of a visible key's value, or undefined if absent. */
  async read(key: string, start = 0, end?: number): Promise<Uint8Array | undefined> {
    const entry = this.lookup(key);
    if (entry === undefined) return undefined;
    const size = this.size(entry);
    end ??= size;
    if (start < 0 || end < start || end > size) {
      throw new VzipError("request", `range [${start}, ${end}) of a ${size}-byte value`);
    }
    if (entry.reference === undefined) {
      return (await this.body(entry)).subarray(start, end);
    }
    type Read = { source: number; offset: bigint; length: bigint };
    // Each part is the bytes of a range that overlaps the request. A range with
    // a crc32c is read whole and checked (spec §8.3); `cuts` says which part of
    // each part's bytes the request takes, and which checksum they must have.
    const parts: (Promise<Uint8Array> | Read)[] = [];
    const cuts: { lo: number; hi: number; crc?: number }[] = [];
    let at = 0;
    for (const r of this.ranges(entry)) {
      const n = Number(rangeSize(r));
      const a = Math.max(start, at);
      const b = Math.min(end, at + n);
      // Only ranges that overlap the request are resolved.
      if (a < b) {
        if ("data" in r) {
          parts.push(Promise.resolve(r.data.subarray(a - at, b - at)));
          cuts.push({ lo: 0, hi: b - a });
        } else {
          let read: Read;
          if (r.crc32c === undefined) {
            read = { source: r.source, offset: r.offset + BigInt(a - at), length: BigInt(b - a) };
            cuts.push({ lo: 0, hi: b - a });
          } else {
            read = { source: r.source, offset: r.offset, length: r.length };
            cuts.push({ lo: a - at, hi: b - at, crc: r.crc32c });
          }
          parts.push(this.sources[r.source].url !== undefined ? read : this.source(read));
        }
      }
      at += n;
    }
    // Combine nearby reads of each url source into runs, fetch each run once,
    // and slice the reads back out of it.
    const reads = parts.filter((p): p is Read => !(p instanceof Promise));
    const sorted = [...reads].sort((x, y) =>
      x.source - y.source || (x.offset < y.offset ? -1 : x.offset > y.offset ? 1 : 0));
    const runs: { source: number; offset: bigint; end: bigint; data?: Promise<Uint8Array> }[] = [];
    const runOf = new Map<Read, (typeof runs)[number]>();
    for (const read of sorted) {
      const last = runs[runs.length - 1];
      const readEnd = read.offset + read.length;
      if (last && last.source === read.source && read.offset - last.end <= BigInt(MERGE_GAP)) {
        if (readEnd > last.end) last.end = readEnd;
      } else {
        runs.push({ source: read.source, offset: read.offset, end: readEnd });
      }
      runOf.set(read, runs[runs.length - 1]);
    }
    if (runs.length > this.policy.maxReads) {
      // spec §8.7 rule 5: before any read. The other parts read no url source.
      for (const p of parts) if (p instanceof Promise) p.catch(() => {});
      throw new VzipError(
        "request",
        `the value needs ${runs.length} reads; this reader allows ${this.policy.maxReads} (spec §8.7)`,
      );
    }
    for (const run of runs) {
      run.data = this.source({ source: run.source, offset: run.offset, length: run.end - run.offset });
    }
    const whole = await Promise.all(
      parts.map(async (p) => {
        if (p instanceof Promise) return p;
        const run = runOf.get(p)!;
        const from = Number(p.offset - run.offset);
        return (await run.data!).subarray(from, from + Number(p.length));
      }),
    );
    const chunks = whole.map((data, i) => {
      const { lo, hi, crc } = cuts[i];
      if (crc !== undefined && crc32c(data) !== crc) {
        throw new VzipError(
          "resolution",
          `the bytes read do not match the range's crc32c ${crc} (got ${crc32c(data)}): the ` +
            "source changed since the archive was written",
        );
      }
      return data.subarray(lo, hi);
    });
    const out = new Uint8Array(end - start);
    let o = 0;
    for (const c of chunks) {
      out.set(c, o);
      o += c.length;
    }
    return out;
  }
}

export function compareUtf8(a: string, b: string): number {
  // Code point order equals UTF-8 byte order.
  const x = [...a];
  const y = [...b];
  for (let i = 0; i < Math.min(x.length, y.length); i++) {
    const d = x[i].codePointAt(0)! - y[i].codePointAt(0)!;
    if (d !== 0) return d;
  }
  return x.length - y.length;
}

export { SOURCES_KEY };

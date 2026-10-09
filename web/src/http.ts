/**
 * @file Reading a byte range of an http(s) `url` source (spec §6.2), with
 * its pins (§6.1).
 *
 * Browser limitations, which a JavaScript reader cannot work around:
 *
 * - `Accept-Encoding` is a forbidden request header. Browsers send
 *   `Accept-Encoding: identity` on their own for requests with a `Range`
 *   header, and the response's `Content-Encoding` is still checked here.
 * - `fetch` follows redirects itself (up to 20), re-sending the request
 *   headers, so the 5-redirect limit of §6.2 cannot be enforced. A redirect
 *   to a non-http(s) URL is refused by the browser.
 * - Cross-origin, `ETag`, `Last-Modified` and `Content-Range` are visible
 *   only if the server lists them in `Access-Control-Expose-Headers`, and
 *   `If-Match` / `If-Unmodified-Since` need a CORS preflight. Without them,
 *   pins cannot be checked, which is a resolution error (pins fail closed).
 *   A cross-origin 206 without a visible `Content-Range` is accepted if its
 *   body has exactly the requested length (a deviation from §6.2). An
 *   application may allow the reader to skip such pins (spec §8.7 rule 7,
 *   `unverifiablePins`); range checksums (§5.2) still apply.
 * - A redirect cannot be seen before `fetch` follows it, so the reader's
 *   policy (§8.7) is checked on the final URL (`allow`).
 * - `fetch` does not expose the address it connects to, so rule 3 of §8.7
 *   (private and special hosts) is checked on hosts as written: IP literals
 *   and `localhost`. A browser's Private Network Access protections cover
 *   names that resolve to such addresses, and redirect hops in between.
 *   Under Node, `options.fetch` (from `#net`) checks the connected addresses.
 */

import type { Source } from "./protobuf.ts";
import {
  checkHttpUrl,
  formatImfFixdate,
  parseImfFixdate,
} from "./uri.ts";

export class HttpResolutionError extends Error {}

/**
 * A `fetch` that applies rule 3 of spec §8.7 to the addresses it connects to
 * (`#net` under Node), following redirects itself and calling `allow` on each
 * target before requesting it.
 */
export type CheckedFetch = (
  url: string,
  init: { headers: Record<string, string>; signal?: AbortSignal },
  allow?: (url: string) => void,
) => Promise<Response>;

/** What the reader's policy (spec §8.7) asks of one range read. */
export interface ReadOptions {
  /** Sends the requests, in place of the platform's `fetch`. */
  fetch?: CheckedFetch;
  /** Throws for a URL the policy refuses: here, the target of a redirect. */
  allow?: (url: string) => void;
  /** Skip pins whose headers the response hides (spec §8.7 rule 7). */
  unverifiablePins?: boolean;
}

function single(headers: Headers, name: string): string | null {
  // `Headers` joins repeated fields with ", ". Values that cannot contain a
  // comma (`Content-Range`, an `ETag`, an IMF-fixdate's day/month part) are
  // checked for that, so repeated fields are detected (spec §6.2).
  return headers.get(name);
}

/**
 * Bytes `[start, end)` of the object at `url`, and its total size if known.
 */
export async function readHttpRange(
  url: string,
  start: number,
  end: number,
  pins: Pick<Source, "size" | "etag" | "modifiedNotAfter">,
  signal?: AbortSignal,
  options: ReadOptions = {},
): Promise<{ data: Uint8Array<ArrayBuffer>; size: number | undefined; hidden?: boolean; etag?: string }> {
  try {
    checkHttpUrl(url);
  } catch (e) {
    throw new HttpResolutionError((e as Error).message);
  }
  const headers: Record<string, string> = {
    Range: `bytes=${start}-${end - 1}`,
    // Ignored by browsers (forbidden header); honoured elsewhere.
    "Accept-Encoding": "identity",
  };
  if (pins.etag !== undefined) headers["If-Match"] = pins.etag;
  if (pins.modifiedNotAfter !== undefined) {
    try {
      headers["If-Unmodified-Since"] = formatImfFixdate(pins.modifiedNotAfter);
    } catch (e) {
      throw new HttpResolutionError((e as Error).message);
    }
  }
  let response: Response;
  try {
    response = options.fetch
      ? await options.fetch(url, { headers, signal }, options.allow)
      : await fetch(url, { headers, signal, redirect: "follow" });
  } catch (e) {
    signal?.throwIfAborted();
    throw new HttpResolutionError(`${url}: ${(e as Error).message}`);
  }
  const fail = (message: string): never => {
    throw new HttpResolutionError(`${url}: ${message}`);
  };
  if (response.redirected) {
    // The final URL must still satisfy the URL rules, and the policy (§8.7).
    try {
      checkHttpUrl(response.url);
      options.allow?.(response.url);
    } catch (e) {
      await response.body?.cancel();
      fail((e as Error).message);
    }
  }
  if (response.status === 412) fail("a pin failed (412)");
  if (response.status === 416)
    fail("the object is shorter than the range (416)");
  if (response.status !== 200 && response.status !== 206) {
    fail(`HTTP ${response.status}`);
  }
  const encoding = response.headers.get("Content-Encoding");
  if (encoding !== null) {
    const values = encoding.split(",").map((v) => v.trim().toLowerCase());
    if (values.length !== 1 || values[0] !== "identity") {
      fail(`response has Content-Encoding ${JSON.stringify(encoding)}`);
    }
  }
  const etag = single(response.headers, "ETag");
  if (etag !== null && etag.includes(",")) fail("more than one ETag field");
  const lastModified = single(response.headers, "Last-Modified");
  if (lastModified !== null && lastModified.split(",").length > 2) {
    fail("more than one Last-Modified field");
  }
  // Pins are checked against the response itself: servers may ignore the
  // conditional headers (spec §6.2). A cross-origin response hides headers the
  // server does not expose; the application may allow skipping those pins.
  const skip = (header: string | null) =>
    header === null && response.type === "cors" && options.unverifiablePins === true;
  if (pins.etag !== undefined && etag !== pins.etag && !skip(etag)) {
    fail(
      `the ETag is ${JSON.stringify(etag)}, but the archive pins ${pins.etag}: the source ` +
        "changed since the archive was written, or its server does not send the ETag",
    );
  }
  if (pins.modifiedNotAfter !== undefined && !skip(lastModified)) {
    const t = parseImfFixdate(lastModified);
    if (t === undefined) {
      fail(
        `Last-Modified ${JSON.stringify(lastModified)} is not an IMF-fixdate`,
      );
    }
    if (BigInt(t!) > pins.modifiedNotAfter)
      fail("Last-Modified is after the pin");
  }
  const body = new Uint8Array(await response.arrayBuffer());
  if (response.status === 200) {
    // The server ignored `Range` and sent the whole object.
    if (body.length < end) fail(`object is shorter than ${end} bytes`);
    return { data: body.slice(start, end), size: body.length, ...(etag !== null ? { etag } : {}) };
  }
  const contentRange = single(response.headers, "Content-Range");
  if (contentRange === null && response.type === "cors") {
    // Deviation from spec §6.2: a cross-origin response hides
    // `Content-Range` unless the server exposes it, and many servers that
    // allow range requests do not. Only the body length can be checked, and
    // the object's size is unknown, so a `size` pin fails (spec §6.1).
    if (body.length !== end - start) {
      fail(`206 of ${body.length} bytes for [${start}, ${end})`);
    }
    return { data: body, size: undefined, hidden: true, ...(etag !== null ? { etag } : {}) };
  }
  if (contentRange === null) fail("206 without Content-Range");
  const m = contentRange!.trim().match(/^bytes (\d+)-(\d+)\/(\d+|\*)$/i);
  if (m === null) fail(`invalid Content-Range ${JSON.stringify(contentRange)}`);
  const a = Number(m![1]);
  const z = Number(m![2]);
  const total = m![3] === "*" ? undefined : Number(m![3]);
  if (
    a !== start ||
    z !== end - 1 ||
    body.length !== z - a + 1 ||
    (total !== undefined && z >= total)
  ) {
    fail(
      `server returned ${JSON.stringify(contentRange)} (${body.length} bytes) for [${start}, ${end})`,
    );
  }
  return { data: body, size: total, ...(etag !== null ? { etag } : {}) };
}

/**
 * Opens an http(s) object for range reads. Its size comes from a HEAD
 * request (`Content-Length` is visible cross-origin without being exposed),
 * or, if the server refuses HEAD, from the `Content-Range` of a one-byte
 * range request (visible cross-origin only if the server exposes it).
 */
export async function openHttpFile(
  url: string,
  signal?: AbortSignal,
): Promise<{
  size: number;
  read: (offset: number, length: number) => Promise<Uint8Array>;
  /** The object's strong ETag, if every response so far gave the same one (an `etag` pin). */
  etag?: () => string | undefined;
}> {
  const etags = new EtagLog();
  // The size, from the first of these that gives it: a HEAD request; the total
  // in a range response's Content-Range; or the Content-Length of a plain GET,
  // cancelled once its headers arrive. Each can fail in a browser: a URL
  // presigned for GET (as figshare's S3 redirects are) refuses HEAD, and a
  // cross-origin server may not expose Content-Range, while Content-Length is
  // always readable.
  const lengthOf = (r: Response) => {
    const v = r.headers.get("Content-Length");
    // Only a 200 describes the object: a 202 (such as a bot challenge) or
    // other success status says nothing about its size.
    return r.status === 200 && v !== null && /^\d+$/.test(v) ? Number(v) : undefined;
  };
  let size: number | undefined;
  let headStatus = "failed";
  try {
    const head = await fetch(url, { method: "HEAD", signal });
    headStatus = String(head.status);
    size = lengthOf(head);
    if (size !== undefined) etags.saw(head.headers.get("ETag"));
  } catch (e) {
    if (signal?.aborted) throw e;
  }
  if (size === undefined) {
    const probe = await fetch(url, { headers: { Range: "bytes=0-0" }, signal });
    const total = probe.headers.get("Content-Range")?.match(/\/(\d+)$/)?.[1];
    await probe.body?.cancel();
    if (probe.status === 206 && total !== undefined) size = Number(total);
  }
  if (size === undefined) {
    const abort = new AbortController();
    signal?.addEventListener("abort", () => abort.abort(), { once: true });
    const full = await fetch(url, { signal: abort.signal });
    if (full.status === 200) size = lengthOf(full);
    abort.abort(); // only the headers were needed
  }
  if (size === undefined) {
    throw new HttpResolutionError(
      `${url}: cannot determine the size (HEAD ${headStatus})`,
    );
  }
  return {
    size,
    read: async (offset, n) => {
      if (n === 0) return new Uint8Array();
      const r = await readHttpRange(url, offset, offset + n, {}, signal);
      etags.saw(r.etag ?? null);
      return r.data;
    },
    etag: () => etags.pin(),
  };
}

const STRONG_ETAG = /^"[\x21\x23-\x7e]*"$/;

/** The ETag of every response for one object, for its `etag` pin (VIRTUALIZE.md §1.2). */
export class EtagLog {
  private seen = new Set<string | null>();

  saw(etag: string | null): void {
    this.seen.add(etag);
    const strong = [...this.seen].filter((e) => e !== null && !e.startsWith("W/"));
    if (strong.length > 1) {
      // The object changed while it was read: a failure, not a rejection.
      throw new HttpResolutionError(`the object changed while it was read (ETags ${strong.sort().join(", ")})`);
    }
  }

  /** The object's strong ETag, if every response gave the same one. */
  pin(): string | undefined {
    if (this.seen.size !== 1) return undefined;
    const [etag] = this.seen;
    return etag !== null && STRONG_ETAG.test(etag) ? etag : undefined;
  }
}

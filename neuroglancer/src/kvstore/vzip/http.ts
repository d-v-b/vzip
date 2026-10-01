/**
 * @license
 * Copyright 2026 Google Inc.
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

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
 */

import type { VzipPins } from "#src/kvstore/vzip/proto.js";
import {
  checkHttpUrl,
  formatImfFixdate,
  parseImfFixdate,
} from "#src/kvstore/vzip/uri.js";

export class HttpResolutionError extends Error {}

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
  pins: VzipPins,
  signal?: AbortSignal,
): Promise<{ data: Uint8Array<ArrayBuffer>; size: number | undefined }> {
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
    response = await fetch(url, { headers, signal, redirect: "follow" });
  } catch (e) {
    signal?.throwIfAborted();
    throw new HttpResolutionError(`${url}: ${(e as Error).message}`);
  }
  const fail = (message: string): never => {
    throw new HttpResolutionError(`${url}: ${message}`);
  };
  if (response.redirected) {
    // The final URL must still satisfy the URL rules.
    try {
      checkHttpUrl(response.url);
    } catch (e) {
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
  // conditional headers (spec §6.2).
  if (pins.etag !== undefined && etag !== pins.etag) {
    fail(`ETag ${JSON.stringify(etag)} does not match the pin`);
  }
  if (pins.modifiedNotAfter !== undefined) {
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
    return { data: body.slice(start, end), size: body.length };
  }
  const contentRange = single(response.headers, "Content-Range");
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
  return { data: body, size: total };
}

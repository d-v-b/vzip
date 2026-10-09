// Reading byte ranges over HTTP (spec §6.2) with pin checks (§6.1).

import http from "node:http";
import https from "node:https";
import { resolutionErr, requestErr, VzError } from "./errors.ts";
import { parseUriReference, recompose, resolve, type Uri } from "./uri.ts";

export const MAX_HTTP_BODY = 1 << 30; // documented resource limit (1 GiB)

export type Pins = { size: bigint | null; etag: string | null; modifiedNotAfter: bigint | null };

const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** Formats seconds since the epoch as an IMF-fixdate; null if outside years 1..9999. */
export function formatImfFixdate(secs: bigint): string | null {
  // Year 1 Jan 1 = -62135596800; year 10000 Jan 1 = 253402300800
  if (secs < -62135596800n || secs >= 253402300800n) return null;
  const d = new Date(Number(secs) * 1000);
  const p2 = (n: number) => String(n).padStart(2, "0");
  return (
    `${DAYS[d.getUTCDay()]}, ${p2(d.getUTCDate())} ${MONTHS[d.getUTCMonth()]} ` +
    `${String(d.getUTCFullYear()).padStart(4, "0")} ${p2(d.getUTCHours())}:${p2(d.getUTCMinutes())}:${p2(d.getUTCSeconds())} GMT`
  );
}

/** Parses a strict IMF-fixdate into seconds since the epoch; null if invalid. */
export function parseImfFixdate(s: string): bigint | null {
  const m = /^(Mon|Tue|Wed|Thu|Fri|Sat|Sun), (\d{2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (\d{4}) (\d{2}):(\d{2}):(\d{2}) GMT$/.exec(s);
  if (!m) return null;
  const day = +m[2], mon = MONTHS.indexOf(m[3]), year = +m[4];
  const hh = +m[5], mm = +m[6], ss = +m[7];
  if (hh > 23 || mm > 59 || ss > 60) return null;
  const d = new Date(0);
  d.setUTCFullYear(year, mon, day);
  d.setUTCHours(0, 0, 0, 0);
  if (d.getUTCFullYear() !== year || d.getUTCMonth() !== mon || d.getUTCDate() !== day) return null;
  if (DAYS[d.getUTCDay()] !== m[1]) return null;
  return BigInt(d.getTime() / 1000) + BigInt(hh * 3600 + mm * 60 + ss);
}

export function isStrongEtag(s: string): boolean {
  return /^"[\x21\x23-\x7e]*"$/.test(s);
}

type Resp = { status: number; headers: http.IncomingHttpHeaders; raw: string[]; body: Buffer };

function headerValues(raw: string[], name: string): string[] {
  const out: string[] = [];
  for (let i = 0; i < raw.length; i += 2) if (raw[i].toLowerCase() === name) out.push(raw[i + 1]);
  return out;
}

function doRequest(u: Uri, headers: Record<string, string>): Promise<Resp> {
  const lib = u.scheme!.toLowerCase() === "https" ? https : http;
  const authority = u.authority!;
  let host = authority;
  let port: string | undefined;
  const m = /^(\[[^\]]*\]|[^:]*)(?::(\d*))?$/.exec(authority);
  if (m) {
    host = m[1];
    port = m[2] || undefined;
  }
  const hostname = host.startsWith("[") ? host.slice(1, -1) : decodeURIComponent(host);
  const reqPath = (u.path === "" ? "/" : u.path) + (u.query !== undefined ? "?" + u.query : "");
  return new Promise((resolveP, rejectP) => {
    const req = lib.request(
      {
        hostname,
        port: port ? Number(port) : undefined,
        path: reqPath,
        method: "GET",
        headers,
        agent: false,
      },
      (res) => {
        const chunks: Buffer[] = [];
        let total = 0;
        res.on("data", (c: Buffer) => {
          total += c.length;
          if (total > MAX_HTTP_BODY) {
            req.destroy();
            rejectP(requestErr(`HTTP response body exceeds ${MAX_HTTP_BODY} bytes`));
            return;
          }
          chunks.push(c);
        });
        res.on("end", () =>
          resolveP({ status: res.statusCode ?? 0, headers: res.headers, raw: res.rawHeaders, body: Buffer.concat(chunks) }),
        );
        res.on("error", (e) => rejectP(resolutionErr(`HTTP error: ${e.message}`)));
      },
    );
    req.on("error", (e) => rejectP(resolutionErr(`HTTP request failed: ${e.message}`)));
    req.end();
  });
}

function checkHttpUrl(u: Uri): void {
  const a = u.authority;
  if (a === undefined) throw resolutionErr("HTTP URL has no authority");
  if (a.includes("@")) throw resolutionErr("HTTP URL with userinfo");
  const host = a.startsWith("[") ? a.slice(0, a.indexOf("]") + 1) : a.split(":")[0];
  if (host === "") throw resolutionErr("HTTP URL with empty host");
}

/**
 * Reads bytes [start, end) of the HTTP object at `url` (end > start), checking pins.
 */
export async function httpReadRange(url: Uri, start: bigint, end: bigint, pins: Pins): Promise<Buffer> {
  const headers: Record<string, string> = {
    Range: `bytes=${start}-${end - 1n}`,
    "Accept-Encoding": "identity",
  };
  if (pins.etag !== null) headers["If-Match"] = pins.etag;
  if (pins.modifiedNotAfter !== null) {
    const d = formatImfFixdate(pins.modifiedNotAfter);
    if (d === null) throw resolutionErr("modified_not_after pin is outside years 1-9999 and cannot be sent");
    headers["If-Unmodified-Since"] = d;
  }
  let cur = url;
  let redirects = 0;
  for (;;) {
    checkHttpUrl(cur);
    let resp: Resp;
    try {
      resp = await doRequest(cur, headers);
    } catch (e) {
      if (e instanceof VzError) throw e;
      throw resolutionErr(`HTTP request failed: ${(e as Error).message}`);
    }
    const st = resp.status;
    if ([301, 302, 303, 307, 308].includes(st)) {
      redirects++;
      if (redirects > 5) throw resolutionErr("too many redirects");
      const locs = headerValues(resp.raw, "location");
      if (locs.length !== 1) throw resolutionErr("redirect without exactly one Location header");
      const ref = parseUriReference(locs[0]);
      if (ref === null) throw resolutionErr(`redirect Location is not a valid URI reference: ${locs[0]}`);
      const next = resolve(cur, ref);
      const sch = next.scheme!.toLowerCase();
      if (sch !== "http" && sch !== "https") throw resolutionErr(`redirect to unsupported scheme: ${recompose(next)}`);
      delete next.fragment;
      cur = next;
      continue;
    }
    if (st === 412) throw resolutionErr("HTTP 412: a pin failed");
    if (st === 416) throw resolutionErr("HTTP 416: object shorter than requested range");
    if (st !== 200 && st !== 206) throw resolutionErr(`HTTP status ${st}`);

    for (const ce of headerValues(resp.raw, "content-encoding")) {
      if (ce.trim().toLowerCase() !== "identity") throw resolutionErr(`Content-Encoding ${ce} not allowed`);
    }
    if (headerValues(resp.raw, "content-encoding").length > 1)
      throw resolutionErr("multiple Content-Encoding headers");

    let data: Buffer;
    let size: bigint | null;
    if (st === 200) {
      size = BigInt(resp.body.length);
      if (size < end) throw resolutionErr(`source has ${size} bytes, need ${end}`);
      data = resp.body.subarray(Number(start), Number(end));
    } else {
      const crs = headerValues(resp.raw, "content-range");
      if (crs.length !== 1) throw resolutionErr("206 without exactly one Content-Range");
      const ct = (resp.headers["content-type"] ?? "").toString().toLowerCase();
      if (ct.startsWith("multipart/byteranges")) throw resolutionErr("multipart/byteranges response");
      const m = /^bytes ([0-9]+)-([0-9]+)\/([0-9]+|\*)$/.exec(crs[0].trim());
      if (!m) throw resolutionErr(`invalid Content-Range: ${crs[0]}`);
      const a = BigInt(m[1]), z = BigInt(m[2]);
      if (a !== start || z !== end - 1n) throw resolutionErr(`Content-Range ${crs[0]} is not the requested range`);
      if (m[3] === "*") size = null;
      else {
        size = BigInt(m[3]);
        if (z >= size) throw resolutionErr(`Content-Range ${crs[0]}: end not less than total`);
      }
      if (BigInt(resp.body.length) !== z - a + 1n) throw resolutionErr("206 body length does not match Content-Range");
      data = resp.body;
    }
    // Pin checks against the final response.
    if (pins.size !== null) {
      if (size === null) throw resolutionErr("size pin cannot be checked: object size unknown");
      if (size !== pins.size) throw resolutionErr(`size pin failed: object has ${size} bytes, pin ${pins.size}`);
    }
    if (pins.etag !== null) {
      const ets = headerValues(resp.raw, "etag");
      if (ets.length !== 1) throw resolutionErr("etag pin cannot be checked: no single ETag header");
      const et = ets[0].trim();
      if (!isStrongEtag(et) || et !== pins.etag) throw resolutionErr(`etag pin failed: ${et} != ${pins.etag}`);
    }
    if (pins.modifiedNotAfter !== null) {
      const lms = headerValues(resp.raw, "last-modified");
      if (lms.length !== 1) throw resolutionErr("modified_not_after pin cannot be checked: no single Last-Modified");
      const t = parseImfFixdate(lms[0].trim());
      if (t === null) throw resolutionErr(`modified_not_after pin cannot be checked: bad Last-Modified ${lms[0]}`);
      if (t > pins.modifiedNotAfter) throw resolutionErr("modified_not_after pin failed");
    }
    return data;
  }
}

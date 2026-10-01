// HTTP(S) range reads with pin checks and redirects (spec §6.1, §6.2).

import * as http from "node:http";
import * as https from "node:https";
import { parseUriReference, resolve, uriToString, type Uri } from "./uri.ts";

export class HttpResolutionError extends Error {}

export type Pins = { size: bigint | null; etag: string | null; modifiedNotAfter: bigint | null };

const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** Formats seconds since the epoch as an IMF-fixdate; null if outside years 1..9999. */
export function formatImfFixdate(secs: bigint): string | null {
  // Year 1 Jan 1 00:00:00 = -62135596800; year 10000 Jan 1 = 253402300800
  if (secs < -62135596800n || secs >= 253402300800n) return null;
  const d = new Date(Number(secs) * 1000);
  const pad = (n: number, w = 2) => String(n).padStart(w, "0");
  return `${DAYS[d.getUTCDay()]}, ${pad(d.getUTCDate())} ${MONTHS[d.getUTCMonth()]} ${pad(d.getUTCFullYear(), 4)} ${pad(
    d.getUTCHours(),
  )}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())} GMT`;
}

function mkTime(year: number, mon: number, day: number, h: number, mi: number, s: number): bigint | null {
  if (mon < 0 || mon > 11 || day < 1 || day > 31 || h > 23 || mi > 59 || s > 60) return null;
  const d = new Date(0);
  d.setUTCFullYear(year, mon, day);
  d.setUTCHours(h, mi, s, 0);
  if (d.getUTCDate() !== day || d.getUTCMonth() !== mon) return null; // e.g. Feb 30
  return BigInt(Math.floor(d.getTime() / 1000));
}

/** Parses an HTTP-date (RFC 9110 §5.6.7: IMF-fixdate, rfc850-date, asctime-date). */
export function parseHttpDate(v: string): bigint | null {
  let m = /^(Mon|Tue|Wed|Thu|Fri|Sat|Sun), (\d{2}) (\w{3}) (\d{4}) (\d{2}):(\d{2}):(\d{2}) GMT$/.exec(v);
  if (m) {
    const mon = MONTHS.indexOf(m[3]);
    return mon < 0 ? null : mkTime(+m[4], mon, +m[2], +m[5], +m[6], +m[7]);
  }
  m = /^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), (\d{2})-(\w{3})-(\d{2}) (\d{2}):(\d{2}):(\d{2}) GMT$/.exec(v);
  if (m) {
    const mon = MONTHS.indexOf(m[3]);
    if (mon < 0) return null;
    // RFC 9110: two-digit year more than 50 years in the future is in the past century.
    const nowYear = new Date().getUTCFullYear();
    let year = 1900 + +m[4];
    while (year + 100 <= nowYear + 50) year += 100;
    return mkTime(year, mon, +m[2], +m[5], +m[6], +m[7]);
  }
  m = /^(Mon|Tue|Wed|Thu|Fri|Sat|Sun) (\w{3}) ([ \d]\d) (\d{2}):(\d{2}):(\d{2}) (\d{4})$/.exec(v);
  if (m) {
    const mon = MONTHS.indexOf(m[2]);
    return mon < 0 ? null : mkTime(+m[7], mon, +m[3].trim(), +m[4], +m[5], +m[6]);
  }
  return null;
}

/** Strong comparison of entity tags (RFC 9110 §8.8.3.2). */
function strongEqual(a: string, b: string): boolean {
  if (a.startsWith("W/") || b.startsWith("W/")) return false;
  return a === b;
}

type RawResponse = { status: number; headers: http.IncomingHttpHeaders; body: Buffer };

const MAX_BODY = 1 << 30;

function doRequest(u: Uri, headers: Record<string, string>, maxBody: number): Promise<RawResponse> {
  return new Promise((resolveP, reject) => {
    const scheme = u.scheme!.toLowerCase();
    const mod = scheme === "https" ? https : http;
    let auth = u.authority ?? "";
    const at = auth.lastIndexOf("@");
    if (at !== -1) auth = auth.slice(at + 1); // userinfo is not sent
    let host = auth;
    let port: string | undefined;
    const m = /^(\[[^\]]*\]|[^:]*)(?::(\d*))?$/.exec(auth);
    if (m) {
      host = m[1];
      port = m[2] || undefined;
    }
    if (host.startsWith("[")) host = host.slice(1, -1);
    else host = decodeURIComponent(host);
    if (host === "") {
      reject(new HttpResolutionError("HTTP URL has an empty host"));
      return;
    }
    const reqPath = (u.path === "" ? "/" : u.path) + (u.query !== null ? "?" + u.query : "");
    const req = mod.request(
      { method: "GET", host, port: port ? Number(port) : undefined, path: reqPath, headers, agent: false },
      (res) => {
        const chunks: Buffer[] = [];
        let total = 0;
        res.on("data", (c: Buffer) => {
          total += c.length;
          if (total > maxBody) {
            req.destroy(new HttpResolutionError("HTTP response body too large"));
            return;
          }
          chunks.push(c);
        });
        res.on("end", () => resolveP({ status: res.statusCode ?? 0, headers: res.headers, body: Buffer.concat(chunks) }));
        res.on("error", reject);
      },
    );
    req.on("error", (e) => reject(e instanceof HttpResolutionError ? e : new HttpResolutionError(`HTTP request failed: ${e.message}`)));
    req.end();
  });
}

function header(h: http.IncomingHttpHeaders, name: string): string | undefined {
  const v = h[name];
  return Array.isArray(v) ? v.join(", ") : v;
}

/**
 * Reads bytes [a, b) (b > a) of the HTTP object at `url`, checking pins.
 * Throws HttpResolutionError on any failure.
 */
export async function httpReadRange(url: Uri, a: bigint, b: bigint, pins: Pins): Promise<Uint8Array> {
  const headers: Record<string, string> = {
    Range: `bytes=${a}-${b - 1n}`,
    "Accept-Encoding": "identity",
  };
  if (pins.etag !== null) headers["If-Match"] = pins.etag;
  if (pins.modifiedNotAfter !== null) {
    const d = formatImfFixdate(pins.modifiedNotAfter);
    if (d === null) throw new HttpResolutionError("modified_not_after pin outside years 1-9999 cannot be sent");
    headers["If-Unmodified-Since"] = d;
  }
  let cur = url;
  let res: RawResponse;
  let redirects = 0;
  for (;;) {
    res = await doRequest(cur, headers, MAX_BODY);
    if ([301, 302, 303, 307, 308].includes(res.status)) {
      redirects++;
      if (redirects > 5) throw new HttpResolutionError("too many redirects");
      const loc = header(res.headers, "location");
      if (loc === undefined) throw new HttpResolutionError(`redirect ${res.status} without Location`);
      const ref = parseUriReference(loc);
      if (ref === null) throw new HttpResolutionError(`invalid redirect Location '${loc}'`);
      const next = resolve(cur, ref);
      const sch = next.scheme!.toLowerCase();
      if (sch !== "http" && sch !== "https") throw new HttpResolutionError(`redirect to unsupported scheme '${sch}'`);
      cur = next;
      continue;
    }
    break;
  }
  const where = uriToString(cur);
  const enc = header(res.headers, "content-encoding");
  if (enc !== undefined && enc.trim().toLowerCase() !== "identity")
    throw new HttpResolutionError(`${where}: Content-Encoding '${enc}'`);
  if (res.status === 412) throw new HttpResolutionError(`${where}: 412 Precondition Failed (pin failed)`);
  if (res.status === 416) throw new HttpResolutionError(`${where}: 416 Range Not Satisfiable (object too short)`);
  if (res.status !== 200 && res.status !== 206) throw new HttpResolutionError(`${where}: HTTP status ${res.status}`);

  // Pins checked on every successful response.
  if (pins.etag !== null) {
    const et = header(res.headers, "etag");
    if (et === undefined) throw new HttpResolutionError(`${where}: no ETag header; etag pin cannot be checked`);
    if (!strongEqual(et.trim(), pins.etag)) throw new HttpResolutionError(`${where}: ETag ${et} does not match pin ${pins.etag}`);
  }
  if (pins.modifiedNotAfter !== null) {
    const lm = header(res.headers, "last-modified");
    const t = lm === undefined ? null : parseHttpDate(lm.trim());
    if (t === null) throw new HttpResolutionError(`${where}: missing or invalid Last-Modified; pin cannot be checked`);
    if (t > pins.modifiedNotAfter) throw new HttpResolutionError(`${where}: Last-Modified ${lm} is after the pin`);
  }

  if (res.status === 206) {
    const cr = header(res.headers, "content-range");
    const m = cr === undefined ? null : /^bytes (\d+)-(\d+)\/(\d+|\*)$/.exec(cr.trim());
    if (m === null) throw new HttpResolutionError(`${where}: 206 without a valid Content-Range`);
    const ra = BigInt(m[1]);
    const rz = BigInt(m[2]);
    if (ra !== a || rz !== b - 1n) throw new HttpResolutionError(`${where}: server returned range ${cr}, requested ${a}-${b - 1n}`);
    if (BigInt(res.body.length) !== b - a) throw new HttpResolutionError(`${where}: body length does not match Content-Range`);
    if (m[3] === "*") {
      if (pins.size !== null) throw new HttpResolutionError(`${where}: object size unknown; size pin cannot be checked`);
    } else {
      const total = BigInt(m[3]);
      if (total <= rz) throw new HttpResolutionError(`${where}: invalid Content-Range ${cr}`);
      if (pins.size !== null && total !== pins.size)
        throw new HttpResolutionError(`${where}: size ${total} does not match pin ${pins.size}`);
    }
    return res.body;
  }
  // 200: whole object
  const size = BigInt(res.body.length);
  if (pins.size !== null && size !== pins.size) throw new HttpResolutionError(`${where}: size ${size} does not match pin ${pins.size}`);
  if (size < b) throw new HttpResolutionError(`${where}: object (${size} bytes) shorter than requested range end ${b}`);
  return res.body.subarray(Number(a), Number(b));
}

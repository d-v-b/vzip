// Reading byte ranges of external objects named by URL (spec §6, §6.1, §6.2).
import * as fs from "node:fs";
import * as http from "node:http";
import * as https from "node:https";
import { VzError } from "./errors.ts";
import { type Uri, fileUriToPath, parseUriRef, resolve, uriToString } from "./url.ts";

export interface Pins {
  size: bigint | null;
  etag: string | null;
  modifiedNotAfter: bigint | null;
}

function resErr(msg: string): VzError {
  return new VzError("resolution", msg);
}

/** Floor division for bigint (towards negative infinity). */
function floorDiv(a: bigint, b: bigint): bigint {
  const q = a / b;
  return (a % b !== 0n && (a < 0n) !== (b < 0n)) ? q - 1n : q;
}

const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const MIN_IMF = -62135596800n; // 0001-01-01T00:00:00Z
const MAX_IMF = 253402300799n; // 9999-12-31T23:59:59Z

/** IMF-fixdate (RFC 9110 §5.6.7) for seconds since the epoch, or null if outside years 1-9999. */
export function imfFixdate(secs: bigint): string | null {
  if (secs < MIN_IMF || secs > MAX_IMF) return null;
  const d = new Date(Number(secs) * 1000);
  const p2 = (n: number) => String(n).padStart(2, "0");
  return `${DAYS[d.getUTCDay()]}, ${p2(d.getUTCDate())} ${MONTHS[d.getUTCMonth()]} ${String(d.getUTCFullYear()).padStart(4, "0")} ${p2(d.getUTCHours())}:${p2(d.getUTCMinutes())}:${p2(d.getUTCSeconds())} GMT`;
}

/**
 * Read bytes [a, b) (a < b) of the object named by `url`, resolved against `base`, checking pins.
 * Throws VzError("resolution") on any failure.
 */
export async function readUrlRange(base: Uri, url: string, pins: Pins, a: bigint, b: bigint): Promise<Uint8Array> {
  const ref = parseUriRef(url);
  if (ref === null) throw resErr(`not a valid URI reference: ${JSON.stringify(url)}`);
  const target = resolve(base, ref);
  const scheme = (target.scheme ?? "").toLowerCase();
  if (scheme === "file") return readFileRange(target, pins, a, b);
  if (scheme === "http" || scheme === "https") return readHttpRange(target, pins, a, b);
  throw resErr(`unsupported URL scheme '${target.scheme}'`);
}

function readFileRange(target: Uri, pins: Pins, a: bigint, b: bigint): Uint8Array {
  let p: Buffer;
  try {
    p = fileUriToPath(target);
  } catch (e) {
    throw resErr((e as Error).message);
  }
  if (pins.etag !== null) throw resErr("an etag pin cannot be checked on a file: source");
  let fd: number;
  try {
    fd = fs.openSync(p, "r");
  } catch (e) {
    throw resErr(`cannot open ${uriToString(target)}: ${(e as Error).message}`);
  }
  try {
    const st = fs.fstatSync(fd, { bigint: true });
    if (!st.isFile()) throw resErr(`${uriToString(target)} is not a regular file`);
    if (pins.size !== null && st.size !== pins.size) {
      throw resErr(`size pin failed: file has ${st.size} bytes, pin says ${pins.size}`);
    }
    if (pins.modifiedNotAfter !== null) {
      const mtime = floorDiv(st.mtimeNs, 1_000_000_000n);
      if (mtime > pins.modifiedNotAfter) {
        throw resErr(`modified_not_after pin failed: mtime ${mtime} > ${pins.modifiedNotAfter}`);
      }
    }
    if (b > st.size) throw resErr(`source is shorter (${st.size} bytes) than the range end ${b}`);
    const len = Number(b - a);
    const buf = Buffer.alloc(len);
    let got = 0;
    while (got < len) {
      const n = fs.readSync(fd, buf, got, len - got, Number(a) + got);
      if (n === 0) throw resErr("unexpected end of file while reading source");
      got += n;
    }
    return buf;
  } catch (e) {
    if (e instanceof VzError) throw e;
    throw resErr(`cannot read ${uriToString(target)}: ${(e as Error).message}`);
  } finally {
    fs.closeSync(fd);
  }
}

interface HttpResponse {
  status: number;
  headers: http.IncomingHttpHeaders;
  body: Buffer;
}

function httpGet(u: Uri, headers: Record<string, string>, maxBody: number): Promise<HttpResponse> {
  return new Promise((resolveP, reject) => {
    const scheme = u.scheme!.toLowerCase();
    const lib = scheme === "https" ? https : http;
    const auth = u.authority ?? "";
    const hostport = auth.includes("@") ? auth.slice(auth.lastIndexOf("@") + 1) : auth;
    let hostname: string;
    let port: string | undefined;
    if (hostport.startsWith("[")) {
      const close = hostport.indexOf("]");
      hostname = hostport.slice(1, close);
      const rest = hostport.slice(close + 1);
      port = rest.startsWith(":") ? rest.slice(1) : undefined;
    } else {
      const c = hostport.indexOf(":");
      hostname = c >= 0 ? hostport.slice(0, c) : hostport;
      port = c >= 0 ? hostport.slice(c + 1) : undefined;
    }
    if (hostname === "") {
      reject(resErr("HTTP URL has no host"));
      return;
    }
    hostname = decodeURIComponent(hostname);
    const reqPath = (u.path === "" ? "/" : u.path) + (u.query !== undefined ? "?" + u.query : "");
    const req = lib.request(
      {
        method: "GET",
        hostname,
        port: port ? Number(port) : undefined,
        path: reqPath,
        headers,
      },
      (res) => {
        const chunks: Buffer[] = [];
        let total = 0;
        res.on("data", (c: Buffer) => {
          total += c.length;
          if (total > maxBody) {
            req.destroy(resErr(`HTTP response body larger than ${maxBody} bytes`));
            return;
          }
          chunks.push(c);
        });
        res.on("end", () => resolveP({ status: res.statusCode ?? 0, headers: res.headers, body: Buffer.concat(chunks) }));
        res.on("error", reject);
      },
    );
    req.on("error", reject);
    req.setTimeout(HTTP_TIMEOUT_MS, () => req.destroy(resErr(`HTTP request timed out after ${HTTP_TIMEOUT_MS} ms`)));
    req.end();
  });
}

const MAX_REDIRECTS = 5;
const HTTP_TIMEOUT_MS = 30_000;
export const MAX_HTTP_BODY = 1 << 30;

async function readHttpRange(target: Uri, pins: Pins, a: bigint, b: bigint): Promise<Uint8Array> {
  const headers: Record<string, string> = {
    Range: `bytes=${a}-${b - 1n}`,
    "Accept-Encoding": "identity",
  };
  if (pins.etag !== null) headers["If-Match"] = pins.etag;
  if (pins.modifiedNotAfter !== null) {
    const d = imfFixdate(pins.modifiedNotAfter);
    if (d === null) throw resErr("modified_not_after pin is outside years 1-9999 and cannot be sent");
    headers["If-Unmodified-Since"] = d;
  }
  let u: Uri = { ...target, fragment: undefined };
  let res: HttpResponse;
  for (let hops = 0; ; hops++) {
    try {
      res = await httpGet(u, headers, MAX_HTTP_BODY);
    } catch (e) {
      if (e instanceof VzError) throw e;
      throw resErr(`HTTP request to ${uriToString(u)} failed: ${(e as Error).message}`);
    }
    if ([301, 302, 303, 307, 308].includes(res.status) && typeof res.headers.location === "string") {
      if (hops >= MAX_REDIRECTS) throw resErr("too many HTTP redirects");
      const loc = parseUriRef(res.headers.location);
      if (loc === null) throw resErr(`invalid redirect Location ${JSON.stringify(res.headers.location)}`);
      const next = resolve(u, loc);
      const ns = (next.scheme ?? "").toLowerCase();
      if (ns !== "http" && ns !== "https") throw resErr(`redirect to unsupported scheme '${next.scheme}'`);
      u = { ...next, fragment: undefined };
      continue;
    }
    break;
  }
  const ce = res.headers["content-encoding"];
  if (ce !== undefined && ce.trim().toLowerCase() !== "identity") {
    throw resErr(`unexpected Content-Encoding '${ce}'`);
  }
  if (res.status === 206) {
    const cr = res.headers["content-range"];
    const m = typeof cr === "string" ? /^\s*bytes\s+(\d+)-(\d+)\/(\d+|\*)\s*$/i.exec(cr) : null;
    if (!m) throw resErr(`206 response without a usable single-range Content-Range (${cr})`);
    const first = BigInt(m[1]);
    const last = BigInt(m[2]);
    if (first !== a || last !== b - 1n) throw resErr(`server returned range ${first}-${last}, requested ${a}-${b - 1n}`);
    if (m[3] === "*") {
      if (pins.size !== null) throw resErr("size pin cannot be checked: server did not report the object size");
    } else {
      const total = BigInt(m[3]);
      if (pins.size !== null && total !== pins.size) throw resErr(`size pin failed: object has ${total} bytes, pin says ${pins.size}`);
      if (total < b) throw resErr(`Content-Range total ${total} is smaller than the requested range`);
    }
    if (BigInt(res.body.length) !== b - a) throw resErr(`206 body has ${res.body.length} bytes, expected ${b - a}`);
    return res.body;
  }
  if (res.status === 200) {
    const size = BigInt(res.body.length);
    if (pins.size !== null && size !== pins.size) throw resErr(`size pin failed: object has ${size} bytes, pin says ${pins.size}`);
    if (size < b) throw resErr(`source is shorter (${size} bytes) than the range end ${b}`);
    return res.body.subarray(Number(a), Number(b));
  }
  if (res.status === 412) throw resErr("HTTP 412: a pin failed");
  if (res.status === 416) throw resErr("HTTP 416: the object is shorter than the requested range");
  throw resErr(`unexpected HTTP status ${res.status}`);
}

// RFC 3986 URI-reference validation and resolution, and file: URI mapping (spec §6).
import * as path from "node:path";

export interface Uri {
  scheme: string | undefined;
  authority: string | undefined;
  path: string;
  query: string | undefined;
  fragment: string | undefined;
}

const UNRESERVED = "A-Za-z0-9\\-._~";
const SUBDELIMS = "!$&'()*+,;=";
const PCT = "%[0-9A-Fa-f]{2}";
const PCHAR = `(?:[${UNRESERVED}${SUBDELIMS}:@]|${PCT})`;
const reSegment = new RegExp(`^${PCHAR}*$`);
const reNcSegment = new RegExp(`^(?:[${UNRESERVED}${SUBDELIMS}@]|${PCT})*$`);
const reQueryFrag = new RegExp(`^(?:${PCHAR}|[/?])*$`);
const reScheme = /^[A-Za-z][A-Za-z0-9+\-.]*$/;
const reUserinfo = new RegExp(`^(?:[${UNRESERVED}${SUBDELIMS}:]|${PCT})*$`);
const reRegName = new RegExp(`^(?:[${UNRESERVED}${SUBDELIMS}]|${PCT})*$`);
const rePort = /^[0-9]*$/;
const reIpvFuture = new RegExp(`^v[0-9A-Fa-f]+\\.[${UNRESERVED}${SUBDELIMS}:]+$`);
const reDecOctet = /^(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9][0-9]|[0-9])$/;
const reH16 = /^[0-9A-Fa-f]{1,4}$/;

function isIPv4(s: string): boolean {
  const parts = s.split(".");
  return parts.length === 4 && parts.every((p) => reDecOctet.test(p));
}

function isIPv6(s: string): boolean {
  // Split on "::" (at most one occurrence).
  const dbl = s.split("::");
  if (dbl.length > 2) return false;
  const parsePart = (p: string): number | null => {
    // returns number of 16-bit groups, or null if invalid
    if (p === "") return 0;
    const groups = p.split(":");
    let n = 0;
    for (let i = 0; i < groups.length; i++) {
      const g = groups[i];
      if (i === groups.length - 1 && g.includes(".")) {
        if (!isIPv4(g)) return null;
        n += 2;
      } else {
        if (!reH16.test(g)) return null;
        n += 1;
      }
    }
    return n;
  };
  if (dbl.length === 1) {
    return parsePart(s) === 8;
  }
  const left = dbl[0];
  const right = dbl[1];
  // An embedded IPv4 is only allowed at the very end.
  if (left.includes(".")) return false;
  const l = parsePart(left);
  const r = parsePart(right);
  if (l === null || r === null) return false;
  return l + r <= 7;
}

function validAuthority(a: string): boolean {
  let rest = a;
  const at = rest.indexOf("@");
  if (at >= 0) {
    if (!reUserinfo.test(rest.slice(0, at))) return false;
    rest = rest.slice(at + 1);
  }
  let host: string;
  let port: string | undefined;
  if (rest.startsWith("[")) {
    const close = rest.indexOf("]");
    if (close < 0) return false;
    host = rest.slice(1, close);
    const after = rest.slice(close + 1);
    if (after !== "") {
      if (!after.startsWith(":")) return false;
      port = after.slice(1);
    }
    if (!(isIPv6(host) || reIpvFuture.test(host))) return false;
  } else {
    const colon = rest.indexOf(":");
    if (colon >= 0) {
      host = rest.slice(0, colon);
      port = rest.slice(colon + 1);
    } else host = rest;
    if (!reRegName.test(host)) return false; // IPv4address is a subset of reg-name
  }
  if (port !== undefined && !rePort.test(port)) return false;
  return true;
}

const reSplit = /^(?:([^:/?#]+):)?(?:\/\/([^/?#]*))?([^?#]*)(?:\?([^#]*))?(?:#(.*))?$/s;

/** Parse a string as an RFC 3986 URI-reference. Returns null if it doesn't match the grammar exactly. */
export function parseUriRef(s: string): Uri | null {
  // eslint-disable-next-line no-control-regex
  if (/[^\x21-\x7e]/.test(s)) return null; // ASCII printable only, no spaces
  const m = reSplit.exec(s);
  if (!m) return null;
  const [, scheme, authority, p, query, fragment] = m;
  if (scheme !== undefined && !reScheme.test(scheme)) return null;
  if (authority !== undefined && !validAuthority(authority)) return null;
  const segs = p.split("/");
  for (const seg of segs) if (!reSegment.test(seg)) return null;
  if (scheme === undefined && authority === undefined && !p.startsWith("/")) {
    // path-noscheme or path-empty: first segment may not contain ':'
    if (!reNcSegment.test(segs[0])) return null;
  }
  if (query !== undefined && !reQueryFrag.test(query)) return null;
  if (fragment !== undefined && !reQueryFrag.test(fragment)) return null;
  return { scheme, authority, path: p, query, fragment };
}

export function isUriReference(s: string): boolean {
  return parseUriRef(s) !== null;
}

// RFC 3986 §5.2.4
export function removeDotSegments(input: string): string {
  let inp = input;
  let out = "";
  while (inp.length > 0) {
    if (inp.startsWith("../")) inp = inp.slice(3);
    else if (inp.startsWith("./")) inp = inp.slice(2);
    else if (inp.startsWith("/./")) inp = inp.slice(2);
    else if (inp === "/.") inp = "/";
    else if (inp.startsWith("/../")) {
      inp = inp.slice(3);
      const i = out.lastIndexOf("/");
      out = i >= 0 ? out.slice(0, i) : "";
    } else if (inp === "/..") {
      inp = "/";
      const i = out.lastIndexOf("/");
      out = i >= 0 ? out.slice(0, i) : "";
    } else if (inp === "." || inp === "..") inp = "";
    else {
      const start = inp.startsWith("/") ? 1 : 0;
      const j = inp.indexOf("/", start);
      const seg = j < 0 ? inp : inp.slice(0, j);
      out += seg;
      inp = j < 0 ? "" : inp.slice(j);
    }
  }
  return out;
}

function merge(base: Uri, refPath: string): string {
  if (base.authority !== undefined && base.path === "") return "/" + refPath;
  const i = base.path.lastIndexOf("/");
  return (i >= 0 ? base.path.slice(0, i + 1) : "") + refPath;
}

// RFC 3986 §5.2.2, strict.
export function resolve(base: Uri, r: Uri): Uri {
  const t: Uri = { scheme: undefined, authority: undefined, path: "", query: undefined, fragment: undefined };
  if (r.scheme !== undefined) {
    t.scheme = r.scheme;
    t.authority = r.authority;
    t.path = removeDotSegments(r.path);
    t.query = r.query;
  } else {
    if (r.authority !== undefined) {
      t.authority = r.authority;
      t.path = removeDotSegments(r.path);
      t.query = r.query;
    } else {
      if (r.path === "") {
        t.path = base.path;
        t.query = r.query !== undefined ? r.query : base.query;
      } else {
        if (r.path.startsWith("/")) t.path = removeDotSegments(r.path);
        else t.path = removeDotSegments(merge(base, r.path));
        t.query = r.query;
      }
      t.authority = base.authority;
    }
    t.scheme = base.scheme;
  }
  t.fragment = r.fragment;
  return t;
}

export function uriToString(u: Uri): string {
  let s = "";
  if (u.scheme !== undefined) s += u.scheme + ":";
  if (u.authority !== undefined) s += "//" + u.authority;
  s += u.path;
  if (u.query !== undefined) s += "?" + u.query;
  if (u.fragment !== undefined) s += "#" + u.fragment;
  return s;
}

const FILE_PATH_OK = new Set<number>();
for (const c of "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~!$&'()*+,;=:@/") FILE_PATH_OK.add(c.charCodeAt(0));

/** Build the base URI for an archive opened from a local path (spec §6). */
export function fileBaseUri(localPath: string): string {
  // path.resolve joins with process.cwd() (getcwd) and normalises lexically without resolving symlinks.
  const abs = path.resolve(localPath);
  const bytes = Buffer.from(abs, "utf8");
  let s = "file://";
  for (const b of bytes) {
    if (FILE_PATH_OK.has(b)) s += String.fromCharCode(b);
    else s += "%" + b.toString(16).toUpperCase().padStart(2, "0");
  }
  return s;
}

/** Map a resolved file: URI to a local path (as bytes). Throws Error with a message on violation. */
export function fileUriToPath(u: Uri): Buffer {
  if (u.authority !== undefined && u.authority !== "" && u.authority.toLowerCase() !== "localhost") {
    throw new Error(`file: URI has a non-local authority '${u.authority}'`);
  }
  if (!u.path.startsWith("/")) throw new Error("file: URI path is not absolute");
  if (u.query !== undefined) throw new Error("file: URI has a query component");
  const out: number[] = [];
  const p = u.path;
  for (let i = 0; i < p.length; i++) {
    const c = p.charCodeAt(i);
    if (c === 0x25) {
      const v = parseInt(p.slice(i + 1, i + 3), 16);
      if (v === 0x2f) throw new Error("file: URI path contains an encoded '/'");
      if (v === 0) throw new Error("file: URI path contains a NUL byte");
      out.push(v);
      i += 2;
    } else out.push(c);
  }
  const bytes = Buffer.from(out);
  for (const seg of bytes.toString("latin1").split("/")) {
    if (seg === "." || seg === "..") throw new Error("file: URI path contains a dot segment after decoding");
  }
  return bytes;
}

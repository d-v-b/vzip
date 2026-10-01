// RFC 3986 URI-reference validation and strict resolution (§5.2.2),
// plus the vzip file: URI rules (spec §6).

import path from "node:path";

export type Uri = {
  scheme?: string;
  authority?: string;
  path: string;
  query?: string;
  fragment?: string;
};

const SPLIT = /^(?:([^:/?#]+):)?(?:\/\/([^/?#]*))?([^?#]*)(?:\?([^#]*))?(?:#(.*))?$/s;

const UNRESERVED = "A-Za-z0-9\\-._~";
const SUBDELIMS = "!$&'()*+,;=";
const PCT = "%[0-9A-Fa-f]{2}";
const PCHAR = `(?:[${UNRESERVED}${SUBDELIMS}:@]|${PCT})`;
const reScheme = /^[A-Za-z][A-Za-z0-9+\-.]*$/;
const rePath = new RegExp(`^(?:${PCHAR}|/)*$`);
const reQF = new RegExp(`^(?:${PCHAR}|[/?])*$`);
const reUserinfo = new RegExp(`^(?:[${UNRESERVED}${SUBDELIMS}:]|${PCT})*$`);
const reRegName = new RegExp(`^(?:[${UNRESERVED}${SUBDELIMS}]|${PCT})*$`);
const rePort = /^[0-9]*$/;
const reIPvFuture = new RegExp(`^v[0-9A-Fa-f]+\\.[${UNRESERVED}${SUBDELIMS}:]+$`);
const reDecOctet = "(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9][0-9]|[0-9])";
const reIPv4 = new RegExp(`^${reDecOctet}\\.${reDecOctet}\\.${reDecOctet}\\.${reDecOctet}$`);
const reH16 = /^[0-9A-Fa-f]{1,4}$/;

function isIPv6(s: string): boolean {
  // Split off an IPv4 tail if present.
  let groups = 0;
  let body = s;
  const lastColon = s.lastIndexOf(":");
  if (lastColon >= 0 && s.slice(lastColon + 1).includes(".")) {
    if (!reIPv4.test(s.slice(lastColon + 1))) return false;
    groups += 2;
    body = s.slice(0, lastColon + 1);
    // body now ends with ":"; if it ends with "::" keep that, else drop the trailing ":"
    if (body.endsWith("::")) {
      // fine
    } else {
      body = body.slice(0, -1);
      if (body === "") return false;
    }
  }
  const dbl = body.indexOf("::");
  if (dbl >= 0) {
    if (body.indexOf("::", dbl + 1) >= 0) return false;
    const left = body.slice(0, dbl);
    const right = body.slice(dbl + 2);
    const l = left === "" ? [] : left.split(":");
    const r = right === "" ? [] : right.split(":");
    if (![...l, ...r].every((g) => reH16.test(g))) return false;
    return l.length + r.length + groups <= 7;
  }
  const parts = body.split(":");
  if (!parts.every((g) => reH16.test(g))) return false;
  return parts.length + groups === 8;
}

function validAuthority(a: string): boolean {
  let hostport = a;
  const at = a.indexOf("@");
  if (at >= 0) {
    if (!reUserinfo.test(a.slice(0, at))) return false;
    hostport = a.slice(at + 1);
  }
  let host: string;
  let port = "";
  if (hostport.startsWith("[")) {
    const close = hostport.indexOf("]");
    if (close < 0) return false;
    host = hostport.slice(0, close + 1);
    const rest = hostport.slice(close + 1);
    if (rest !== "") {
      if (!rest.startsWith(":")) return false;
      port = rest.slice(1);
    }
    const inner = host.slice(1, -1);
    if (!(isIPv6(inner) || reIPvFuture.test(inner))) return false;
  } else {
    const c = hostport.indexOf(":");
    if (c >= 0) {
      host = hostport.slice(0, c);
      port = hostport.slice(c + 1);
    } else host = hostport;
    if (!reRegName.test(host)) return false;
  }
  return rePort.test(port);
}

/** Parses a string that must match RFC 3986 `URI-reference`; null if not. */
export function parseUriReference(s: string): Uri | null {
  const m = SPLIT.exec(s);
  if (!m) return null;
  const [, scheme, authority, p, query, fragment] = m;
  if (scheme !== undefined && !reScheme.test(scheme)) return null;
  if (authority !== undefined && !validAuthority(authority)) return null;
  if (!rePath.test(p)) return null;
  if (scheme === undefined && authority === undefined) {
    // relative-ref: path-noscheme: first segment must not contain ':'
    const first = p.split("/")[0];
    if (first.includes(":")) return null;
  }
  if (query !== undefined && !reQF.test(query)) return null;
  if (fragment !== undefined && !reQF.test(fragment)) return null;
  const u: Uri = { path: p };
  if (scheme !== undefined) u.scheme = scheme;
  if (authority !== undefined) u.authority = authority;
  if (query !== undefined) u.query = query;
  if (fragment !== undefined) u.fragment = fragment;
  return u;
}

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
      out = out.slice(0, Math.max(0, out.lastIndexOf("/")));
    } else if (inp === "/..") {
      inp = "/";
      out = out.slice(0, Math.max(0, out.lastIndexOf("/")));
    } else if (inp === "." || inp === "..") inp = "";
    else {
      const start = inp.startsWith("/") ? 1 : 0;
      let next = inp.indexOf("/", start);
      if (next < 0) next = inp.length;
      out += inp.slice(0, next);
      inp = inp.slice(next);
    }
  }
  return out;
}

function merge(base: Uri, refPath: string): string {
  if (base.authority !== undefined && base.path === "") return "/" + refPath;
  const i = base.path.lastIndexOf("/");
  return i < 0 ? refPath : base.path.slice(0, i + 1) + refPath;
}

/** RFC 3986 §5.2.2, strict. */
export function resolve(base: Uri, r: Uri): Uri {
  const t: Uri = { path: "" };
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
  for (const k of ["authority", "query", "fragment"] as const) if (t[k] === undefined) delete t[k];
  return t;
}

export function recompose(u: Uri): string {
  let s = "";
  if (u.scheme !== undefined) s += u.scheme + ":";
  if (u.authority !== undefined) s += "//" + u.authority;
  s += u.path;
  if (u.query !== undefined) s += "?" + u.query;
  if (u.fragment !== undefined) s += "#" + u.fragment;
  return s;
}

// ------------------------------------------------------------ file: URIs

const FILE_KEEP = new Set(
  Array.from("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~!$&'()*+,;=:@/"),
);

/** Spec §6: base URI of an archive opened from a local path. */
export function fileBaseUri(p: string): string {
  const abs = path.isAbsolute(p) ? p : process.cwd() + "/" + p;
  const out: string[] = [];
  for (const seg of abs.split("/")) {
    if (seg === "" || seg === ".") continue;
    if (seg === "..") {
      out.pop();
      continue;
    }
    out.push(seg);
  }
  const norm = "/" + out.join("/");
  let enc = "file://";
  for (const b of Buffer.from(norm, "utf8")) {
    const c = String.fromCharCode(b);
    if (b < 0x80 && FILE_KEEP.has(c)) enc += c;
    else enc += "%" + b.toString(16).toUpperCase().padStart(2, "0");
  }
  return enc;
}

/**
 * Maps a resolved file: URI to a local path (as a Buffer of bytes).
 * Returns an error message string on failure.
 */
export function fileUriToPath(u: Uri): Buffer | string {
  if (u.authority !== undefined && u.authority !== "" && u.authority.toLowerCase() !== "localhost")
    return `file: URI authority must be empty or localhost, got ${JSON.stringify(u.authority)}`;
  if (!u.path.startsWith("/")) return "file: URI path is not absolute";
  if (u.query !== undefined) return "file: URI must not have a query";
  const segs = u.path.split("/");
  const outSegs: Buffer[] = [];
  for (const seg of segs) {
    const bytes: number[] = [];
    for (let i = 0; i < seg.length; i++) {
      if (seg[i] === "%") {
        bytes.push(parseInt(seg.slice(i + 1, i + 3), 16));
        i += 2;
      } else bytes.push(seg.charCodeAt(i));
    }
    const b = Buffer.from(bytes);
    if (b.includes(0x2f)) return "file: URI path contains an encoded '/'";
    if (b.includes(0)) return "file: URI path contains a NUL byte";
    const s = b.toString("latin1");
    if (s === "." || s === "..") return "file: URI path contains a dot segment";
    outSegs.push(b);
  }
  const parts: Buffer[] = [];
  outSegs.forEach((b, i) => {
    if (i > 0) parts.push(Buffer.from("/"));
    parts.push(b);
  });
  return Buffer.concat(parts);
}

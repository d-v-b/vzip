// RFC 3986 URI-reference validation, parsing, strict resolution (§5.2.2),
// base URI construction from a local path and file: URI mapping (spec §6).

export type Uri = {
  scheme: string | null;
  authority: string | null;
  path: string;
  query: string | null;
  fragment: string | null;
};

const UNRESERVED = "A-Za-z0-9\\-._~";
const SUB_DELIMS = "!$&'()*+,;=";
const PCT = "%[0-9A-Fa-f]{2}";
const PCHAR = `(?:[${UNRESERVED}${SUB_DELIMS}:@]|${PCT})`;
const reSegment = new RegExp(`^${PCHAR}*$`);
const reSegmentNzNc = new RegExp(`^(?:[${UNRESERVED}${SUB_DELIMS}@]|${PCT})+$`);
const reQueryFrag = new RegExp(`^(?:${PCHAR}|[/?])*$`);
const reUserinfo = new RegExp(`^(?:[${UNRESERVED}${SUB_DELIMS}:]|${PCT})*$`);
const reRegName = new RegExp(`^(?:[${UNRESERVED}${SUB_DELIMS}]|${PCT})*$`);
const rePort = /^[0-9]*$/;
const reScheme = /^[A-Za-z][A-Za-z0-9+.\-]*$/;
const reIpvFuture = new RegExp(`^[vV][0-9A-Fa-f]+\\.[${UNRESERVED}${SUB_DELIMS}:]+$`);
const reDecOctet = /^(?:[0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])$/;
const reH16 = /^[0-9A-Fa-f]{1,4}$/;

function isIPv4(s: string): boolean {
  const p = s.split(".");
  return p.length === 4 && p.every((x) => reDecOctet.test(x));
}

export function isIPv6(s: string): boolean {
  // Handle "::" compression and an optional trailing IPv4 (ls32).
  const dbl = s.indexOf("::");
  if (dbl !== s.lastIndexOf("::")) return false;
  const parse = (part: string, allowV4Tail: boolean): number | null => {
    // returns number of 16-bit groups, or null if invalid
    if (part === "") return 0;
    const groups = part.split(":");
    let count = 0;
    for (let i = 0; i < groups.length; i++) {
      const g = groups[i];
      if (i === groups.length - 1 && allowV4Tail && g.includes(".")) {
        if (!isIPv4(g)) return null;
        count += 2;
      } else {
        if (!reH16.test(g)) return null;
        count += 1;
      }
    }
    return count;
  };
  if (dbl === -1) {
    const n = parse(s, true);
    return n === 8;
  }
  const head = s.slice(0, dbl);
  const tail = s.slice(dbl + 2);
  const a = parse(head, false);
  const b = parse(tail, true);
  if (a === null || b === null) return false;
  return a + b <= 7;
}

function validHost(h: string): boolean {
  if (h.startsWith("[")) {
    if (!h.endsWith("]")) return false;
    const inner = h.slice(1, -1);
    return isIPv6(inner) || reIpvFuture.test(inner);
  }
  return reRegName.test(h); // reg-name also covers IPv4address
}

function validAuthority(a: string): boolean {
  let rest = a;
  const at = rest.lastIndexOf("@");
  if (at !== -1) {
    // userinfo cannot contain '@', so the first '@' must be the only one
    if (rest.indexOf("@") !== at) return false;
    if (!reUserinfo.test(rest.slice(0, at))) return false;
    rest = rest.slice(at + 1);
  }
  // port: after the last ':' that is not inside an IP-literal
  let host = rest;
  let port: string | null = null;
  if (rest.startsWith("[")) {
    const close = rest.indexOf("]");
    if (close === -1) return false;
    host = rest.slice(0, close + 1);
    const after = rest.slice(close + 1);
    if (after !== "") {
      if (!after.startsWith(":")) return false;
      port = after.slice(1);
    }
  } else {
    const c = rest.indexOf(":");
    if (c !== -1) {
      host = rest.slice(0, c);
      port = rest.slice(c + 1);
    }
  }
  if (port !== null && !rePort.test(port)) return false;
  return validHost(host);
}

const reSplit = /^(?:([^:/?#]+):)?(?:\/\/([^/?#]*))?([^?#]*)(?:\?([^#]*))?(?:#(.*))?$/s;

/** Parses and validates a URI-reference (RFC 3986 §4.1). Returns null if invalid. */
export function parseUriReference(s: string): Uri | null {
  // ASCII only, no controls/space etc. (character classes below enforce it)
  const m = reSplit.exec(s);
  if (!m) return null;
  const scheme = m[1] ?? null;
  const authority = m[2] ?? null;
  const p = m[3];
  const query = m[4] ?? null;
  const fragment = m[5] ?? null;
  if (scheme !== null && !reScheme.test(scheme)) return null;
  if (authority !== null && !validAuthority(authority)) return null;
  if (query !== null && !reQueryFrag.test(query)) return null;
  if (fragment !== null && !reQueryFrag.test(fragment)) return null;
  const segs = p.split("/");
  if (!segs.every((x) => reSegment.test(x))) return null;
  if (authority !== null) {
    // path-abempty
    if (p !== "" && !p.startsWith("/")) return null;
  } else if (p.startsWith("//")) {
    return null; // would have been parsed as authority; unreachable
  } else if (scheme === null && p !== "" && !p.startsWith("/")) {
    // path-noscheme: first segment must not contain ':'
    if (!reSegmentNzNc.test(segs[0])) return null;
  }
  return { scheme, authority, path: p, query, fragment };
}

export function removeDotSegments(input: string): string {
  let inp = input;
  const out: string[] = [];
  while (inp.length > 0) {
    if (inp.startsWith("../")) inp = inp.slice(3);
    else if (inp.startsWith("./")) inp = inp.slice(2);
    else if (inp.startsWith("/./")) inp = inp.slice(2);
    else if (inp === "/.") inp = "/";
    else if (inp.startsWith("/../")) {
      inp = inp.slice(3);
      out.pop();
    } else if (inp === "/..") {
      inp = "/";
      out.pop();
    } else if (inp === "." || inp === "..") inp = "";
    else {
      const start = inp.startsWith("/") ? 1 : 0;
      let next = inp.indexOf("/", start);
      if (next === -1) next = inp.length;
      out.push(inp.slice(0, next));
      inp = inp.slice(next);
    }
  }
  return out.join("");
}

function merge(base: Uri, refPath: string): string {
  if (base.authority !== null && base.path === "") return "/" + refPath;
  const i = base.path.lastIndexOf("/");
  return i === -1 ? refPath : base.path.slice(0, i + 1) + refPath;
}

/** Strict RFC 3986 §5.2.2 resolution. */
export function resolve(base: Uri, r: Uri): Uri {
  const t: Uri = { scheme: null, authority: null, path: "", query: null, fragment: null };
  if (r.scheme !== null) {
    t.scheme = r.scheme;
    t.authority = r.authority;
    t.path = removeDotSegments(r.path);
    t.query = r.query;
  } else {
    if (r.authority !== null) {
      t.authority = r.authority;
      t.path = removeDotSegments(r.path);
      t.query = r.query;
    } else {
      if (r.path === "") {
        t.path = base.path;
        t.query = r.query !== null ? r.query : base.query;
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
  if (u.scheme !== null) s += u.scheme + ":";
  if (u.authority !== null) s += "//" + u.authority;
  s += u.path;
  if (u.query !== null) s += "?" + u.query;
  if (u.fragment !== null) s += "#" + u.fragment;
  return s;
}

const FILE_PATH_SAFE = new Set<number>();
for (const c of "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~!$&'()*+,;=:@/")
  FILE_PATH_SAFE.add(c.charCodeAt(0));

/** Normalises a local path lexically against getcwd (spec §6, base URI). */
export function normaliseLocalPath(p: string): string {
  const abs = p.startsWith("/") ? p : process.cwd() + "/" + p;
  const out: string[] = [];
  for (const seg of abs.split("/")) {
    if (seg === "" || seg === ".") continue;
    if (seg === "..") {
      out.pop();
      continue;
    }
    out.push(seg);
  }
  return "/" + out.join("/");
}

/** Base URI for an archive opened from a local path. */
export function baseUriForPath(p: string): string {
  const norm = normaliseLocalPath(p);
  let s = "file://";
  for (const b of Buffer.from(norm, "utf8")) {
    if (FILE_PATH_SAFE.has(b)) s += String.fromCharCode(b);
    else s += "%" + b.toString(16).toUpperCase().padStart(2, "0");
  }
  return s;
}

export class UriMapError extends Error {}

/** Maps a resolved file: URI to local path bytes (spec §6). Throws UriMapError. */
export function fileUriToPath(u: Uri): Buffer {
  if (u.scheme === null || u.scheme.toLowerCase() !== "file") throw new UriMapError("not a file: URI");
  if (u.authority !== null && u.authority !== "" && u.authority.toLowerCase() !== "localhost")
    throw new UriMapError(`file: URI has a non-local authority '${u.authority}'`);
  if (!u.path.startsWith("/")) throw new UriMapError("file: URI path is not absolute");
  if (u.query !== null) throw new UriMapError("file: URI has a query component");
  const segs = u.path.split("/");
  const outSegs: Buffer[] = [];
  for (const seg of segs) {
    const bytes = pctDecode(seg);
    if (bytes.includes(0x2f)) throw new UriMapError("file: URI path contains an encoded '/'");
    if (bytes.includes(0)) throw new UriMapError("file: URI path contains a NUL byte");
    const str = bytes.toString("latin1");
    if (str === "." || str === "..") throw new UriMapError("file: URI path contains a dot segment");
    outSegs.push(bytes);
  }
  const parts: Buffer[] = [];
  outSegs.forEach((b, i) => {
    if (i > 0) parts.push(Buffer.from("/"));
    parts.push(b);
  });
  return Buffer.concat(parts);
}

function pctDecode(s: string): Buffer {
  const out: number[] = [];
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c === 0x25 && i + 2 < s.length && /^[0-9A-Fa-f]{2}$/.test(s.slice(i + 1, i + 3))) {
      out.push(parseInt(s.slice(i + 1, i + 3), 16));
      i += 2;
    } else {
      out.push(c & 0xff);
    }
  }
  return Buffer.from(out);
}

// RFC 3986 URI-reference validation and resolution, and file: URI mapping (spec §6).
import * as path from "node:path";

const UNRESERVED = /[A-Za-z0-9\-._~]/;
const SUB_DELIMS = /[!$&'()*+,;=]/;
const HEX = /^[0-9A-Fa-f]{2}$/;

/** Check that `s` consists of the allowed single characters and valid %HH escapes. */
function checkChars(s: string, extra: string): boolean {
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (c === "%") {
      if (!HEX.test(s.slice(i + 1, i + 3))) return false;
      i += 2;
      continue;
    }
    if (UNRESERVED.test(c) || SUB_DELIMS.test(c) || extra.includes(c)) continue;
    return false;
  }
  return true;
}

function isIPv6(s: string): boolean {
  // IPv6address per RFC 3986 §3.2.2, including an embedded IPv4 tail.
  let parts = s;

  const lastColon = s.lastIndexOf(":");
  if (lastColon >= 0 && s.slice(lastColon + 1).includes(".")) {
    if (!isIPv4(s.slice(lastColon + 1))) return false;

    parts = s.slice(0, lastColon + 1) + "0:0"; // stand-in for two h16
    // careful: "::1.2.3.4" -> "::0:0"
  }
  const dbl = parts.indexOf("::");
  if (dbl !== parts.lastIndexOf("::")) return false;
  const h16 = /^[0-9A-Fa-f]{1,4}$/;
  const groups = (str: string): string[] | null => {
    if (str === "") return [];
    const g = str.split(":");
    return g.every((x) => h16.test(x)) ? g : null;
  };
  if (dbl >= 0) {
    const left = groups(parts.slice(0, dbl));
    const right = groups(parts.slice(dbl + 2));
    if (left === null || right === null) return false;
    return left.length + right.length <= 7;
  }
  const all = groups(parts);
  return all !== null && all.length === 8;
}

function isIPv4(s: string): boolean {
  const m = s.split(".");
  if (m.length !== 4) return false;
  return m.every((x) => /^(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9][0-9]|[0-9])$/.test(x));
}

function isValidAuthority(a: string): boolean {
  let hostport = a;
  const at = a.indexOf("@");
  if (at >= 0) {
    const userinfo = a.slice(0, at);
    hostport = a.slice(at + 1);
    if (!checkChars(userinfo, ":")) return false;
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
    if (/^[vV]/.test(inner)) {
      if (!/^[vV][0-9A-Fa-f]+\.[A-Za-z0-9\-._~!$&'()*+,;=:]+$/.test(inner)) return false;
    } else if (!isIPv6(inner)) {
      return false;
    }
  } else {
    const colon = hostport.indexOf(":");
    if (colon >= 0) {
      host = hostport.slice(0, colon);
      port = hostport.slice(colon + 1);
    } else {
      host = hostport;
    }
    if (!checkChars(host, "")) return false; // reg-name (covers IPv4address)
  }
  return /^[0-9]*$/.test(port);
}

/** True iff `s` matches the RFC 3986 §4.1 `URI-reference` rule exactly. */
export function isUriReference(s: string): boolean {
  let rest = s;
  const hash = rest.indexOf("#");
  if (hash >= 0) {
    if (!checkChars(rest.slice(hash + 1), ":@/?")) return false;
    rest = rest.slice(0, hash);
  }
  const q = rest.indexOf("?");
  if (q >= 0) {
    if (!checkChars(rest.slice(q + 1), ":@/?")) return false;
    rest = rest.slice(0, q);
  }
  const sm = /^([A-Za-z][A-Za-z0-9+\-.]*):/.exec(rest);
  let hier = rest;
  if (sm) {
    hier = rest.slice(sm[0].length);
  } else {
    // relative-ref: path-noscheme forbids ':' in the first segment
    const firstSeg = rest.split("/")[0];
    if (!rest.startsWith("/") && firstSeg.includes(":")) return false;
  }
  let p = hier;
  if (hier.startsWith("//")) {
    const end = hier.indexOf("/", 2);
    const auth = end >= 0 ? hier.slice(2, end) : hier.slice(2);
    p = end >= 0 ? hier.slice(end) : "";
    if (!isValidAuthority(auth)) return false;
  }
  return checkChars(p, ":@/");
}

interface Parts {
  scheme?: string;
  authority?: string;
  path: string;
  query?: string;
  fragment?: string;
}

/** RFC 3986 Appendix B parse. */
export function parseUri(s: string): Parts {
  const m = /^(([^:/?#]+):)?(\/\/([^/?#]*))?([^?#]*)(\?([^#]*))?(#(.*))?$/s.exec(s)!;
  return {
    scheme: m[1] !== undefined ? m[2] : undefined,
    authority: m[3] !== undefined ? m[4] : undefined,
    path: m[5],
    query: m[6] !== undefined ? m[7] : undefined,
    fragment: m[8] !== undefined ? m[9] : undefined,
  };
}

function recompose(p: Parts): string {
  let r = "";
  if (p.scheme !== undefined) r += p.scheme + ":";
  if (p.authority !== undefined) r += "//" + p.authority;
  r += p.path;
  if (p.query !== undefined) r += "?" + p.query;
  if (p.fragment !== undefined) r += "#" + p.fragment;
  return r;
}

/** RFC 3986 §5.2.4 */
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
      const next = inp.indexOf("/", start);
      const seg = next >= 0 ? inp.slice(0, next) : inp;
      out.push(seg);
      inp = next >= 0 ? inp.slice(next) : "";
    }
  }
  return out.join("");
}

function merge(base: Parts, refPath: string): string {
  if (base.authority !== undefined && base.path === "") return "/" + refPath;
  const i = base.path.lastIndexOf("/");
  return (i >= 0 ? base.path.slice(0, i + 1) : "") + refPath;
}

/** RFC 3986 §5.2.2 strict resolution. `base` must be an absolute URI. */
export function resolveUri(baseStr: string, refStr: string): string {
  const base = parseUri(baseStr);
  const r = parseUri(refStr);
  const t: Parts = { path: "" };
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
  return recompose(t);
}

const PATH_SAFE = /[A-Za-z0-9\-._~!$&'()*+,;=:@/]/;

/** Build the base URI of an archive opened from a local path (§6). */
export function fileBaseUri(p: string): string {
  const abs = path.resolve(process.cwd(), p); // lexical normalisation, no symlink resolution
  const bytes = Buffer.from(abs, "utf8");
  let out = "file://";
  for (const b of bytes) {
    const c = String.fromCharCode(b);
    if (b < 0x80 && PATH_SAFE.test(c)) out += c;
    else out += "%" + b.toString(16).toUpperCase().padStart(2, "0");
  }
  return out;
}

/**
 * Map a resolved `file:` URI to a local path (as raw bytes), or return an
 * error message when it violates the §6 rules.
 */
export function fileUriToPath(uri: string): Buffer | string {
  const p = parseUri(uri);
  if (p.scheme === undefined || p.scheme.toLowerCase() !== "file") return "not a file: URI";
  if (p.authority !== undefined && p.authority !== "" && p.authority.toLowerCase() !== "localhost") {
    return `file: URI has a non-local authority '${p.authority}'`;
  }
  if (!p.path.startsWith("/")) return "file: URI path is not absolute";
  if (p.query !== undefined) return "file: URI has a query component";
  const segs = p.path.split("/");
  const decoded: Buffer[] = [];
  for (const seg of segs) {
    const bytes: number[] = [];
    for (let i = 0; i < seg.length; i++) {
      if (seg[i] === "%") {
        bytes.push(parseInt(seg.slice(i + 1, i + 3), 16));
        i += 2;
      } else {
        bytes.push(seg.charCodeAt(i));
      }
    }
    if (bytes.includes(0x2f)) return "file: URI path contains an encoded '/'";
    if (bytes.includes(0)) return "file: URI path contains a NUL byte";
    const b = Buffer.from(bytes);
    const str = b.toString("latin1");
    if (str === "." || str === "..") return "file: URI path contains a dot segment";
    decoded.push(b);
  }
  const out: Buffer[] = [];
  decoded.forEach((b, i) => {
    if (i > 0) out.push(Buffer.from("/"));
    out.push(b);
  });
  return Buffer.concat(out);
}

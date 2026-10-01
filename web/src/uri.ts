/**
 * @file Strict RFC 3986 handling for vzip `url` sources (spec §6), and the
 * IMF-fixdate format used by `modified_not_after` pins (spec §6.2).
 *
 * The WHATWG `URL` parser repairs and normalizes invalid input, which the
 * spec does not allow, so references are validated and resolved here.
 */

const PCT = "%[0-9A-Fa-f]{2}";
const UNRESERVED = "A-Za-z0-9\\-._~";
const SUB_DELIMS = "!$&'()*+,;=";
const PCHAR = `(?:[${UNRESERVED}${SUB_DELIMS}:@]|${PCT})`;
const SPLIT =
  /^(?:([^:/?#]+):)?(?:\/\/([^/?#]*))?([^?#]*)(?:\?([^#]*))?(?:#(.*))?$/;
const SCHEME = /^[A-Za-z][A-Za-z0-9+.-]*$/;
const AUTHORITY = new RegExp(
  `^(?:(?:[${UNRESERVED}${SUB_DELIMS}:]|${PCT})*@)?` +
    `(?:\\[[0-9A-Fa-f:.vV${UNRESERVED}${SUB_DELIMS}]+\\]|(?:[${UNRESERVED}${SUB_DELIMS}]|${PCT})*)` +
    "(?::[0-9]*)?$",
);
const PATH = new RegExp(`^(?:${PCHAR}|/)*$`);
const QUERY = new RegExp(`^(?:${PCHAR}|[/?])*$`);

interface Parts {
  scheme?: string;
  authority?: string;
  path: string;
  query?: string;
  fragment?: string;
}

function split(ref: string): Parts {
  const m = ref.match(SPLIT)!;
  return {
    scheme: m[1],
    authority: m[2],
    path: m[3],
    query: m[4],
    fragment: m[5],
  };
}

/** Does `ref` match the RFC 3986 `URI-reference` rule exactly? */
export function isUriReference(ref: string): boolean {
  // eslint-disable-next-line no-control-regex
  if (!/^[\x00-\x7f]*$/.test(ref)) return false;
  const { scheme, authority, path, query, fragment } = split(ref);
  if (scheme !== undefined && !SCHEME.test(scheme)) return false;
  if (authority !== undefined && !AUTHORITY.test(authority)) return false;
  if (!PATH.test(path)) return false;
  if (authority !== undefined && path !== "" && !path.startsWith("/")) {
    return false;
  }
  if (scheme === undefined && authority === undefined) {
    // A relative-path reference cannot have a colon in its first segment.
    if (path.split("/", 1)[0].includes(":")) return false;
  }
  for (const part of [query, fragment]) {
    if (part !== undefined && !QUERY.test(part)) return false;
  }
  return true;
}

/** RFC 3986 §5.2.4. */
function removeDotSegments(input: string): string {
  const out: string[] = [];
  let path = input;
  while (path !== "") {
    if (path.startsWith("../")) path = path.slice(3);
    else if (path.startsWith("./")) path = path.slice(2);
    else if (path.startsWith("/./")) path = path.slice(2);
    else if (path === "/.") path = "/";
    else if (path.startsWith("/../")) {
      path = path.slice(3);
      out.pop();
    } else if (path === "/..") {
      path = "/";
      out.pop();
    } else if (path === "." || path === "..") path = "";
    else {
      const i = path.indexOf("/", 1);
      const segment = i === -1 ? path : path.slice(0, i);
      path = i === -1 ? "" : path.slice(i);
      out.push(segment);
    }
  }
  return out.join("");
}

function compose(p: Parts): string {
  let s = p.scheme !== undefined ? `${p.scheme}:` : "";
  if (p.authority !== undefined) s += `//${p.authority}`;
  s += p.path;
  if (p.query !== undefined) s += `?${p.query}`;
  if (p.fragment !== undefined) s += `#${p.fragment}`;
  return s;
}

/**
 * Resolves `ref` against `base` with the strict algorithm of RFC 3986
 * §5.2.2. Throws if `ref` is not a valid URI reference.
 */
export function resolveReference(base: string, ref: string): string {
  if (!isUriReference(ref)) {
    throw new Error(`not a valid URI reference: ${JSON.stringify(ref)}`);
  }
  const b = split(base);
  const r = split(ref);
  if (r.scheme !== undefined) {
    return compose({ ...r, path: removeDotSegments(r.path) });
  }
  if (r.authority !== undefined) {
    return compose({ ...r, scheme: b.scheme, path: removeDotSegments(r.path) });
  }
  if (r.path === "") {
    return compose({
      scheme: b.scheme,
      authority: b.authority,
      path: b.path,
      query: r.query ?? b.query,
      fragment: r.fragment,
    });
  }
  let path: string;
  if (r.path.startsWith("/")) path = removeDotSegments(r.path);
  else if (b.authority !== undefined && b.path === "") {
    path = removeDotSegments("/" + r.path);
  } else {
    path = removeDotSegments(
      b.path.slice(0, b.path.lastIndexOf("/") + 1) + r.path,
    );
  }
  return compose({
    scheme: b.scheme,
    authority: b.authority,
    path,
    query: r.query,
    fragment: r.fragment,
  });
}

export function getScheme(url: string): string | undefined {
  return split(url).scheme?.toLowerCase();
}

/**
 * Checks the URL rules of spec §6.2 for an http(s) URL: no userinfo, a
 * non-empty host, and a port of at most 65535. Throws if they fail.
 */
export function checkHttpUrl(url: string) {
  const { authority } = split(url);
  if (authority === undefined || authority === "") {
    throw new Error(`${url}: empty or absent host`);
  }
  if (authority.includes("@")) throw new Error(`${url}: userinfo in URL`);
  const hostPort = authority.match(/^(\[[^\]]*\]|[^:]*)(?::(.*))?$/)!;
  if (hostPort[1] === "") throw new Error(`${url}: empty host`);
  const port = hostPort[2];
  if (port !== undefined && port !== "") {
    if (!/^[0-9]+$/.test(port) || Number(port) > 65535) {
      throw new Error(`${url}: invalid port ${JSON.stringify(port)}`);
    }
  }
}

const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
];

/**
 * Seconds since the Unix epoch for an IMF-fixdate (RFC 9110 §5.6.7), or
 * `undefined` if `value` is not one. Names are case-sensitive, the day name
 * must match the date, and a second of 60 counts as the following second
 * (spec §6.2).
 */
export function parseImfFixdate(value: string | null): number | undefined {
  if (value === null) return undefined;
  const m = value.match(
    /^([A-Za-z]{3}), (\d{2}) ([A-Za-z]{3}) (\d{4}) (\d{2}):(\d{2}):(\d{2}) GMT$/,
  );
  if (m === null) return undefined;
  const month = MONTHS.indexOf(m[3]);
  if (!DAYS.includes(m[1]) || month === -1) return undefined;
  const [day, year, hour, minute, second] = [m[2], m[4], m[5], m[6], m[7]].map(
    Number,
  );
  if (hour > 23 || minute > 59 || second > 60) return undefined;
  const date = new Date(0);
  date.setUTCFullYear(year, month, day);
  date.setUTCHours(hour, minute, second === 60 ? 59 : second);
  if (date.getUTCDate() !== day || date.getUTCMonth() !== month) {
    return undefined; // e.g. 31 Feb
  }
  if (DAYS[date.getUTCDay()] !== m[1]) return undefined;
  return Math.floor(date.getTime() / 1000) + (second === 60 ? 1 : 0);
}

/** Formats seconds since the epoch as an IMF-fixdate, or throws if the year is outside 1–9999. */
export function formatImfFixdate(seconds: bigint): string {
  const date = new Date(0);
  date.setUTCSeconds(Number(seconds));
  const year = date.getUTCFullYear();
  if (!Number.isFinite(date.getTime()) || year < 1 || year > 9999) {
    throw new Error("modified_not_after cannot be written as an HTTP-date");
  }
  const pad = (n: number, w = 2) => String(n).padStart(w, "0");
  return (
    `${DAYS[date.getUTCDay()]}, ${pad(date.getUTCDate())} ` +
    `${MONTHS[date.getUTCMonth()]} ${pad(year, 4)} ${pad(date.getUTCHours())}:` +
    `${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())} GMT`
  );
}

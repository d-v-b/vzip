// The OME-XML tag scan of VIRTUALIZE.md §3.2.

export type Tag = {
  start: number; // index of '<'
  end: number; // index after '>'
  isEnd: boolean;
  self: boolean;
  name: string; // local name
  attrs: Map<string, string>; // raw (undecoded) values, first of duplicates
};

export type Scan = { tags: Tag[]; skips: [number, number][] };

const isWs = (c: string | undefined) => c === " " || c === "\t" || c === "\r" || c === "\n";
const isNameChar = (c: string | undefined) => c !== undefined && /^[A-Za-z0-9_.\-]$/.test(c);
const isAnameChar = (c: string | undefined) =>
  c !== undefined && !isWs(c) && c !== "=" && c !== "/" && c !== ">" && c !== '"' && c !== "'" && c !== "<";

function wsRun(x: string, i: number): number {
  while (i < x.length && isWs(x[i])) i++;
  return i;
}

function nameRun(x: string, i: number): number {
  while (i < x.length && isNameChar(x[i])) i++;
  return i;
}

function matchTag(x: string, p: number): Tag | null {
  let q = p + 1;
  let isEnd = false;
  if (x[q] === "/") {
    isEnd = true;
    q++;
  }
  const n1 = nameRun(x, q);
  if (n1 === q) return null;
  let name = x.slice(q, n1);
  q = n1;
  if (x[q] === ":") {
    const n2 = nameRun(x, q + 1);
    if (n2 === q + 1) return null;
    name = x.slice(q + 1, n2);
    q = n2;
  }
  const attrs = new Map<string, string>();
  for (;;) {
    const w = wsRun(x, q);
    if (w > q && isAnameChar(x[w])) {
      let a = w;
      while (a < x.length && isAnameChar(x[a])) a++;
      const aname = x.slice(w, a);
      let r = wsRun(x, a);
      if (x[r] !== "=") return null;
      r = wsRun(x, r + 1);
      const quote = x[r];
      if (quote !== '"' && quote !== "'") return null;
      const close = x.indexOf(quote, r + 1);
      if (close < 0) return null;
      if (!attrs.has(aname)) attrs.set(aname, x.slice(r + 1, close));
      q = close + 1;
      continue;
    }
    q = w;
    break;
  }
  let self = false;
  if (x[q] === "/") {
    self = true;
    q++;
  }
  if (x[q] !== ">") return null;
  return { start: p, end: q + 1, isEnd, self, name, attrs };
}

export function scan(x: string): Scan {
  const tags: Tag[] = [];
  const skips: [number, number][] = [];
  let i = 0;
  const skipTo = (from: number, endMark: string, j: number) => {
    const e = x.indexOf(endMark, from);
    const end = e < 0 ? x.length : e + endMark.length;
    skips.push([j, end]);
    return end;
  };
  for (;;) {
    const j = x.indexOf("<", i);
    if (j < 0) break;
    if (x.startsWith("<!--", j)) i = skipTo(j + 4, "-->", j);
    else if (x.startsWith("<![CDATA[", j)) i = skipTo(j + 9, "]]>", j);
    else if (x.startsWith("<?", j)) i = skipTo(j + 2, "?>", j);
    else if (x.startsWith("<!", j)) i = skipTo(j + 2, ">", j);
    else {
      const t = matchTag(x, j);
      if (t) {
        tags.push(t);
        i = t.end;
      } else i = j + 1;
    }
  }
  return { tags, skips };
}

const ENT: Record<string, string> = { lt: "<", gt: ">", amp: "&", quot: '"', apos: "'" };

export function decodeRefs(s: string): string {
  return s.replace(/&(lt|gt|amp|quot|apos);|&#([0-9]+);|&#x([0-9A-Fa-f]+);/g, (m, ent, dec, hex) => {
    if (ent !== undefined) return ENT[ent];
    const digits: string = dec !== undefined ? dec : hex;
    const v = digits.replace(/^0+/, "");
    if (v.length > 8) return m;
    const cp = parseInt(v || "0", dec !== undefined ? 10 : 16);
    if (cp === 0 || (cp >= 0xd800 && cp <= 0xdfff) || cp > 0x10ffff) return m;
    return String.fromCodePoint(cp);
  });
}

/** Characters of x in [a, b) with skipped sections removed. */
export function textBetween(x: string, a: number, b: number, skips: [number, number][]): string {
  let out = "";
  let p = a;
  for (const [s, e] of skips) {
    if (e <= p || s >= b) continue;
    if (s > p) out += x.slice(p, s);
    p = Math.max(p, e);
  }
  if (p < b) out += x.slice(p, b);
  return out;
}

export function trimWs(s: string): string {
  return s.replace(/^[ \t\r\n]+/, "").replace(/[ \t\r\n]+$/, "");
}

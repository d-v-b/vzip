// The OME-XML tag scan of VIRTUALIZE.md §3.2.

export interface Tag {
  start: number; // index of "<"
  end: number; // index after ">"
  isEnd: boolean;
  selfClosing: boolean;
  name: string; // local name
  attrs: Map<string, string>; // decoded values, first of duplicates
}

export interface Skip {
  start: number;
  end: number;
}

export type Token = ({ kind: "tag" } & Tag) | ({ kind: "skip" } & Skip);

const TAG_RE =
  /<(\/?)(?:[A-Za-z0-9_.\-]+:)?([A-Za-z0-9_.\-]+)((?:[ \t\r\n]+[^ \t\r\n=\/>"'<]+[ \t\r\n]*=[ \t\r\n]*(?:"[^"]*"|'[^']*'))*)[ \t\r\n]*(\/?)>/y;
const ATTR_RE = /[ \t\r\n]+([^ \t\r\n=\/>"'<]+)[ \t\r\n]*=[ \t\r\n]*(?:"([^"]*)"|'([^']*)')/y;

const ENTITIES: Record<string, string> = { lt: "<", gt: ">", amp: "&", quot: '"', apos: "'" };

/** Decodes the five predefined entities and valid numeric character references. */
export function decodeRefs(s: string): string {
  return s.replace(/&(?:(lt|gt|amp|quot|apos)|#([0-9]+)|#x([0-9A-Fa-f]+));/g, (m, ent, dec, hex) => {
    if (ent !== undefined) return ENTITIES[ent];
    const v = dec !== undefined ? BigInt(dec) : BigInt("0x" + hex);
    if (v === 0n || v > 0x10ffffn || (v >= 0xd800n && v <= 0xdfffn)) return m;
    return String.fromCodePoint(Number(v));
  });
}

export function scan(x: string): Token[] {
  const out: Token[] = [];
  let i = 0;
  const n = x.length;
  const skipTo = (start: number, from: number, endMarker: string) => {
    const k = x.indexOf(endMarker, from);
    const end = k < 0 ? n : k + endMarker.length;
    out.push({ kind: "skip", start, end });
    return end;
  };
  while (i < n) {
    const lt = x.indexOf("<", i);
    if (lt < 0) break;
    i = lt;
    if (x.startsWith("<!--", i)) {
      i = skipTo(i, i + 4, "-->");
      continue;
    }
    if (x.startsWith("<![CDATA[", i)) {
      i = skipTo(i, i + 9, "]]>");
      continue;
    }
    if (x.startsWith("<?", i)) {
      i = skipTo(i, i + 2, "?>");
      continue;
    }
    if (x.startsWith("<!", i)) {
      i = skipTo(i, i + 2, ">");
      continue;
    }
    TAG_RE.lastIndex = i;
    const m = TAG_RE.exec(x);
    if (!m) {
      i++;
      continue;
    }
    const attrs = new Map<string, string>();
    const a = m[3];
    ATTR_RE.lastIndex = 0;
    let am: RegExpExecArray | null;
    while (ATTR_RE.lastIndex < a.length && (am = ATTR_RE.exec(a)) !== null) {
      if (!attrs.has(am[1])) attrs.set(am[1], decodeRefs(am[2] !== undefined ? am[2] : am[3]));
    }
    out.push({
      kind: "tag",
      start: i,
      end: i + m[0].length,
      isEnd: m[1] === "/",
      selfClosing: m[4] === "/",
      name: m[2],
      attrs,
    });
    i += m[0].length;
  }
  return out;
}

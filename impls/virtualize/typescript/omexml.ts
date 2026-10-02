// OME-XML tag scan (VIRTUALIZE.md §3.2).
import { MAX_SAFE, reject } from "./io.ts";

type Tag = {
  kind: "start" | "end";
  local: string;
  attrs: Map<string, string>; // first occurrence of each attribute name
  selfClosing: boolean;
  textAfter: string; // raw text from the end of this tag to the next '<'
};

const WS = new Set([" ", "\t", "\r", "\n"]);

function decodeEntities(s: string): string {
  return s.replace(/&(lt|gt|amp|quot|apos);|&#([0-9]+);|&#x([0-9a-fA-F]+);/g, (m, named, dec, hex) => {
    if (named) return { lt: "<", gt: ">", amp: "&", quot: '"', apos: "'" }[named as "lt"];
    const cp = dec !== undefined ? parseInt(dec, 10) : parseInt(hex, 16);
    if (!Number.isFinite(cp) || cp > 0x10ffff || (cp >= 0xd800 && cp <= 0xdfff)) return m;
    return String.fromCodePoint(cp);
  });
}

function localName(n: string): string {
  const i = n.lastIndexOf(":");
  return i < 0 ? n : n.slice(i + 1);
}

export function scanTags(x: string): Tag[] {
  const tags: Tag[] = [];
  const n = x.length;
  let i = x.indexOf("<");
  let last: Tag | null = null;
  const setText = (end: number, from: number) => {
    if (last) last.textAfter = x.slice(from, end);
  };
  let textFrom = 0;
  while (i >= 0 && i < n) {
    setText(i, textFrom);
    last = null;
    let next: number;
    if (x.startsWith("<!--", i)) {
      const j = x.indexOf("-->", i + 4);
      next = j < 0 ? n : j + 3;
    } else if (x.startsWith("<![CDATA[", i)) {
      const j = x.indexOf("]]>", i + 9);
      next = j < 0 ? n : j + 3;
    } else if (x.startsWith("<?", i)) {
      const j = x.indexOf("?>", i + 2);
      next = j < 0 ? n : j + 2;
    } else if (x.startsWith("<!", i)) {
      const j = x.indexOf(">", i + 2);
      next = j < 0 ? n : j + 1;
    } else if (x.startsWith("</", i)) {
      let j = i + 2;
      while (j < n && !WS.has(x[j]) && x[j] !== ">") j++;
      const name = x.slice(i + 2, j);
      const k = x.indexOf(">", j);
      next = k < 0 ? n : k + 1;
      const t: Tag = { kind: "end", local: localName(name), attrs: new Map(), selfClosing: false, textAfter: "" };
      tags.push(t);
      last = t;
    } else {
      let j = i + 1;
      while (j < n && !WS.has(x[j]) && x[j] !== "/" && x[j] !== ">" && x[j] !== "<") j++;
      const name = x.slice(i + 1, j);
      if (name === "" || j >= n || x[j] === "<") {
        // not a start tag
        next = i + 1;
      } else {
        const attrs = new Map<string, string>();
        let selfClosing = false;
        let p = j;
        for (;;) {
          while (p < n && WS.has(x[p])) p++;
          if (p >= n) break;
          if (x[p] === ">") { p++; break; }
          if (x[p] === "/") {
            if (x[p + 1] === ">") { selfClosing = true; p += 2; break; }
            p++;
            continue;
          }
          let q = p;
          while (q < n && !WS.has(x[q]) && x[q] !== "=" && x[q] !== ">" && !(x[q] === "/" && x[q + 1] === ">")) q++;
          const an = x.slice(p, q);
          p = q;
          while (p < n && WS.has(x[p])) p++;
          let val = "";
          if (x[p] === "=") {
            p++;
            while (p < n && WS.has(x[p])) p++;
            if (x[p] === '"' || x[p] === "'") {
              const qc = x[p];
              const e = x.indexOf(qc, p + 1);
              const end = e < 0 ? n : e;
              val = x.slice(p + 1, end);
              p = e < 0 ? n : e + 1;
            } else {
              let e = p;
              while (e < n && !WS.has(x[e]) && x[e] !== ">") e++;
              val = x.slice(p, e);
              p = e;
            }
          }
          if (an !== "" && !attrs.has(an)) attrs.set(an, decodeEntities(val));
          if (an === "" && p === q) p++; // guard against no progress
        }
        next = p;
        const t: Tag = { kind: "start", local: localName(name), attrs, selfClosing, textAfter: "" };
        tags.push(t);
        last = t;
      }
    }
    textFrom = next;
    i = next >= n ? -1 : x.indexOf("<", next);
    if (i < 0) setText(n, textFrom);
  }
  return tags;
}

export type TiffDataInfo = { attrs: Record<string, number | undefined>; uuidKey: string | null };

export type OmeInfo = {
  imageName: string | null;
  sizeZ?: number;
  sizeC?: number;
  sizeT?: number;
  dimensionOrder: string;
  physX: number | null;
  physY: number | null;
  physZ: number | null;
  unitX?: string;
  unitY?: string;
  unitZ?: string;
  tiffData: TiffDataInfo[];
};

function parseInt53(v: string | undefined, what: string, min: number): number | undefined {
  if (v === undefined) return undefined;
  const m = /^[ \t\r\n]*([0-9]+)[ \t\r\n]*$/.exec(v);
  if (!m) reject(`${what}="${v}" is not a decimal integer`);
  const n = Number(m[1]);
  if (n > MAX_SAFE) reject(`${what}="${v}" too large`);
  if (n < min) reject(`${what}="${v}" below ${min}`);
  return n;
}

const PHYS = /^[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?$/;

function parsePhys(v: string | undefined): number | null {
  if (v === undefined || !PHYS.test(v)) return null;
  const n = Number(v);
  return Number.isFinite(n) && n > 0 ? n : null;
}

/** Returns null if the text has no OME start tag. */
export function parseOme(x: string): OmeInfo | null {
  const tags = scanTags(x);
  if (!tags.some((t) => t.kind === "start" && t.local === "OME")) return null;
  const image = tags.find((t) => t.kind === "start" && t.local === "Image");
  const pi = tags.findIndex((t) => t.kind === "start" && t.local === "Pixels");
  const info: OmeInfo = {
    imageName: image?.attrs.get("Name") ?? null,
    dimensionOrder: "XYZCT",
    physX: null,
    physY: null,
    physZ: null,
    tiffData: [],
  };
  if (pi < 0) return info;
  const px = tags[pi];
  const a = px.attrs;
  info.sizeZ = parseInt53(a.get("SizeZ"), "SizeZ", 1);
  info.sizeC = parseInt53(a.get("SizeC"), "SizeC", 1);
  info.sizeT = parseInt53(a.get("SizeT"), "SizeT", 1);
  const dor = a.get("DimensionOrder");
  if (dor !== undefined) {
    if (!/^XY(ZCT|ZTC|CZT|CTZ|TZC|TCZ)$/.test(dor)) reject(`DimensionOrder "${dor}" invalid`);
    info.dimensionOrder = dor;
  }
  info.physX = parsePhys(a.get("PhysicalSizeX"));
  info.physY = parsePhys(a.get("PhysicalSizeY"));
  info.physZ = parsePhys(a.get("PhysicalSizeZ"));
  info.unitX = a.get("PhysicalSizeXUnit");
  info.unitY = a.get("PhysicalSizeYUnit");
  info.unitZ = a.get("PhysicalSizeZUnit");
  if (px.selfClosing) return info;
  let end = tags.findIndex((t, k) => k > pi && t.kind === "end" && t.local === "Pixels");
  if (end < 0) end = tags.length;
  for (let k = pi + 1; k < end; k++) {
    const t = tags[k];
    if (t.kind !== "start" || t.local !== "TiffData") continue;
    const ta = t.attrs;
    const attrs: Record<string, number | undefined> = {
      IFD: parseInt53(ta.get("IFD"), "IFD", 0),
      FirstZ: parseInt53(ta.get("FirstZ"), "FirstZ", 0),
      FirstC: parseInt53(ta.get("FirstC"), "FirstC", 0),
      FirstT: parseInt53(ta.get("FirstT"), "FirstT", 0),
      PlaneCount: parseInt53(ta.get("PlaneCount"), "PlaneCount", 1),
    };
    let uuidKey: string | null = null;
    if (!t.selfClosing) {
      for (let m = k + 1; m < end; m++) {
        const u = tags[m];
        if (u.local === "TiffData") break; // its end tag, or (malformed) the next TiffData
        if (u.kind === "start" && u.local === "UUID") {
          const fn = u.attrs.get("FileName");
          if (fn !== undefined) uuidKey = "F:" + fn;
          else uuidKey = "U:" + (u.selfClosing ? "" : decodeEntities(u.textAfter));
          break;
        }
      }
    }
    info.tiffData.push({ attrs, uuidKey });
  }
  return info;
}

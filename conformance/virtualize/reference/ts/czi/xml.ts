// The values the CZI layout reads from the metadata XML (spec/virtualize/czi.md §2.7).

import { DECIMAL, scan } from "../tiff/virtualize.ts";

export const MAX_XML = 2 ** 26; // the XML is read for the layout only when at most this many bytes
const INTEGER = /^[0-9]+$/;
const COLOR = /^#(?:[0-9A-Fa-f]{2})?([0-9A-Fa-f]{6})$/;
const NAMED: Record<string, string> = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'" };

/** XML character and entity references decoded, as the TIFF profile's scan decodes attributes. */
function decodeXml(s: string): string {
  return s.replace(/&(?:#x([0-9a-fA-F]+)|#([0-9]+)|(lt|gt|amp|quot|apos));/g, (ref, hex, dec, named) => {
    if (named) return NAMED[named];
    const c = hex ? parseInt(hex, 16) : Number(dec);
    if (c === 0 || (c >= 0xd800 && c <= 0xdfff) || c > 0x10ffff) return ref;
    return String.fromCodePoint(c);
  });
}

export interface Channel {
  name?: string;
  color?: string;
  bits?: number;
  low?: number;
  high?: number;
}

export interface XmlValues {
  px?: number; // metres
  py?: number;
  pz?: number;
  inc?: number; // seconds
  bits?: number;
  info: Channel[]; // Information/Image/Dimensions/Channels/Channel[k]
  display: Channel[]; // DisplaySetting/Channels/Channel[k]
  scenes: Map<number, string | undefined>; // S Index -> Name of its first Scene
}

export const emptyValues = (): XmlValues => ({ info: [], display: [], scenes: new Map() });

type Tag = ReturnType<typeof scan>["tags"][number];

/** The scan's tags as nested elements (spec/virtualize/czi.md §2.7). */
class Tree {
  readonly tags: Tag[];
  readonly skipped: [number, number][];
  readonly children: number[][] = []; // element -> its child elements
  readonly tag: number[] = []; // element -> its start tag's index
  root: number | undefined;

  readonly xml: string;

  constructor(xml: string) {
    this.xml = xml;
    ({ tags: this.tags, skipped: this.skipped } = scan(xml));
    const stack: number[] = [];
    for (const [ti, t] of this.tags.entries()) {
      if (t.closing) {
        for (let k = stack.length - 1; k >= 0; k--) {
          if (this.tags[this.tag[stack[k]]].name === t.name) {
            stack.length = k;
            break;
          }
        }
        continue;
      }
      const e = this.tag.length;
      this.tag.push(ti);
      this.children.push([]);
      if (stack.length) this.children[stack[stack.length - 1]].push(e);
      else if (this.root === undefined) this.root = e;
      if (!t.selfClosing) stack.push(e);
    }
  }

  name(e: number): string {
    return this.tags[this.tag[e]].name;
  }

  attr(e: number, key: string): string | undefined {
    const attrs = this.tags[this.tag[e]].attrs;
    return Object.hasOwn(attrs, key) ? attrs[key] : undefined;
  }

  child(e: number | undefined, name: string): number | undefined {
    if (e === undefined) return undefined;
    return this.children[e].find((c) => this.name(c) === name);
  }

  path(p: string): number | undefined {
    let e = this.root !== undefined && this.name(this.root) === "ImageDocument" ? this.root : undefined;
    for (const part of p.split("/")) e = this.child(e, part);
    return e;
  }

  text(e: number | undefined): string | undefined {
    if (e === undefined) return undefined;
    const ti = this.tag[e];
    const { end, selfClosing } = this.tags[ti];
    if (selfClosing) return "";
    const stop = ti + 1 < this.tags.length ? this.tags[ti + 1].start : this.xml.length;
    const pieces: string[] = [];
    let pos = end;
    // The first skipped section ending after `end` (they ascend, and do not overlap).
    let lo = 0, hi = this.skipped.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (this.skipped[mid][1] <= end) lo = mid + 1;
      else hi = mid;
    }
    for (let k = lo; k < this.skipped.length; k++) {
      const [a, b] = this.skipped[k];
      if (a >= stop) break;
      pieces.push(this.xml.slice(pos, a));
      pos = b;
    }
    pieces.push(this.xml.slice(pos, stop));
    return decodeXml(pieces.join("")).replace(/^[ \t\r\n]+|[ \t\r\n]+$/g, "");
  }

  childrenOf(e: number | undefined): number[] {
    return e === undefined ? [] : this.children[e];
  }
}

export function decimal(t: string | undefined): number | undefined {
  if (t === undefined || !DECIMAL.test(t)) return undefined;
  const v = Number(t);
  return Number.isFinite(v) ? v : undefined;
}

export function integer(t: string | undefined): number | undefined {
  if (t === undefined || !INTEGER.test(t) || t.replace(/^0+/, "").length > 16) return undefined;
  const v = Number(t);
  return v <= Number.MAX_SAFE_INTEGER ? v : undefined;
}

export function color(t: string | undefined): string | undefined {
  const m = t === undefined ? null : COLOR.exec(t);
  return m ? m[1].toUpperCase() : undefined;
}

const positive = (v: number | undefined) => (v !== undefined && v > 0 ? v : undefined);

export function readXmlValues(data: Uint8Array): XmlValues {
  const out = emptyValues();
  if (data.length > MAX_XML) return out;
  if (data[0] === 0xef && data[1] === 0xbb && data[2] === 0xbf) data = data.subarray(3);
  let xml: string;
  try {
    xml = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(data);
  } catch {
    return out;
  }
  const t = new Tree(xml);
  if (t.root === undefined || t.name(t.root) !== "ImageDocument") return out;
  const items = t.path("Metadata/Scaling/Items");
  for (const axis of ["x", "y", "z"] as const) {
    for (const c of t.childrenOf(items)) {
      if (t.name(c) === "Distance" && t.attr(c, "Id") === axis.toUpperCase()) {
        out[`p${axis}`] = positive(decimal(t.text(t.child(c, "Value"))));
        break;
      }
    }
  }
  out.inc = positive(decimal(t.text(t.path("Metadata/Information/Image/Dimensions/T/Positions/Interval/Increment"))));
  out.bits = integer(t.text(t.path("Metadata/Information/Image/ComponentBitCount")));
  for (const [parent, target, withBits] of [
    ["Metadata/Information/Image/Dimensions/Channels", out.info, true],
    ["Metadata/DisplaySetting/Channels", out.display, false],
  ] as const) {
    for (const c of t.childrenOf(t.path(parent))) {
      if (t.name(c) !== "Channel") continue;
      const ch: Channel = { name: t.attr(c, "Name"), color: color(t.text(t.child(c, "Color"))) };
      if (withBits) {
        ch.bits = integer(t.text(t.child(c, "ComponentBitCount")));
      } else {
        ch.low = decimal(t.text(t.child(c, "Low")));
        ch.high = decimal(t.text(t.child(c, "High")));
      }
      target.push(ch);
    }
  }
  for (const c of t.childrenOf(t.path("Metadata/Information/Image/Dimensions/S/Scenes"))) {
    if (t.name(c) !== "Scene") continue;
    const index = integer(t.attr(c, "Index"));
    if (index !== undefined && !out.scenes.has(index)) out.scenes.set(index, t.attr(c, "Name"));
  }
  return out;
}

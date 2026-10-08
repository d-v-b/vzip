// The HDF5 attributes of an Imaris file as JSON: the source metadata of the
// IMS convention (conventions/ims/README.md §5).

import { base64, decodeText, jsonNumber } from "../common.ts";
import { attributeValue, type Hdf5, ImsError, type Links } from "./hdf5.ts";

const MAX_VALUE_BYTES = 2 ** 26; // the most attribute data recorded per file

const bytesOf = (latin1: string) => Uint8Array.from(latin1, (c) => c.charCodeAt(0));
const untilNul = (b: Uint8Array) => {
  const i = b.indexOf(0);
  return i < 0 ? b : b.subarray(0, i);
};

/** An attribute's value by its datatype class: strings, numbers, or opaque. */
export function valueJson(cls: number, size: number, bits: number, data: Uint8Array): unknown {
  if (size === 0) return { class: cls, size, data: base64(data) };
  const count = data.length / size;
  if (cls === 3) {
    if (size === 1) return decodeText(untilNul(data));
    return Array.from({ length: count }, (_, i) => decodeText(untilNul(data.subarray(i * size, (i + 1) * size))));
  }
  const little = !(bits & 1);
  const view = new DataView(data.buffer, data.byteOffset, data.byteLength);
  const signed = Boolean(bits & 8);
  if (cls === 0 && [1, 2, 4, 8].includes(size)) {
    return Array.from({ length: count }, (_, i) => {
      const at = i * size;
      switch (size) {
        case 1: return signed ? view.getInt8(at) : view.getUint8(at);
        case 2: return signed ? view.getInt16(at, little) : view.getUint16(at, little);
        case 4: return signed ? view.getInt32(at, little) : view.getUint32(at, little);
        default: return jsonNumber(signed ? view.getBigInt64(at, little) : view.getBigUint64(at, little));
      }
    });
  }
  if (cls === 1 && (size === 4 || size === 8)) {
    return Array.from({ length: count }, (_, i) =>
      jsonNumber(size === 4 ? view.getFloat32(i * 4, little) : view.getFloat64(i * 8, little)));
  }
  return { class: cls, size, data: base64(data) };
}

/** Reads attributes for the source metadata, sharing one budget; a failure to
 * read an object or an attribute records null, and never rejects. */
class Source {
  private used = 0;
  private f: Hdf5;

  constructor(f: Hdf5) {
    this.f = f;
  }

  async attributes(at: number): Promise<Record<string, unknown> | null> {
    let raw: Map<string, Uint8Array>;
    try {
      raw = await this.f.attributes(at);
    } catch (e) {
      if (e instanceof ImsError) return null;
      throw e;
    }
    const out: Record<string, unknown> = {};
    for (const name of [...raw.keys()].sort()) {
      const key = decodeText(bytesOf(name));
      let value: ReturnType<typeof attributeValue>;
      try {
        value = attributeValue(raw.get(name)!);
      } catch (e) {
        if (!(e instanceof ImsError)) throw e;
        out[key] = null;
        continue;
      }
      const [datatype, data] = value;
      if (this.used + data.length > MAX_VALUE_BYTES) {
        out[key] = null;
        continue;
      }
      this.used += data.length;
      out[key] = valueJson(datatype.cls, datatype.size, datatype.bits, data);
    }
    return out;
  }

  /** The attributes of the group that a link leads to, or null. */
  async group(links: Links, name: string): Promise<Record<string, unknown> | null> {
    let at: number;
    try {
      at = await this.f.follow(links, name);
      await this.f.links(at); // it must be a group
    } catch (e) {
      if (e instanceof ImsError) return null;
      throw e;
    }
    return this.attributes(at);
  }
}

/** {root, DataSetInfo, DataSet}: the root group's attributes, those of each
 * group that DataSetInfo links to, and those of each channel group. */
export async function sourceJson(
  f: Hdf5,
  infoLinks: Links,
  channels: [string, number][],
): Promise<Record<string, unknown>> {
  const s = new Source(f);
  const root = await s.attributes(f.root);
  const info: Record<string, unknown> = {};
  for (const name of [...infoLinks.keys()].sort()) info[decodeText(bytesOf(name))] = await s.group(infoLinks, name);
  const data: Record<string, unknown> = {};
  for (const [path, at] of channels) data[path] = await s.attributes(at);
  return { root, DataSetInfo: info, DataSet: data };
}

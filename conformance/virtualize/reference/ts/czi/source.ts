// The CZI source metadata node (spec/virtualize/czi.md §5).

import {
  base64, blobChunks, type ByteReader, declare, familyPlans, gridChunks, jsonText, type Part, type Plan, stringifyJson,
} from "../../../../../js/src/virtualize/common.ts";
import { jsonSize } from "../nd2/source.ts";
import {
  type Attachment, dataOffset, type Directory, dv, equal, guid, LETTERS, type MetadataSegment, type Segment,
  type Subblocks,
} from "./segments.ts";

const MAX_COPIED = 2 ** 16; // the chunk limit of copied columns (spec/virtualize/czi.md §5.3)
const MAX_NODE = 2 ** 16; // vzip_source's S, past which the attachment list is an array (§5.6)
const MAX_EVENTS = 2 ** 20; // an event list is decoded up to this many events
const MAX_EVENT_BYTES = 2 ** 26; // and this many bytes
const MAX_CHUNK = 2 ** 24;
export const INDEX_PATH = "attachments/index";

const bytesOf = (a: ArrayBufferView) => new Uint8Array(a.buffer, a.byteOffset, a.byteLength);

function concat(parts: Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const p of parts) {
    out.set(p, at);
    at += p.length;
  }
  return out;
}

/** Values held in memory, copied, and cut as contiguous values with the chunk limit 2^16. */
export async function copied(
  path: string, dataType: string, shape: number[], item: number, data: Uint8Array, dims: string[],
): Promise<Plan> {
  const [chunkShape, chunks] = await gridChunks(0, shape, item, async (o, n) => data.slice(o, o + n), MAX_COPIED);
  const resolved = new Map<string, Uint8Array>();
  for (const [k, v] of chunks) {
    resolved.set(k, v instanceof Uint8Array ? v : concat(v.map((r: Part) =>
      r instanceof Uint8Array ? r : data.subarray(r[0], r[0] + r[1]))));
  }
  return { path, dataType, shape, chunkShape, dims, chunks: resolved };
}

function bytesPlan(path: string, offset: number, length: number, attributes?: Record<string, unknown>): Plan {
  const [size, chunks] = blobChunks(offset, length);
  return { path, dataType: "uint8", shape: [length], chunkShape: [size], dims: ["byte"], chunks, attributes };
}

/** Bytes held in memory as a 1-D uint8 array (conventions §7), each chunk copied. */
function copiedBytes(path: string, data: Uint8Array): Plan {
  const k = Math.ceil(data.length / MAX_CHUNK);
  const size = Math.ceil(data.length / k);
  const chunks = new Map<string, Uint8Array>();
  for (let i = 0; i < k; i++) {
    const chunk = new Uint8Array(size);
    chunk.set(data.subarray(i * size, (i + 1) * size));
    chunks.set(String(i), chunk);
  }
  return { path, dataType: "uint8", shape: [data.length], chunkShape: [size], dims: ["byte"], chunks };
}

/** The directory's columns (spec/virtualize/czi.md §5.3). */
export async function directoryPlans(d: Directory): Promise<Plan[]> {
  const n = d.count;
  if (n === 0) return [];
  const plans = [
    await copied("directory/pixel_type", "int32", [n], 4, bytesOf(d.pixelType), ["index"]),
    await copied("directory/compression", "int32", [n], 4, bytesOf(d.compression), ["index"]),
    await copied("directory/pyramid_type", "uint8", [n], 1, d.pyramidType, ["index"]),
  ];
  if (d.spare.some((b) => b !== 0)) plans.push(await copied("directory/spare", "uint8", [n, 5], 1, d.spare, ["index", "byte"]));
  for (const letter of LETTERS) {
    const c = d.columns.get(letter);
    if (c === undefined) continue;
    for (const [name, dataType, values] of [
      ["start", "int32", c.start], ["size", "int32", c.size], ["stored_size", "int32", c.stored],
      ["start_coordinate", "float32", c.coordinate], ["position", "int8", c.position],
    ] as const) {
      plans.push(await copied(`directory/${letter}/${name}`, dataType, [n], values.BYTES_PER_ELEMENT, bytesOf(values),
        ["index"]));
    }
  }
  return plans;
}

/** The subblock families (spec/virtualize/czi.md §5.4); `trailing` lists [i, offset, length]. */
export async function subblockPlans(
  read: ByteReader, d: Directory, s: Subblocks, unplaced: number[], trailing: [number, number, number][],
): Promise<Plan[]> {
  const n = d.count;
  const metadata: [number, number, number][] = [], attachment: [number, number, number][] = [];
  const data: [number, number, number][] = [], entry: [number, number, number][] = [];
  for (let i = 0; i < n; i++) {
    const o = d.filePosition[i] + 32;
    if (s.metadata[i]) metadata.push([i, o + s.length[i], s.metadata[i]]);
    if (s.attachment[i]) attachment.push([i, o + s.length[i] + s.metadata[i] + s.data[i], s.attachment[i]]);
    if (!s.agrees[i]) entry.push([i, o + 16, s.copyLength[i]]);
  }
  for (const i of unplaced) {
    if (s.data[i]) data.push([i, d.filePosition[i] + 32 + s.length[i] + s.metadata[i], s.data[i]]);
  }
  const plans: Plan[] = [];
  for (const [name, members] of [["metadata", metadata], ["attachment", attachment], ["data", data],
    ["trailing", trailing], ["entry", entry]] as const) {
    if (members.length) plans.push(...(await familyPlans(`subblocks/${name}`, members, read, n)));
  }
  return plans;
}

export function metadataPlans(m: MetadataSegment | undefined): Plan[] {
  if (m === undefined) return [];
  const plans: Plan[] = [];
  if (m.xml) plans.push(bytesPlan("metadata/xml", m.offset + 32 + 256, m.xml));
  if (m.attachment) plans.push(bytesPlan("metadata/attachment", m.offset + 32 + 256 + m.xml, m.attachment));
  return plans;
}

/** The events of an event list's data from byte 8: [offset in data, description
 * length], or undefined when they do not fill it exactly. */
function events(data: Uint8Array, count: number): [number, number][] | undefined {
  const out: [number, number][] = [];
  const v = dv(data);
  let pos = 8;
  for (let k = 0; k < count; k++) {
    if (pos + 20 > data.length) return undefined;
    const e = v.getInt32(pos, true), u = v.getInt32(pos + 16, true);
    if (u < 0 || e !== 20 + u || pos + 20 + u > data.length) return undefined;
    out.push([pos, u]);
    pos += 20 + u;
  }
  return pos === data.length ? out : undefined;
}

const own = (s: Record<string, unknown>) => declare({}, "czi", undefined, s);

/** A group of the source metadata node with source metadata: [path, attributes]. */
export type GroupPlan = [string, Record<string, unknown>];

/** The attachment list and the attachments' arrays (spec/virtualize/czi.md §5.6). */
export async function attachmentPlans(
  read: ByteReader, atts: Attachment[],
): Promise<[unknown[], Plan[], GroupPlan[]]> {
  const listed: unknown[] = [], plans: Plan[] = [], groups: GroupPlan[] = [];
  const latin = (b: Uint8Array) => String.fromCharCode(...b);
  for (const [k, a] of atts.entries()) {
    if (!a.a1) {
      listed.push({ entry: base64(a.entry) });
      continue;
    }
    const typeField = a.entry.subarray(40, 48);
    const nul = typeField.indexOf(0);
    const kind = latin(nul < 0 ? typeField : typeField.subarray(0, nul));
    const item: Record<string, unknown> = {
      name: jsonText(a.entry.subarray(48, 128)), content_file_type: jsonText(typeField),
      content_guid: guid(a.entry.subarray(24, 40)),
    };
    if (!equal(a.segmentEntry, a.entry)) item.segment_entry = base64(a.segmentEntry);
    const s = a.dataSize, at = dataOffset(a), path = `attachments/${k}`;
    let form = s ? "bytes" : "empty";
    if ((kind === "CZTIMS" || kind === "CZFOC") && s >= 8) {
      const v = dv(await read(at, 8));
      const size = v.getInt32(0, true), c = v.getInt32(4, true);
      if (s === 8 + 8 * c) {
        form = kind === "CZTIMS" ? "time_stamps" : "focus_positions";
        const [chunkShape, chunks] = await gridChunks(at + 8, [c], 8, read);
        plans.push({ path, dataType: "float64", shape: [c], chunkShape, dims: ["index"], chunks, attributes: own({ size }) });
      }
    } else if (kind === "CZEVL" && s >= 8 && s <= MAX_EVENT_BYTES) {
      const data = await read(at, s);
      const v = dv(data);
      const size = v.getInt32(0, true), c = v.getInt32(4, true);
      const evs = c >= 0 && c <= MAX_EVENTS ? events(data, c) : undefined;
      if (evs !== undefined) {
        form = "event_list";
        const times = concat(evs.map(([p]) => data.subarray(p + 4, p + 12)));
        const types = concat(evs.map(([p]) => data.subarray(p + 12, p + 16)));
        plans.push(await copied(`${path}/time`, "float64", [c], 8, times, ["index"]));
        plans.push(await copied(`${path}/type`, "int32", [c], 4, types, ["index"]));
        const described: [number, number, number][] = [];
        for (const [e, [p, u]] of evs.entries()) if (u > 0) described.push([e, at + p + 20, u]);
        if (described.length) plans.push(...(await familyPlans(`${path}/description`, described, read, c)));
        groups.push([path, own({ size })]);
      }
    }
    if (form === "bytes") plans.push(bytesPlan(path, at, s));
    item.form = form;
    listed.push(item);
  }
  return [listed, plans, groups];
}

/** vzip_source's S, and the index array when the list is past the budget. */
export function nodeMetadata(listed: unknown[]): [Record<string, unknown>, Plan[]] {
  if (listed.length === 0) return [{}, []];
  if (jsonSize({ attachments: listed }) <= MAX_NODE) return [{ attachments: listed }, []];
  const text = new TextEncoder().encode(stringifyJson(listed));
  return [{ attachments: INDEX_PATH }, [copiedBytes(INDEX_PATH, text)]];
}

/** The segments nothing references (spec/virtualize/czi.md §5.7). */
export async function segmentPlans(read: ByteReader, segments: Segment[]): Promise<Plan[]> {
  if (segments.length === 0) return [];
  const count = segments.length;
  const plans = [await copied("segments/id", "uint8", [count, 16], 1, concat(segments.map(([, id]) => id)),
    ["index", "byte"])];
  const members: [number, number, number][] = [];
  for (const [k, [o, , allocated, used]] of segments.entries()) {
    const n = Math.min(used || allocated, allocated);
    if (n) members.push([k, o + 32, n]);
  }
  if (members.length) plans.push(...(await familyPlans("segments/data", members, read, count)));
  return plans;
}

export function tailPlan(start: number, size: number): Plan[] {
  return start < size ? [bytesPlan("tail", start, size - start)] : [];
}

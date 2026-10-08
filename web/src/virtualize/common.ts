// What every profile of VIRTUALIZE.md shares: reading the input through range
// reads, rejecting it, and sizing reference payloads.

import type { Range, Source } from "../protobuf.ts";

/** The input is rejected for a reason no single profile owns (§1.2). */
export class ImageError extends Error {}

/** Reads `length` bytes at `offset`; must return exactly that many. */
export type ByteReader = (offset: number, length: number) => Promise<Uint8Array>;

/** Caches reads in aligned blocks, so nearby small reads share a request. */
export function blockReader(
  read: ByteReader,
  fileSize: number,
  blockSize = 1 << 16,
): ByteReader {
  const blocks = new Map<number, Promise<Uint8Array>>();
  const block = (i: number) => {
    let b = blocks.get(i);
    if (b === undefined) {
      const start = i * blockSize;
      b = read(start, Math.min(blockSize, fileSize - start));
      blocks.set(i, b);
    }
    return b;
  };
  return async (offset, length) => {
    if (offset < 0 || offset + length > fileSize) {
      throw new ImageError(`read of [${offset}, ${offset + length}) outside the ${fileSize}-byte file`);
    }
    const out = new Uint8Array(length);
    const first = Math.floor(offset / blockSize);
    const last = Math.floor((offset + Math.max(length, 1) - 1) / blockSize);
    const parts = await Promise.all(
      Array.from({ length: last - first + 1 }, (_, k) => block(first + k)),
    );
    for (const [k, data] of parts.entries()) {
      const start = (first + k) * blockSize;
      const a = Math.max(offset, start);
      const b = Math.min(offset + length, start + data.length);
      if (a < b) out.set(data.subarray(a - start, b - start), a - offset);
    }
    return out;
  };
}

// ---- reference payloads (§1.2)

/** The largest reference payload a vzip entry can carry. */
export const MAX_PAYLOAD = 65519;

function varintSize(v: number): number {
  let n = 1;
  while (v >= 128) {
    v = Math.floor(v / 128);
    n++;
  }
  return n;
}

/** A range of an output: [offset, length] of source 0, [source, offset,
 * length] of any source, or literal bytes. */
export type Part = [number, number] | [number, number, number] | Uint8Array;

function rangeSize(r: Part): number {
  if (r instanceof Uint8Array) return 1 + varintSize(r.length) + r.length;
  const [source, offset, length] = r.length === 3 ? r : [0, ...r];
  return [source, offset, length].reduce((n, v) => n + (v ? 1 + varintSize(v) : 0), 0);
}

/** The encoded size of a reference to `ranges` (§1.2). */
export function payloadSize(ranges: Part[]): number {
  if (ranges.length === 1) return rangeSize(ranges[0]);
  return ranges.reduce((n, range) => {
    const r = rangeSize(range);
    return n + 1 + varintSize(r) + r;
  }, 0);
}

/** The data sources of a file input's output (§1.2): byte strings shared by
 * many references, numbered from 1 (after the url source 0) in order of
 * first use. */
export class DataSources {
  readonly sources: Uint8Array[] = [];
  private readonly index = new Map<string, number>();

  /** A range of all of `value`, adding it as a source the first time it is used. */
  range(value: Uint8Array): [number, number, number] {
    const key = Array.from(value, (b) => String.fromCharCode(b)).join("");
    let i = this.index.get(key);
    if (i === undefined) {
      this.sources.push(value.slice());
      i = this.sources.length;
      this.index.set(key, i);
    }
    return [i, 0, value.length];
  }

  /** The output's source table: `url`, then the data sources. */
  table(url: string): Source[] {
    return [{ url }, ...this.sources.map((data) => ({ data }))];
  }
}

/** A Part as an archive Range. */
export function toRange(p: Part): Range {
  if (p instanceof Uint8Array) return { data: p };
  const [source, offset, length] = p.length === 3 ? p : [0, ...p];
  return { source, offset: BigInt(offset), length: BigInt(length) };
}

// ---- the virtualization conventions (conventions §2)

/** The key of the virtualization convention's property (conventions §2). */
export const CONVENTION_KEY = "vzip_virtualized";

export type Profile = "tiff" | "ndpi" | "nd2" | "dicom" | "nifti" | "ims" | "n5" | "zarr2" | "ome-zarr";

/** Each profile's convention: its fixed UUID, its current version and its name in the description. */
export const PROFILES: Record<Profile, [uuid: string, version: number, title: string]> = {
  tiff: ["48e9ac4e-1156-4a62-955e-20467d9c2700", 1, "TIFF"],
  ndpi: ["6cac71ef-dbb2-4acd-b60c-00389aa4238a", 1, "NDPI"],
  nd2: ["59612f14-e314-4207-ba00-8f422ba71490", 1, "ND2"],
  dicom: ["acf17198-e5a5-48d3-8187-22ec4bb40ea5", 1, "DICOM"],
  nifti: ["06e5809d-4d54-4b72-afd0-6bf61a7b4c85", 1, "NIfTI"],
  ims: ["5067a535-8261-4b25-a93c-1985ed333bde", 1, "IMS"],
  n5: ["ad5d4c39-c69e-48f7-a3ef-4cc8c607d416", 1, "N5"],
  zarr2: ["8e792619-d671-4687-ab51-752885dd3ee6", 1, "Zarr v2"],
  "ome-zarr": ["b74ea302-65bb-49ae-b81f-f9bb52cd4eed", 1, "OME-Zarr"],
};
export const UUIDS = new Set(Object.values(PROFILES).map(([uuid]) => uuid));

/** The Convention Metadata Object of a profile's convention. */
export function convention(profile: Profile): { [k: string]: string } {
  const [uuid, version, title] = PROFILES[profile];
  const tag = `virtualize-${profile}-v${version}`;
  return {
    uuid,
    schema_url: `https://raw.githubusercontent.com/d-v-b/vzip/refs/tags/${tag}/conventions/${profile}/schema.json`,
    spec_url: `https://github.com/d-v-b/vzip/blob/${tag}/conventions/${profile}/README.md`,
    name: CONVENTION_KEY,
    description: `The Zarr layout of a ${title} source virtualized by vzip, and the source's metadata`,
  };
}

/** A node's attributes (conventions §2): `attributes`, the members the target formats
 * define (such as `ome`), and the profile's convention when the node is the
 * root (`url`, the source URL, is given) or has source-specific metadata
 * (`own` is a nonempty object): its metadata object in `zarr_conventions`,
 * and the property `vzip_virtualized`, which holds `own` as its member named
 * after the profile. */
export function declare(
  attributes: { [k: string]: unknown },
  profile: Profile,
  url: string | undefined,
  own?: object,
): { [k: string]: unknown } {
  const value: { [k: string]: unknown } = url === undefined
    ? {}
    : { profile, version: PROFILES[profile][1], source: { url } };
  if (own !== undefined && Object.keys(own).length > 0) value[profile] = own;
  if (Object.keys(value).length === 0) return { ...attributes };
  return { ...attributes, zarr_conventions: [convention(profile)], [CONVENTION_KEY]: value };
}

/** A number as source metadata (conventions/README.md §6). */
export function jsonNumber(v: number | bigint): number | string {
  const max = BigInt(Number.MAX_SAFE_INTEGER);
  if (typeof v === "bigint") return v <= max && v >= -max ? Number(v) : v.toString();
  if (Number.isNaN(v)) return "NaN";
  if (!Number.isFinite(v)) return v > 0 ? "Infinity" : "-Infinity";
  return v;
}

/** Bytes as text: UTF-8 if valid, else ISO 8859-1 (conventions/README.md §6). */
export function decodeText(b: Uint8Array): string {
  try {
    return new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(b);
  } catch {
    let s = "";
    for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode(...b.subarray(i, i + 0x8000));
    return s;
  }
}

/** A fixed-size character field: its bytes up to the first NUL, as text. */
export function jsonText(b: Uint8Array): string {
  const nul = b.indexOf(0);
  return decodeText(nul < 0 ? b : b.subarray(0, nul));
}

export function base64(b: Uint8Array): string {
  let s = "";
  for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode(...b.subarray(i, i + 0x8000));
  return btoa(s);
}

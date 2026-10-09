// Virtualizing an image file by the profile its first bytes select
// (VIRTUALIZE.md §1.2): TIFF, NDPI, ND2, DICOM, NIfTI, IMS, CZI or a SAFE zip, or a
// store by the profile its root keys select (§1.4): N5, Zarr v2, OME-Zarr 0.4 or a
// SAFE directory. TIFF (but not NDPI), ND2 and CZI are virtualized by the Rust core
// (ir/, rust/vzip-ir); each other profile is in its own directory; store.ts holds the
// store machinery.

import { blockReader, type ByteReader, ImageError } from "./common.ts";
import { isDicom, virtualizeDicom } from "./dicom/virtualize.ts";
import { virtualizeIms } from "./ims/virtualize.ts";
import { type IrFormat, virtualizeIr } from "./ir/run.ts";
import { type RangeSource, readerSource } from "./ir/source.ts";
import { detectNdpi, virtualizeNdpi } from "./ndpi/virtualize.ts";
import { detectNifti, virtualizeNifti } from "./nifti/virtualize.ts";
import { virtualizeN5 } from "./n5/virtualize.ts";
import { declares04, virtualizeOmeZarr } from "./ome-zarr/virtualize.ts";
import { isZip, virtualizeSafeStore, virtualizeSafeZip } from "./safe/virtualize.ts";
import { chooseProfile, closeStore, objectUrl, type Store, storeArchive } from "./store.ts";
import { crc32c } from "../deflate.ts";
import { virtualizeZarr2 } from "./zarr2/virtualize.ts";
import type { ArchiveDesc } from "../writer.ts";

/** How a virtualizer pins its sources and checksums its ranges (VIRTUALIZE.md §1.2, §1.4). */
export interface PinOptions {
  /** The input file's strong ETag, if every response gave the same one (an `etag` pin). */
  etag?: () => string | undefined;
  /** Record the CRC-32C of every range of a url source (SPEC.md §5.2), reading its bytes. */
  checksums?: boolean;
}

/**
 * Records on every range of a url source the CRC-32C of its bytes (SPEC.md
 * §5.2), read by `read(url, offset, length)`.
 */
export async function addChecksums(
  desc: ArchiveDesc,
  read: (url: string, offset: number, length: number) => Promise<Uint8Array>,
): Promise<void> {
  for (const e of desc.entries) {
    if (!("ranges" in e)) continue;
    for (const r of e.ranges) {
      if ("data" in r || r.crc32c !== undefined) continue;
      const url = desc.sources[r.source].url;
      if (url !== undefined) r.crc32c = crc32c(await read(url, Number(r.offset), Number(r.length)));
    }
  }
}

const NOT_SUPPORTED = "not a TIFF, NDPI, ND2, DICOM, NIfTI, IMS, CZI or SAFE zip file";

/** True if `head` starts with a TIFF or BigTIFF header, in either byte order (§1.2). */
function isTiffMagic(head: Uint8Array): boolean {
  const magic = Array.from(head.subarray(0, 4), (b) => b.toString(16).padStart(2, "0")).join("");
  return ["49492a00", "4d4d002a", "49492b00", "4d4d002b"].includes(magic);
}

// The HDF5 signature, which starts Imaris IMS files (§1.2).
const HDF5 = [0x89, 0x48, 0x44, 0x46, 0x0d, 0x0a, 0x1a, 0x0a];

type ImageDesc = ArchiveDesc & { format: "tiff" | "ndpi" | "nd2" | "dicom" | "nifti" | "ims" | "czi" | "safe"; summary: object };

/** `exact`, when given, reads exactly the ranges asked for, past `read`'s block cache: the
 * SAFE profile's tile-part headers, scattered through a zip file, are read with it, and the
 * Rust core's requests (TIFF, ND2, CZI), which it plans itself.
 * Source 0 pins the file's size, and its ETag when `pins.etag` gives one (§1.2); with
 * `pins.checksums`, the ranges of source 0 carry the CRC-32C of their bytes. */
export async function virtualizeImage(
  url: string,
  read: ByteReader,
  fileSize: number,
  exact?: ByteReader,
  pins: PinOptions = {},
): Promise<ImageDesc> {
  return virtualizeWith(url, { read, size: fileSize, exact, source: readerSource(exact ?? read, fileSize) }, pins);
}

/** Virtualizes the file a range source reads, such as an http(s) object opened under the
 * reader policy by `openHttpSource` (ir/source.ts): the Rust core plans its requests
 * (several at once, multi-range ones where the server answers them), and the other
 * profiles read it through a block cache. Source 0 pins the size, and the ETag when
 * every response gave the same strong one (`pins.etag`, else the source's). */
export async function virtualizeSource(url: string, source: RangeSource, pins: PinOptions = {}): Promise<ImageDesc> {
  const exact: ByteReader = (o, n) => source.read(o, n);
  const read = blockReader(exact, source.size);
  const etag = pins.etag ?? (source.etag ? () => source.etag!() : undefined);
  return virtualizeWith(url, { read, size: source.size, exact, source }, { ...pins, ...(etag ? { etag } : {}) });
}

interface Input {
  read: ByteReader;
  size: number;
  exact?: ByteReader;
  source: RangeSource;
}

async function virtualizeWith(url: string, input: Input, pins: PinOptions): Promise<ImageDesc> {
  const desc = await virtualizeFile(url, input);
  const etag = pins.etag?.();
  desc.sources[0] = { ...desc.sources[0], size: BigInt(input.size), ...(etag !== undefined ? { etag } : {}) };
  if (pins.checksums) await addChecksums(desc, (_u, o, n) => (input.exact ?? input.read)(o, n));
  return desc;
}

/** The profile of the Rust core a file's first bytes select, if one (§1.2; NDPI, a TIFF, is not). */
export function irFormat(head: Uint8Array): IrFormat | undefined {
  if (isTiffMagic(head)) return "tiff";
  if (head.length >= 4 && head[0] === 0xda && head[1] === 0xce && head[2] === 0xbe && head[3] === 0x0a) return "nd2";
  if (head.length >= 16 && String.fromCharCode(...head.subarray(0, 16)) === "ZISRAWFILE\0\0\0\0\0\0") return "czi";
  return undefined;
}

async function virtualizeFile(url: string, { read, size: fileSize, exact, source }: Input): Promise<ImageDesc> {
  const head = await read(0, Math.min(552, fileSize));
  if (isDicom(head)) return { format: "dicom", ...(await virtualizeDicom(url, read, fileSize)) };
  if (isTiffMagic(head)) {
    const first = await detectNdpi(read, fileSize);
    if (first !== undefined) return { format: "ndpi", ...(await virtualizeNdpi(url, read, fileSize, first)) };
  }
  const ir = irFormat(head);
  if (ir === "tiff" || ir === "nd2") return virtualizeIr(ir, url, source);
  if (HDF5.every((b, i) => head[i] === b)) return { format: "ims", ...(await virtualizeIms(url, read, fileSize)) };
  if (detectNifti(head) !== undefined) return { format: "nifti", ...(await virtualizeNifti(url, read, fileSize)) };
  if (ir === "czi") return virtualizeIr(ir, url, source);
  if (isZip(head)) return { format: "safe", ...(await virtualizeSafeZip(url, read, fileSize, exact ?? read)) };
  throw new ImageError(NOT_SUPPORTED);
}

/** Is `url` a store input (§1.2): a URL whose path ends in `/`? */
export function isStoreUrl(url: string): boolean {
  return url.split(/[?#]/, 1)[0].endsWith("/");
}

/** Virtualizes a listed store by the profile its root keys select (§1.4). Every source
 * pins its object's size; with `pins.checksums`, every range carries the CRC-32C of its bytes. */
export async function virtualizeStore(
  store: Store,
  pins: PinOptions = {},
): Promise<ArchiveDesc & { format: "n5" | "zarr2" | "ome-zarr" | "safe"; summary: object }> {
  const desc = await virtualizeStoreProfile(store);
  if (pins.checksums) {
    const keys = new Map([...store.objects.keys()].map((k) => [objectUrl(store.url, k), k]));
    await addChecksums(desc, async (u, o, n) => {
      const key = keys.get(u)!;
      return store.readRange ? store.readRange(key, o, n) : (await store.read(key)).subarray(o, o + n);
    });
  }
  return desc;
}

async function virtualizeStoreProfile(
  store: Store,
): Promise<ArchiveDesc & { format: "n5" | "zarr2" | "ome-zarr" | "safe"; summary: object }> {
  try {
    const chosen = chooseProfile(store);
    if (chosen === "safe") return { format: "safe", ...(await virtualizeSafeStore(store)) };
    let format: "n5" | "zarr2" | "ome-zarr" = chosen;
    if (format === "zarr2" && (await declares04(store))) format = "ome-zarr";
    const result = format === "n5"
      ? await virtualizeN5(store)
      : format === "ome-zarr"
      ? await virtualizeOmeZarr(store)
      : await virtualizeZarr2(store);
    return { format, ...storeArchive(store.url, result), summary: result.summary };
  } finally {
    closeStore(store);
  }
}

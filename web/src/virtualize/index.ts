// Virtualizing an image file by the profile its first bytes select
// (VIRTUALIZE.md §1.2): TIFF, NDPI, ND2, DICOM, NIfTI or IMS, each in its own
// directory.

import { type ByteReader, ImageError } from "./common.ts";
import { isDicom, virtualizeDicom } from "./dicom/virtualize.ts";
import { virtualizeIms } from "./ims/virtualize.ts";
import { isNd2, virtualizeNd2 } from "./nd2/virtualize.ts";
import { detectNdpi, virtualizeNdpi } from "./ndpi/virtualize.ts";
import { detectNifti, virtualizeNifti } from "./nifti/virtualize.ts";
import { virtualizeTiff } from "./tiff/virtualize.ts";
import type { ArchiveDesc } from "../writer.ts";

const NOT_SUPPORTED = "not a TIFF, NDPI, ND2, DICOM, NIfTI or IMS file";

/** True if `head` starts with a TIFF or BigTIFF header, in either byte order (§1.2). */
function isTiffMagic(head: Uint8Array): boolean {
  const magic = Array.from(head.subarray(0, 4), (b) => b.toString(16).padStart(2, "0")).join("");
  return ["49492a00", "4d4d002a", "49492b00", "4d4d002b"].includes(magic);
}

// The HDF5 signature, which starts Imaris IMS files (§1.2).
const HDF5 = [0x89, 0x48, 0x44, 0x46, 0x0d, 0x0a, 0x1a, 0x0a];

export async function virtualizeImage(
  url: string,
  read: ByteReader,
  fileSize: number,
): Promise<ArchiveDesc & { format: "tiff" | "ndpi" | "nd2" | "dicom" | "nifti" | "ims"; summary: object }> {
  const head = await read(0, Math.min(552, fileSize));
  if (isDicom(head)) return { format: "dicom", ...(await virtualizeDicom(url, read, fileSize)) };
  if (isTiffMagic(head)) {
    const first = await detectNdpi(read, fileSize);
    if (first !== undefined) return { format: "ndpi", ...(await virtualizeNdpi(url, read, fileSize, first)) };
    return { format: "tiff", ...(await virtualizeTiff(url, read, fileSize)) };
  }
  if (isNd2(head)) return { format: "nd2", ...(await virtualizeNd2(url, read, fileSize)) };
  if (HDF5.every((b, i) => head[i] === b)) return { format: "ims", ...(await virtualizeIms(url, read, fileSize)) };
  if (detectNifti(head) !== undefined) return { format: "nifti", ...(await virtualizeNifti(url, read, fileSize)) };
  throw new ImageError(NOT_SUPPORTED);
}

// Virtualizing an image file by the profile its first bytes select
// (VIRTUALIZE.md): TIFF, NDPI or ND2, each in its own directory.

import { type ByteReader, ImageError } from "./common.ts";
import { isNd2, virtualizeNd2 } from "./nd2/virtualize.ts";
import { detectNdpi, virtualizeNdpi } from "./ndpi/virtualize.ts";
import { detectNifti, virtualizeNifti } from "./nifti/virtualize.ts";
import { virtualizeTiff } from "./tiff/virtualize.ts";
import type { ArchiveDesc } from "../writer.ts";

export async function virtualizeImage(
  url: string,
  read: ByteReader,
  fileSize: number,
): Promise<ArchiveDesc & { format: "tiff" | "ndpi" | "nd2" | "nifti"; summary: object }> {
  const head = await read(0, Math.min(552, fileSize));
  const order = String.fromCharCode(head[0], head[1]);
  if (order === "II" || order === "MM") {
    const first = await detectNdpi(read, fileSize);
    if (first !== undefined) return { format: "ndpi", ...(await virtualizeNdpi(url, read, fileSize, first)) };
    return { format: "tiff", ...(await virtualizeTiff(url, read, fileSize)) };
  }
  if (isNd2(head)) return { format: "nd2", ...(await virtualizeNd2(url, read, fileSize)) };
  if (detectNifti(head) !== undefined) return { format: "nifti", ...(await virtualizeNifti(url, read, fileSize)) };
  throw new ImageError("not a TIFF or ND2 file, nor a NIfTI file");
}

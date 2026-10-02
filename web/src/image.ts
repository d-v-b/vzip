// Virtualizing an image file by the profile its first bytes select
// (VIRTUALIZE.md): TIFF (including NDPI) or ND2.

import { isNd2, virtualizeNd2 } from "./nd2.ts";
import { detectNdpi, virtualizeNdpi } from "./ndpi.ts";
import type { ByteReader } from "./tiff.ts";
import { virtualizeTiff } from "./virtualize.ts";
import type { ArchiveDesc } from "./writer.ts";

export class ImageError extends Error {}

export async function virtualizeImage(
  url: string,
  read: ByteReader,
  fileSize: number,
): Promise<ArchiveDesc & { format: "tiff" | "ndpi" | "nd2"; summary: object }> {
  const head = await read(0, Math.min(8, fileSize));
  const order = String.fromCharCode(head[0], head[1]);
  if (order === "II" || order === "MM") {
    const first = await detectNdpi(read, fileSize);
    if (first !== undefined) return { format: "ndpi", ...(await virtualizeNdpi(url, read, fileSize, first)) };
    return { format: "tiff", ...(await virtualizeTiff(url, read, fileSize)) };
  }
  if (isNd2(head)) return { format: "nd2", ...(await virtualizeNd2(url, read, fileSize)) };
  throw new ImageError("not a TIFF or ND2 file");
}

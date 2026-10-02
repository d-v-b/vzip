// Virtualizing an image file by the profile its first bytes select
// (VIRTUALIZE.md): TIFF or ND2.

import { isNd2, virtualizeNd2 } from "./nd2.ts";
import type { ByteReader } from "./tiff.ts";
import { virtualizeTiff } from "./virtualize.ts";
import type { ArchiveDesc } from "./writer.ts";

export class ImageError extends Error {}

export async function virtualizeImage(
  url: string,
  read: ByteReader,
  fileSize: number,
): Promise<ArchiveDesc & { format: "tiff" | "nd2"; summary: object }> {
  const head = await read(0, Math.min(8, fileSize));
  const order = String.fromCharCode(head[0], head[1]);
  if (order === "II" || order === "MM") return { format: "tiff", ...(await virtualizeTiff(url, read, fileSize)) };
  if (isNd2(head)) return { format: "nd2", ...(await virtualizeNd2(url, read, fileSize)) };
  throw new ImageError("not a TIFF or ND2 file");
}

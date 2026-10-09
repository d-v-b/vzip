// CRC-32 and raw DEFLATE (RFC 1951) with the platform's compression streams.

const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();

export function crc32(data: Uint8Array): number {
  let c = 0xffffffff;
  for (const b of data) c = CRC_TABLE[(c ^ b) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

// CRC-32C (Castagnoli, RFC 3720 §B.4): the reflected polynomial 0x82F63B78.
const CRC32C_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0x82f63b78 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();

/** CRC-32C of `data`, the range checksum of spec §5.2. */
export function crc32c(data: Uint8Array): number {
  let c = 0xffffffff;
  for (let i = 0; i < data.length; i++) c = CRC32C_TABLE[(c ^ data[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

/** A DEFLATE stream inflates to more than the reader allows (spec §8.7 rule 6). */
export class InflateLimitError extends Error {}

async function transform(
  data: Uint8Array,
  stream: TransformStream<BufferSource, Uint8Array>,
): Promise<Uint8Array> {
  const out = new Response(
    new Blob([data as BlobPart]).stream().pipeThrough(stream),
  );
  return new Uint8Array(await out.arrayBuffer());
}

export function deflateRaw(data: Uint8Array): Promise<Uint8Array> {
  return transform(data, new CompressionStream("deflate-raw"));
}

/**
 * Inflates a raw DEFLATE stream; trailing data after its end is an error.
 * With `limit`, inflating stops as soon as the output passes `limit` bytes,
 * which throws InflateLimitError (spec §8.7 rule 6).
 */
export async function inflateRaw(data: Uint8Array, limit?: number): Promise<Uint8Array> {
  if (limit === undefined) return transform(data, new DecompressionStream("deflate-raw"));
  const reader = new Blob([data as BlobPart]).stream()
    .pipeThrough(new DecompressionStream("deflate-raw")).getReader();
  const chunks: Uint8Array[] = [];
  let n = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    n += value.length;
    if (n > limit) {
      await reader.cancel().catch(() => {});
      throw new InflateLimitError(`inflates to more than ${limit} bytes`);
    }
    chunks.push(value);
  }
  const out = new Uint8Array(n);
  let at = 0;
  for (const c of chunks) {
    out.set(c, at);
    at += c.length;
  }
  return out;
}

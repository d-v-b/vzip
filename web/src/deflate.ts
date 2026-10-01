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

/** Inflates a raw DEFLATE stream; trailing data after its end is an error. */
export function inflateRaw(data: Uint8Array): Promise<Uint8Array> {
  return transform(data, new DecompressionStream("deflate-raw"));
}

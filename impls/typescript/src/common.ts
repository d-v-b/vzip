// Shared helpers and constants.
import * as zlib from "node:zlib";

export const RESERVED_PREFIX = "__vz__/";
export const SOURCES_KEY = "__vz__/sources";
export const INDEX_KEY = "__vz__/index";
export const EXTRA_RANGE = 0x7a76;
export const EXTRA_CONCAT = 0x7a77;
export const U64_MAX = (1n << 64n) - 1n;
export const MAX_PAYLOAD = 65519;

/** Largest value window / inflated body this implementation will materialise (resource limit, §10). */
export const MEMORY_LIMIT = 1024 * 1024 * 1024;

const enc = new TextEncoder();
export function utf8(s: string): Uint8Array {
  return enc.encode(s);
}

export function compareBytes(a: Uint8Array, b: Uint8Array): number {
  return Buffer.compare(a, b);
}

export function compareKeys(a: string, b: string): number {
  return Buffer.compare(utf8(a), utf8(b));
}

export function startsWithBytes(s: Uint8Array, prefix: Uint8Array): boolean {
  if (prefix.length > s.length) return false;
  for (let i = 0; i < prefix.length; i++) if (s[i] !== prefix[i]) return false;
  return true;
}

/** True iff `s` is a well-formed Unicode string (no lone surrogates). */
export function isWellFormed(s: string): boolean {
  return (s as any).isWellFormed();
}

export function isHidden(key: string): boolean {
  return key.startsWith(RESERVED_PREFIX);
}

export function isFormatKey(key: string): boolean {
  return key === SOURCES_KEY || key === INDEX_KEY;
}

/** RFC 9110 strong entity tag: DQUOTE *etagc DQUOTE, etagc = %x21 / %x23-7E. */
export function isStrongEtag(s: string): boolean {
  return /^"[\x21\x23-\x7e]*"$/.test(s);
}

export class InflateError extends Error {}

/**
 * Inflates `body` as a single raw DEFLATE stream that must end exactly at the
 * end of `body`. If `expected` is given, the output must have that length.
 */
export function inflateClean(body: Uint8Array, expected: bigint | null, limit = MEMORY_LIMIT): Buffer {
  const maxOut = expected === null ? limit : Number(expected < BigInt(limit) ? expected : BigInt(limit)) + 1;
  let r: { buffer: Buffer; engine: { bytesWritten: number } };
  try {
    r = zlib.inflateRawSync(body, { info: true, maxOutputLength: Math.max(1, maxOut) }) as any;
  } catch (e) {
    throw new InflateError(`DEFLATE stream invalid: ${(e as Error).message}`);
  }
  if (r.engine.bytesWritten !== body.length)
    throw new InflateError(`DEFLATE stream ends at byte ${r.engine.bytesWritten} of ${body.length}`);
  if (expected !== null && BigInt(r.buffer.length) !== expected)
    throw new InflateError(`inflated to ${r.buffer.length} bytes, expected ${expected}`);
  return r.buffer;
}

export function hex(b: Uint8Array): string {
  return Buffer.from(b.buffer, b.byteOffset, b.byteLength).toString("hex");
}

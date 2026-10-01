export const SIG_LOCAL = 0x04034b50;
export const SIG_CENTRAL = 0x02014b50;
export const SIG_EOCD = 0x06054b50;
export const SIG_ZIP64_EOCD = 0x06064b50;
export const SIG_ZIP64_LOCATOR = 0x07064b50;

export const EXTRA_ZIP64 = 0x0001;
export const EXTRA_RANGE = 0x7a76;
export const EXTRA_CONCAT = 0x7a77;

export const RESERVED_PREFIX = "__vz__/";
export const SOURCES_KEY = "__vz__/sources";
export const INDEX_KEY = "__vz__/index";
export const MAGIC = "vzip/1";

export const MAX_PAYLOAD = 65519;

export function isFormatKey(k: string): boolean {
  return k === SOURCES_KEY || k === INDEX_KEY;
}

export function isHidden(k: string): boolean {
  return k.startsWith(RESERVED_PREFIX);
}

/** Compare two byte strings lexicographically (UTF-8 order). */
export function compareBytes(a: Uint8Array, b: Uint8Array): number {
  return Buffer.compare(a, b);
}

/**
 * @license
 * Copyright 2026 Google Inc.
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/**
 * @file Decoder for the protobuf messages of the vzip format (format
 * version 0, spec §5 and Appendix A). Decoding is strict, as §5.1 requires:
 * malformed input throws a `VzipMalformedError`.
 */

export class VzipMalformedError extends Error {}

const VARINT = 0;
const I64 = 1;
const LEN = 2;
const I32 = 5;

const MAX_UINT64 = (1n << 64n) - 1n;
const MAX_UINT32 = (1n << 32n) - 1n;

/** Largest payload a reader accepts (spec §4.3). */
export const MAX_PAYLOAD = 65519;

interface Field {
  number: number;
  wireType: number;
  // bigint for VARINT, bytes for LEN; undefined for skipped I64/I32 fields.
  value: bigint | Uint8Array | undefined;
}

function readVarint(buf: Uint8Array, pos: number): [bigint, number] {
  let result = 0n;
  for (let i = 0; i < 10; ++i) {
    if (pos >= buf.length) throw new VzipMalformedError("truncated varint");
    const b = buf[pos++];
    result |= BigInt(b & 0x7f) << BigInt(7 * i);
    if ((b & 0x80) === 0) {
      if (result > MAX_UINT64) {
        throw new VzipMalformedError("varint exceeds 2^64 - 1");
      }
      return [result, pos];
    }
  }
  throw new VzipMalformedError("varint longer than 10 bytes");
}

function* fields(buf: Uint8Array): Generator<Field> {
  let pos = 0;
  while (pos < buf.length) {
    let key: bigint;
    [key, pos] = readVarint(buf, pos);
    const number = Number(key >> 3n);
    const wireType = Number(key & 7n);
    if (number === 0 || number > (1 << 29) - 1) {
      throw new VzipMalformedError(`invalid field number ${number}`);
    }
    let value: bigint | Uint8Array | undefined;
    switch (wireType) {
      case VARINT:
        [value, pos] = readVarint(buf, pos);
        break;
      case LEN: {
        let n: bigint;
        [n, pos] = readVarint(buf, pos);
        if (BigInt(pos) + n > BigInt(buf.length)) {
          throw new VzipMalformedError(
            "length-delimited field runs past the end of the message",
          );
        }
        value = buf.subarray(pos, pos + Number(n));
        pos += Number(n);
        break;
      }
      case I64:
      case I32: {
        const n = wireType === I64 ? 8 : 4;
        if (pos + n > buf.length) {
          throw new VzipMalformedError("truncated fixed-width field");
        }
        pos += n;
        break;
      }
      default:
        throw new VzipMalformedError(`invalid wire type ${wireType}`);
    }
    yield { number, wireType, value };
  }
}

/** Yields the fields named in `schema` (field number -> wire type). */
function* knownFields(
  buf: Uint8Array,
  schema: Record<number, number>,
): Generator<Field> {
  for (const field of fields(buf)) {
    const expected = schema[field.number];
    if (expected === undefined) continue; // unknown fields are skipped
    if (field.wireType !== expected) {
      throw new VzipMalformedError(
        `field ${field.number} has wire type ${field.wireType}, expected ${expected}`,
      );
    }
    yield field;
  }
}

const utf8 = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });

export function decodeUtf8(bytes: Uint8Array): string {
  try {
    return utf8.decode(bytes);
  } catch {
    throw new VzipMalformedError("string field is not valid UTF-8");
  }
}

function uint32(v: bigint): number {
  if (v > MAX_UINT32) throw new VzipMalformedError("uint32 exceeds 2^32 - 1");
  return Number(v);
}

export interface VzipRange {
  source: number;
  // Byte offsets are kept as bigint: they can legally exceed 2^53.
  offset: bigint;
  length: bigint;
  data: Uint8Array | undefined;
}

export interface VzipConcat {
  parts: VzipRange[];
}

export type VzipReference = VzipRange[];

export function rangeSize(range: VzipRange): bigint {
  return range.data !== undefined ? BigInt(range.data.length) : range.length;
}

export function decodeRange(buf: Uint8Array): VzipRange {
  let source = 0;
  let offset = 0n;
  let length = 0n;
  let data: Uint8Array | undefined;
  for (const { number, value } of knownFields(buf, {
    1: VARINT,
    3: VARINT,
    4: VARINT,
    5: LEN,
  })) {
    switch (number) {
      case 1:
        source = uint32(value as bigint);
        break;
      case 3:
        offset = value as bigint;
        break;
      case 4:
        length = value as bigint;
        break;
      case 5:
        data = value as Uint8Array;
        break;
    }
  }
  if (data !== undefined && (source !== 0 || offset !== 0n || length !== 0n)) {
    throw new VzipMalformedError(
      "literal range with non-zero source/offset/length",
    );
  }
  if (offset + length > MAX_UINT64) {
    throw new VzipMalformedError("offset + length exceeds 2^64 - 1");
  }
  return { source, offset, length, data };
}

/**
 * Decodes a reference payload (spec §4.3): header ID 0x7A76 holds one
 * `Range`, 0x7A77 a `Concat`. Returns the list of ranges.
 */
export function decodeReference(
  headerId: number,
  payload: Uint8Array,
): VzipReference {
  if (payload.length > MAX_PAYLOAD) {
    throw new VzipMalformedError(
      `reference payload of ${payload.length} bytes exceeds ${MAX_PAYLOAD}`,
    );
  }
  if (headerId === 0x7a76) return [decodeRange(payload)];
  const parts: VzipRange[] = [];
  let total = 0n;
  for (const { value } of knownFields(payload, { 1: LEN })) {
    const range = decodeRange(value as Uint8Array);
    total += rangeSize(range);
    parts.push(range);
  }
  if (total > MAX_UINT64) {
    throw new VzipMalformedError("concat size exceeds 2^64 - 1");
  }
  return parts;
}

export type VzipSource =
  | { kind: "url"; url: string; pins: VzipPins }
  | { kind: "key"; key: string }
  | { kind: "data"; data: Uint8Array };

export interface VzipPins {
  size?: bigint;
  etag?: string;
  modifiedNotAfter?: bigint;
}

// RFC 9110 strong entity tag, ASCII only (spec §6.1).
const STRONG_ETAG = /^"[\x21\x23-\x7e]*"$/;

function decodeSource(buf: Uint8Array): VzipSource {
  let kind: { number: number; value: Uint8Array } | undefined;
  const pins: VzipPins = {};
  for (const { number, value } of knownFields(buf, {
    1: LEN,
    2: LEN,
    3: LEN,
    4: VARINT,
    5: LEN,
    6: VARINT,
  })) {
    switch (number) {
      case 1:
      case 2:
        decodeUtf8(value as Uint8Array); // every occurrence must be valid
      // falls through
      case 3:
        kind = { number, value: value as Uint8Array }; // oneof: last wins
        break;
      case 4:
        pins.size = value as bigint;
        break;
      case 5:
        pins.etag = decodeUtf8(value as Uint8Array);
        break;
      case 6: {
        const v = value as bigint;
        pins.modifiedNotAfter = v >= 1n << 63n ? v - (1n << 64n) : v;
        break;
      }
    }
  }
  if (kind === undefined) throw new VzipMalformedError("Source has no kind");
  const pinned =
    pins.size !== undefined ||
    pins.etag !== undefined ||
    pins.modifiedNotAfter !== undefined;
  if (kind.number === 1) {
    const url = decodeUtf8(kind.value);
    if (url === "") throw new VzipMalformedError("empty url source");
    if (pins.etag !== undefined && !STRONG_ETAG.test(pins.etag)) {
      throw new VzipMalformedError(
        `etag pin is not a strong entity tag: ${JSON.stringify(pins.etag)}`,
      );
    }
    return { kind: "url", url, pins };
  }
  if (pinned) {
    throw new VzipMalformedError("pins are only allowed on url sources");
  }
  if (kind.number === 2) {
    const key = decodeUtf8(kind.value);
    if (key === "") throw new VzipMalformedError("empty key source");
    return { kind: "key", key };
  }
  return { kind: "data", data: kind.value.slice() };
}

export function decodeSourceTable(buf: Uint8Array): VzipSource[] {
  return Array.from(knownFields(buf, { 1: LEN }), ({ value }) =>
    decodeSource(value as Uint8Array),
  );
}

export interface VzipPage {
  firstKey: string;
  offset: number;
  length: number;
}

export interface VzipPinned {
  key: string;
  dataOffset: number;
  size: number;
  csize: number;
  method: number;
}

function toNumber(v: bigint, what: string): number {
  if (v > BigInt(Number.MAX_SAFE_INTEGER)) {
    throw new VzipMalformedError(`${what} is too large for this reader`);
  }
  return Number(v);
}

export function decodeCdIndex(buf: Uint8Array): {
  pages: VzipPage[];
  pinned: VzipPinned[];
} {
  const pages: VzipPage[] = [];
  const pinned: VzipPinned[] = [];
  for (const { number, value } of knownFields(buf, { 1: LEN, 2: LEN })) {
    if (number === 1) {
      let firstKey = "";
      let offset = 0n;
      let length = 0n;
      for (const f of knownFields(value as Uint8Array, {
        1: LEN,
        2: VARINT,
        3: VARINT,
      })) {
        if (f.number === 1) firstKey = decodeUtf8(f.value as Uint8Array);
        else if (f.number === 2) offset = f.value as bigint;
        else length = f.value as bigint;
      }
      pages.push({
        firstKey,
        offset: toNumber(offset, "page offset"),
        length: toNumber(length, "page length"),
      });
    } else {
      let key = "";
      let dataOffset = 0n;
      let size = 0n;
      let csize = 0n;
      let method = 0;
      for (const f of knownFields(value as Uint8Array, {
        1: LEN,
        2: VARINT,
        3: VARINT,
        4: VARINT,
        5: VARINT,
      })) {
        switch (f.number) {
          case 1:
            key = decodeUtf8(f.value as Uint8Array);
            break;
          case 2:
            dataOffset = f.value as bigint;
            break;
          case 3:
            size = f.value as bigint;
            break;
          case 4:
            csize = f.value as bigint;
            break;
          case 5:
            method = uint32(f.value as bigint);
            break;
        }
      }
      pinned.push({
        key,
        dataOffset: toNumber(dataOffset, "pinned data_offset"),
        size: toNumber(size, "pinned size"),
        csize: toNumber(csize, "pinned csize"),
        method,
      });
    }
  }
  return { pages, pinned };
}

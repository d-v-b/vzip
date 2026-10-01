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
 * @file Key-value store over a vzip archive (format version 0): a ZIP file
 * whose entries are either bytes or references to byte ranges of other
 * objects. See the vzip specification, SPEC.md, revision 8.
 */

import { makeSimpleAsyncCache } from "#src/chunk_manager/generic_file_source.js";
import type { SharedKvStoreContextCounterpart } from "#src/kvstore/backend.js";
import { composeByteRangeRequest } from "#src/kvstore/byte_range/file_handle.js";
import type {
  ByteRangeRequest,
  DriverListOptions,
  DriverReadOptions,
  FileHandle,
  KvStore,
  ListEntry,
  ListResponse,
  ReadResponse,
  StatOptions,
  StatResponse,
} from "#src/kvstore/index.js";
import { readFileHandle } from "#src/kvstore/index.js";
import { encodePathForUrl } from "#src/kvstore/url.js";
import { HttpResolutionError, readHttpRange } from "#src/kvstore/vzip/http.js";
import type { VzipEntry, VzipMetadata } from "#src/kvstore/vzip/metadata.js";
import {
  INDEX_KEY,
  inflateEntry,
  pageForKey,
  pagesForPrefix,
  parseCentralDirectory,
  readVzipMetadata,
  RESERVED_PREFIX,
  SOURCES_KEY,
  utf8,
  VzipError,
} from "#src/kvstore/vzip/metadata.js";
import type { VzipRange, VzipReference } from "#src/kvstore/vzip/proto.js";
import { decodeReference, rangeSize } from "#src/kvstore/vzip/proto.js";
import { getScheme, resolveReference } from "#src/kvstore/vzip/uri.js";
import type { ProgressOptions } from "#src/util/progress_listener.js";
import { ProgressSpan } from "#src/util/progress_listener.js";
import { defaultStringCompare } from "#src/util/string.js";

/** Largest value this reader assembles in memory (spec §10). */
const MAX_REQUEST_BYTES = 1 << 30;

function makeReader(base: FileHandle) {
  return async (
    offset: number,
    length: number,
    options: Partial<ProgressOptions>,
  ) => {
    const response = await readFileHandle(base, {
      throwIfMissing: true,
      byteRange: { offset, length },
      strictByteRange: true,
      signal: options.signal,
      progressListener: options.progressListener,
    });
    return new Uint8Array(await response.response.arrayBuffer());
  };
}

function getMetadataCache(
  sharedKvStoreContext: SharedKvStoreContextCounterpart,
  base: FileHandle,
) {
  const url = base.getUrl();
  return makeSimpleAsyncCache(
    sharedKvStoreContext.chunkManager,
    `vzipMetadata:${url}`,
    {
      get: async (_unused: undefined, progressOptions) => {
        using _span = new ProgressSpan(progressOptions.progressListener, {
          message: `Opening vzip archive ${url}`,
        });
        const stat = await base.stat(progressOptions);
        if (stat?.totalSize === undefined) {
          throw new VzipError(
            "archive",
            `failed to determine the size of ${url}`,
          );
        }
        const data = await readVzipMetadata(
          makeReader(base),
          stat.totalSize,
          progressOptions,
        );
        return { data, size: data.sizeEstimate };
      },
    },
  );
}

/** Translates a kvstore byte range request into `[start, end)` of a value. */
function window(
  size: number,
  byteRange: ByteRangeRequest | undefined,
): { start: number; end: number } {
  const {
    outer: { offset, length },
  } = composeByteRangeRequest({ offset: 0, length: size }, byteRange);
  return { start: offset, end: offset + length };
}

function toSafeNumber(v: bigint, what: string): number {
  if (v > BigInt(Number.MAX_SAFE_INTEGER)) {
    throw new VzipError("request", `${what} is too large for this reader`);
  }
  return Number(v);
}

// Shared by every store on the same archive (Neuroglancer opens one per
// array), and released with the cached metadata.
const pageCache = new WeakMap<
  VzipMetadata,
  Map<number, Promise<Map<string, VzipEntry>>>
>();
const referenceCache = new WeakMap<VzipEntry, VzipReference>();

export class VzipKvStore implements KvStore {
  constructor(
    public sharedKvStoreContext: SharedKvStoreContextCounterpart,
    public base: FileHandle,
  ) {}

  private metadata: VzipMetadata | undefined;

  getUrl(key: string) {
    return this.base.getUrl() + `|vzip:${encodePathForUrl(key)}`;
  }

  get supportsOffsetReads() {
    return true;
  }
  get supportsSuffixReads() {
    return true;
  }

  private async getMetadata(options: Partial<ProgressOptions>) {
    let { metadata } = this;
    if (metadata === undefined) {
      const cache = getMetadataCache(this.sharedKvStoreContext, this.base);
      try {
        metadata = this.metadata = await cache.get(undefined, options);
      } finally {
        cache.dispose();
      }
    }
    return metadata;
  }

  private loadPage(
    metadata: VzipMetadata,
    index: number,
    options: Partial<ProgressOptions>,
  ): Promise<Map<string, VzipEntry>> {
    // Pages of the central directory, loaded on demand (paged archives).
    let loaded = pageCache.get(metadata);
    if (loaded === undefined) {
      loaded = new Map();
      pageCache.set(metadata, loaded);
    }
    let page = loaded.get(index);
    if (page === undefined) {
      const { cdOffset, pages, firstKeys } = metadata.paged!;
      const { offset, length } = pages[index];
      page = (async () => {
        const bytes = await makeReader(this.base)(
          cdOffset + offset,
          length,
          options,
        );
        let parsed: VzipEntry[];
        try {
          parsed = parseCentralDirectory(bytes);
        } catch (e) {
          throw new VzipError(
            "entry",
            `page ${index} cannot be parsed: ${(e as Error).message}`,
          );
        }
        const entries = new Map<string, VzipEntry>();
        for (const entry of parsed) {
          // A record counts only if lookup would find it on this page.
          if (pageForKey(firstKeys, utf8(entry.name)) !== index) continue;
          if (!entries.has(entry.name)) entries.set(entry.name, entry);
        }
        return entries;
      })();
      loaded.set(index, page);
      page.catch(() => loaded.delete(index));
    }
    return page;
  }

  /** Looks up a key's entry (spec §7.2), including hidden keys. */
  private async lookup(
    key: string,
    options: Partial<ProgressOptions>,
  ): Promise<VzipEntry | undefined> {
    const metadata = await this.getMetadata(options);
    const known = metadata.entries.get(key);
    if (known !== undefined || metadata.paged === undefined) return known;
    if (key === INDEX_KEY || key === SOURCES_KEY) return undefined;
    const index = pageForKey(metadata.paged.firstKeys, utf8(key));
    if (index < 0) return undefined;
    return (await this.loadPage(metadata, index, options)).get(key);
  }

  /** The entry for `key` in the resolved view, or undefined if missing. */
  private async findEntry(key: string, options: Partial<ProgressOptions>) {
    if (key.startsWith(RESERVED_PREFIX)) return undefined; // hidden
    const entry = await this.lookup(key, options);
    if (entry?.error !== undefined) {
      throw new VzipError("entry", `${JSON.stringify(key)}: ${entry.error}`);
    }
    return entry;
  }

  private getReference(entry: VzipEntry): VzipReference {
    let reference = referenceCache.get(entry);
    if (reference === undefined) {
      try {
        reference = decodeReference(
          entry.reference!.headerId,
          entry.reference!.payload,
        );
      } catch (e) {
        throw new VzipError(
          "payload",
          `${JSON.stringify(entry.name)}: ${(e as Error).message}`,
        );
      }
      referenceCache.set(entry, reference);
    }
    return reference;
  }

  private static valueSize(reference: VzipReference): bigint {
    let size = 0n;
    for (const range of reference) size += rangeSize(range);
    return size;
  }

  /** The (inflated) body of a bytes entry, or a window of a STORED one. */
  private async readBody(
    entry: VzipEntry,
    metadata: VzipMetadata,
    start: number,
    end: number,
    options: Partial<ProgressOptions>,
  ): Promise<Uint8Array<ArrayBuffer>> {
    const name = JSON.stringify(entry.name);
    if (entry.bodyOffset + entry.csize > metadata.fileSize) {
      throw new VzipError("body", `${name}: body lies outside the file`);
    }
    if (entry.method === 0) {
      if (entry.csize !== entry.size) {
        throw new VzipError(
          "body",
          `${name}: STORED entry with sizes ${entry.csize} != ${entry.size}`,
        );
      }
      return makeReader(this.base)(
        entry.bodyOffset + start,
        end - start,
        options,
      );
    }
    const raw = await makeReader(this.base)(
      entry.bodyOffset,
      entry.csize,
      options,
    );
    let body: Uint8Array<ArrayBuffer>;
    try {
      body = await inflateEntry(raw, options.signal);
    } catch {
      options.signal?.throwIfAborted();
      throw new VzipError("body", `${name}: body does not inflate cleanly`);
    }
    if (body.length !== entry.size) {
      throw new VzipError(
        "body",
        `${name}: inflates to ${body.length} bytes, record says ${entry.size}`,
      );
    }
    return body.subarray(start, end);
  }

  /** Bytes `[start, end)` of the source value of `range` (spec §6, §8.3). */
  private async readSource(
    metadata: VzipMetadata,
    range: VzipRange,
    start: number,
    end: number,
    options: Partial<ProgressOptions>,
  ): Promise<Uint8Array> {
    const source = metadata.sources[range.source];
    const resolution = (message: string): never => {
      throw new VzipError("resolution", `source ${range.source}: ${message}`);
    };
    if (source.kind === "data") {
      if (end > source.data.length) {
        resolution(`data source is shorter than ${end} bytes`);
      }
      return source.data.subarray(start, end);
    }
    if (source.kind === "key") {
      const { key } = source;
      if (key === SOURCES_KEY || key === INDEX_KEY) {
        resolution(`key source ${JSON.stringify(key)} names a format entry`);
      }
      const entry = await this.lookup(key, options).catch((e) =>
        resolution((e as Error).message),
      );
      if (entry === undefined)
        resolution(`key source ${JSON.stringify(key)} is missing`);
      if (entry!.error !== undefined) {
        resolution(`key source ${JSON.stringify(key)} has an entry error`);
      }
      if (entry!.reference !== undefined) {
        resolution(`key source ${JSON.stringify(key)} is a reference entry`);
      }
      if (end > entry!.size) {
        resolution(
          `key source ${JSON.stringify(key)} is shorter than ${end} bytes`,
        );
      }
      try {
        return await this.readBody(entry!, metadata, start, end, options);
      } catch (e) {
        options.signal?.throwIfAborted();
        return resolution((e as Error).message);
      }
    }
    // url source: resolved against the archive's own URL (spec §6).
    const baseUrl = this.base.getUrl();
    let url: string;
    try {
      url = resolveReference(baseUrl, source.url);
    } catch (e) {
      return resolution((e as Error).message);
    }
    const scheme = getScheme(url);
    if (scheme === "http" || scheme === "https") {
      try {
        const { data, size } = await readHttpRange(
          url,
          start,
          end,
          source.pins,
          options.signal,
        );
        if (
          source.pins.size !== undefined &&
          BigInt(size ?? -1) !== source.pins.size
        ) {
          resolution(`size pin ${source.pins.size} != ${size ?? "unknown"}`);
        }
        return data;
      } catch (e) {
        if (e instanceof VzipError) throw e;
        options.signal?.throwIfAborted();
        if (e instanceof HttpResolutionError) resolution(e.message);
        throw e;
      }
    }
    // Other schemes (gs:, s3:, ...) go through neuroglancer's kvstores,
    // which cannot check pins: pins fail closed (spec §6.1).
    const { pins } = source;
    if (
      pins.size !== undefined ||
      pins.etag !== undefined ||
      pins.modifiedNotAfter !== undefined
    ) {
      resolution(`pins cannot be checked for ${scheme}: URLs`);
    }
    try {
      const handle =
        this.sharedKvStoreContext.kvStoreContext.getFileHandle(url);
      const response = await readFileHandle(handle, {
        throwIfMissing: true,
        byteRange: { offset: start, length: end - start },
        strictByteRange: true,
        signal: options.signal,
        progressListener: options.progressListener,
      });
      return new Uint8Array(await response.response.arrayBuffer());
    } catch (e) {
      options.signal?.throwIfAborted();
      return resolution(`${url}: ${(e as Error).message}`);
    }
  }

  /** Bytes `[start, end)` of a reference's value (spec §8.3). */
  private async readReference(
    metadata: VzipMetadata,
    reference: VzipReference,
    start: number,
    end: number,
    options: Partial<ProgressOptions>,
  ): Promise<Uint8Array<ArrayBuffer>> {
    // Payload errors cover every range, even ones outside the window.
    for (const range of reference) {
      if (range.data === undefined && range.source >= metadata.sources.length) {
        throw new VzipError(
          "payload",
          `range uses source ${range.source}; the table has ${metadata.sources.length}`,
        );
      }
    }
    const pieces: Promise<Uint8Array>[] = [];
    let pos = 0;
    for (const range of reference) {
      const size = toSafeNumber(rangeSize(range), "range size");
      const lo = Math.max(start, pos);
      const hi = Math.min(end, pos + size);
      if (lo < hi) {
        if (range.data !== undefined) {
          pieces.push(Promise.resolve(range.data.subarray(lo - pos, hi - pos)));
        } else {
          const offset = toSafeNumber(range.offset, "range offset");
          pieces.push(
            this.readSource(
              metadata,
              range,
              offset + lo - pos,
              offset + hi - pos,
              options,
            ),
          );
        }
      }
      pos += size;
    }
    const parts = await Promise.all(pieces);
    const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
    let o = 0;
    for (const p of parts) {
      out.set(p, o);
      o += p.length;
    }
    return out;
  }

  async stat(
    key: string,
    options: StatOptions,
  ): Promise<StatResponse | undefined> {
    const entry = await this.findEntry(key, options);
    if (entry === undefined) return undefined;
    if (entry.reference === undefined) return { totalSize: entry.size };
    const size = VzipKvStore.valueSize(this.getReference(entry));
    return { totalSize: toSafeNumber(size, "value size") };
  }

  async read(
    key: string,
    options: DriverReadOptions,
  ): Promise<ReadResponse | undefined> {
    const metadata = await this.getMetadata(options);
    const entry = await this.findEntry(key, options);
    if (entry === undefined) return undefined;
    let size: number;
    let data: Uint8Array<ArrayBuffer>;
    let start: number;
    let end: number;
    if (entry.reference === undefined) {
      size = entry.size;
      ({ start, end } = window(size, options.byteRange));
      data = await this.readBody(entry, metadata, start, end, options);
    } else {
      const reference = this.getReference(entry);
      size = toSafeNumber(VzipKvStore.valueSize(reference), "value size");
      ({ start, end } = window(size, options.byteRange));
      if (end - start > MAX_REQUEST_BYTES) {
        throw new VzipError(
          "request",
          `request of ${end - start} bytes exceeds the limit`,
        );
      }
      data = await this.readReference(metadata, reference, start, end, options);
    }
    return {
      response: new Response(data),
      offset: start,
      length: end - start,
      totalSize: size,
    };
  }

  async list(
    prefix: string,
    options: DriverListOptions,
  ): Promise<ListResponse> {
    const metadata = await this.getMetadata(options);
    const keys = new Set<string>();
    const consider = (name: string) => {
      if (name.startsWith(prefix) && !name.startsWith(RESERVED_PREFIX))
        keys.add(name);
    };
    for (const name of metadata.entries.keys()) consider(name);
    if (metadata.paged !== undefined) {
      const indices = pagesForPrefix(metadata.paged.firstKeys, utf8(prefix));
      const pages = await Promise.all(
        indices.map((i) => this.loadPage(metadata, i, options)),
      );
      for (const page of pages) for (const name of page.keys()) consider(name);
    }
    const sorted = Array.from(keys).sort(defaultStringCompare);
    const entries: ListEntry[] = [];
    const directories = new Set<string>();
    for (const name of sorted) {
      const i = name.indexOf("/", prefix.length);
      if (i === -1) entries.push({ key: name });
      else directories.add(name.substring(0, i));
    }
    return { entries, directories: Array.from(directories) };
  }
}

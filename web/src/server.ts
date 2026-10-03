// Serves the values of vzip archives as plain HTTP, so that any Zarr reader
// can read them. Archives are either virtualized TIFFs or existing .vzip
// files, named by URL:
//
//   <prefix>image/<id>/<key>     the image file at decodeId(id), or the N5 or
//                                Zarr v2 store if that URL ends in "/",
//                                virtualized by VIRTUALIZE.md
//   <prefix>tiff/<id>/<key>      the same, for TIFF files only
//   <prefix>archive/<id>/<key>   the .vzip archive at decodeId(id)
//   .../__vz__/archive.vzip      the archive itself (keys under __vz__/ are
//                                hidden, so no key can collide with it)
//
// `id` is the base64url encoding of the URL. Used by the service worker
// (sw.ts); kept free of service worker APIs so it can run under Node.

import { Archive, type RangeFetcher, VzipError } from "./archive.ts";
import { HttpResolutionError, openHttpFile, readHttpRange } from "./http.ts";
import { blockReader, ImageError } from "./virtualize/common.ts";
import { isStoreUrl, virtualizeImage, virtualizeStore } from "./virtualize/index.ts";
import { N5Error } from "./virtualize/n5/virtualize.ts";
import { openHttpStore, StoreError, StoreLimitError, StoreReadError } from "./virtualize/store.ts";
import { Zarr2Error } from "./virtualize/zarr2/virtualize.ts";
import { DicomError } from "./virtualize/dicom/virtualize.ts";
import { ImsError } from "./virtualize/ims/virtualize.ts";
import { LVError } from "./virtualize/nd2/lv.ts";
import { Nd2Error } from "./virtualize/nd2/virtualize.ts";
import { NiftiError } from "./virtualize/nifti/virtualize.ts";
import { TiffError } from "./virtualize/tiff/ifd.ts";
import { virtualizeTiff } from "./virtualize/tiff/virtualize.ts";
import { writeVzip } from "./writer.ts";

export const ARCHIVE_KEY = "__vz__/archive.vzip";

/** Marks every response of the handler, so pages can tell it from the network's. */
export const WORKER_HEADER = "X-Vzip-Worker";

export function encodeId(url: string): string {
  let s = "";
  for (const b of new TextEncoder().encode(url)) s += String.fromCharCode(b);
  return btoa(s).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

export function decodeId(id: string): string {
  if (!/^[A-Za-z0-9_-]+$/.test(id)) throw new Error(`invalid id ${id}`);
  const s = atob(id.replaceAll("-", "+").replaceAll("_", "/"));
  return new TextDecoder("utf-8", { fatal: true }).decode(
    Uint8Array.from(s, (c) => c.charCodeAt(0)),
  );
}

export interface HandlerOptions {
  /** Absolute URL that every served path starts with, ending in "/". */
  prefix: string;
  openFile?: typeof openHttpFile;
  /** Lists and reads store inputs (§1.5); defaults to `fetch`. */
  fetchStore?: (url: string, init?: RequestInit) => Promise<Response>;
  fetchRange?: RangeFetcher;
  fetchArchive?: (url: string) => Promise<Uint8Array>;
}

interface Opened {
  archive: Archive;
  filename: string;
}

function response(status: number, body: string | Uint8Array | null, headers: Record<string, string> = {}) {
  return new Response(body as BodyInit | null, {
    status,
    headers: {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Expose-Headers": `Content-Range, Content-Length, ${WORKER_HEADER}`,
      [WORKER_HEADER]: "1",
      ...(typeof body === "string" ? { "Content-Type": "text/plain; charset=utf-8" } : {}),
      ...headers,
    },
  });
}

/** Parses a single-range `Range` header; undefined means "serve it all". */
function parseRange(header: string | null, size: number): { start: number; end: number } | "unsatisfiable" | undefined {
  const m = header?.match(/^bytes=(\d*)-(\d*)$/);
  if (!m || (m[1] === "" && m[2] === "")) return undefined;
  if (m[1] === "") {
    const n = Number(m[2]);
    if (n === 0) return "unsatisfiable";
    return { start: Math.max(0, size - n), end: size };
  }
  const start = Number(m[1]);
  const end = m[2] === "" ? size : Math.min(size, Number(m[2]) + 1);
  if (start >= size || (m[2] !== "" && Number(m[2]) < start)) return "unsatisfiable";
  return { start, end };
}

export function makeHandler(options: HandlerOptions) {
  const {
    prefix,
    openFile = openHttpFile,
    fetchStore = (u, i) => fetch(u, i),
    fetchRange = (url, start, end, pins) => readHttpRange(url, start, end, pins),
    fetchArchive = async (url) => {
      const r = await fetch(url);
      if (!r.ok) throw new HttpResolutionError(`${url}: HTTP ${r.status}`);
      return new Uint8Array(await r.arrayBuffer());
    },
  } = options;
  const opened = new Map<string, Promise<Opened>>();

  function open(kind: string, url: string): Promise<Opened> {
    const name = `${kind}/${url}`;
    let p = opened.get(name);
    if (p === undefined) {
      p = (async () => {
        const base = url.split(/[?#]/, 1)[0];
        const trimmed = base.endsWith("/") ? base.slice(0, -1) : base;
        const stem = decodeURIComponent(trimmed.slice(trimmed.lastIndexOf("/") + 1)) || "archive";
        if (kind === "archive") {
          return { archive: await Archive.open(await fetchArchive(url), url, fetchRange), filename: stem };
        }
        let virtual;
        if (kind === "image" && isStoreUrl(url)) {
          virtual = await virtualizeStore(await openHttpStore(url, { fetch: fetchStore }));
        } else {
          const file = await openFile(url);
          const read = blockReader(file.read, file.size);
          virtual = kind === "tiff"
            ? await virtualizeTiff(url, read, file.size)
            : await virtualizeImage(url, read, file.size);
        }
        const bytes = await writeVzip(virtual);
        return {
          archive: await Archive.open(bytes, url, fetchRange),
          filename: `${stem.replace(/\.(ome\.tiff?|tiff?|nd2|n5|zarr)$/i, "")}.vzip`,
        };
      })();
      opened.set(name, p);
      p.catch(() => opened.delete(name));
    }
    return p;
  }

  return async function handle(request: Request): Promise<Response> {
    if (!request.url.startsWith(prefix)) return response(404, "not found");
    if (request.method !== "GET" && request.method !== "HEAD") {
      return response(405, "method not allowed", { Allow: "GET, HEAD" });
    }
    const path = new URL(request.url).pathname.slice(new URL(prefix).pathname.length);
    const m = path.match(/^(image|tiff|archive)\/([^/]+)\/(.*)$/);
    if (m === null) return response(404, "not found");
    const [, kind, id] = m;
    let key: string;
    let url: string;
    try {
      key = m[3].split("/").map(decodeURIComponent).join("/");
      url = new URL(decodeId(id)).href;
    } catch {
      return response(400, "invalid URL");
    }
    const head = request.method === "HEAD";
    try {
      const { archive, filename } = await open(kind, url);
      if (key === ARCHIVE_KEY) {
        return response(200, head ? null : archive.bytes, {
          "Content-Type": "application/zip",
          "Content-Length": String(archive.bytes.length),
          "Content-Disposition": `attachment; filename="${filename.replaceAll('"', "")}"`,
        });
      }
      const entry = key === "" || key.endsWith("/") ? undefined : archive.lookup(key);
      if (entry === undefined) return response(404, `no key ${key}`);
      const size = archive.size(entry);
      const type = key.endsWith(".json") ? "application/json" : key.endsWith(".xml") ? "application/xml" : "application/octet-stream";
      const range = parseRange(request.headers.get("Range"), size);
      if (range === "unsatisfiable") {
        return response(416, "range not satisfiable", { "Content-Range": `bytes */${size}` });
      }
      const headers = { "Content-Type": type, "Accept-Ranges": "bytes" };
      if (range === undefined) {
        const body = head ? null : await archive.read(key);
        return response(200, body!, { ...headers, "Content-Length": String(size) });
      }
      const body = head ? null : await archive.read(key, range.start, range.end);
      return response(206, body!, {
        ...headers,
        "Content-Length": String(range.end - range.start),
        "Content-Range": `bytes ${range.start}-${range.end - 1}/${size}`,
      });
    } catch (e) {
      const message = (e as Error).message;
      if (e instanceof VzipError) {
        const status = e.errorClass === "request" ? 416 : e.errorClass === "resolution" ? 502 : 500;
        return response(status, message);
      }
      if (e instanceof TiffError) return response(422, `TIFF: ${message}`);
      if (e instanceof Nd2Error || e instanceof LVError) return response(422, `ND2: ${message}`);
      if (e instanceof DicomError) return response(422, `DICOM: ${message}`);
      if (e instanceof NiftiError) return response(422, `NIfTI: ${message}`);
      if (e instanceof ImsError) return response(422, `IMS: ${message}`);
      if (e instanceof N5Error) return response(422, `N5: ${message}`);
      if (e instanceof Zarr2Error) return response(422, `Zarr v2: ${message}`);
      if (e instanceof StoreError) return response(422, `store: ${message}`);
      if (e instanceof ImageError) return response(422, message);
      if (e instanceof StoreLimitError) return response(507, message);
      if (e instanceof StoreReadError) return response(502, message);
      if (e instanceof HttpResolutionError) return response(502, message);
      return response(500, message);
    }
  };
}

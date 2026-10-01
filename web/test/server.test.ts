import assert from "node:assert/strict";
import fs from "node:fs";
import { test } from "node:test";
import { HttpResolutionError } from "../src/http.ts";
import { ARCHIVE_KEY, encodeId, makeHandler } from "../src/server.ts";

const PREFIX = "https://viewer.test/vz/";
const FIXTURES = new URL("fixtures/", import.meta.url);
const remote = (name: string) => `https://data.test/${name}`;
const local = (url: string) => new Uint8Array(fs.readFileSync(new URL(url.slice("https://data.test/".length), FIXTURES)));

function handler(fetched: string[] = []) {
  return makeHandler({
    prefix: PREFIX,
    openFile: async (url) => {
      if (url.endsWith("missing.tif")) throw new HttpResolutionError(`${url}: HTTP 404`);
      const bytes = local(url);
      return { size: bytes.length, read: async (o, n) => bytes.subarray(o, o + n) };
    },
    fetchRange: async (url, start, end) => {
      fetched.push(`${start}-${end}`);
      if (url.endsWith("gone.tif")) throw new HttpResolutionError(`${url}: HTTP 404`);
      return { data: local(url).subarray(start, end), size: undefined };
    },
    fetchArchive: async () => {
      const r = await makeHandler({
        prefix: PREFIX,
        openFile: async (url) => {
          const bytes = local(url);
          return { size: bytes.length, read: async (o, n) => bytes.subarray(o, o + n) };
        },
      })(new Request(`${PREFIX}tiff/${encodeId(remote("float32_uncompressed_be.ome.tif"))}/${ARCHIVE_KEY}`));
      return new Uint8Array(await r.arrayBuffer());
    },
  });
}

const get = (h: ReturnType<typeof handler>, path: string, init?: RequestInit) =>
  h(new Request(PREFIX + path, init));
const tiff = (name: string) => `tiff/${encodeId(remote(name))}/`;

test("serves virtualized TIFFs and archives as plain HTTP", async () => {
  const fetched: string[] = [];
  const h = handler(fetched);
  const base = tiff("float32_uncompressed_be.ome.tif");

  const group = await get(h, `${base}zarr.json`);
  assert.equal(group.status, 200);
  assert.equal(group.headers.get("Content-Type"), "application/json");
  assert.equal((await group.json()).attributes.ome.version, "0.5");
  assert.equal(fetched.length, 0, "metadata needs no reads of tile data");

  // An uncompressed float32 tile is 32 * 32 * 4 bytes, read straight from the TIFF.
  const chunk = await get(h, `${base}0/c/1/2`);
  assert.equal(chunk.status, 200);
  assert.equal(chunk.headers.get("Content-Length"), "4096");
  assert.equal((await chunk.arrayBuffer()).byteLength, 4096);
  assert.equal(fetched.length, 1);

  const part = await get(h, `${base}0/c/1/2`, { headers: { Range: "bytes=10-19" } });
  assert.equal(part.status, 206);
  assert.equal(part.headers.get("Content-Range"), "bytes 10-19/4096");
  assert.deepEqual(new Uint8Array(await part.arrayBuffer()), new Uint8Array(await (await get(h, `${base}0/c/1/2`)).arrayBuffer()).subarray(10, 20));

  const suffix = await get(h, `${base}0/c/1/2`, { headers: { Range: "bytes=-6" } });
  assert.equal(suffix.headers.get("Content-Range"), "bytes 4090-4095/4096");

  const head = await get(h, `${base}0/c/1/2`, { method: "HEAD" });
  assert.equal(head.status, 200);
  assert.equal(head.headers.get("Content-Length"), "4096");
  assert.equal((await head.arrayBuffer()).byteLength, 0);

  assert.equal((await get(h, `${base}0/c/9/9`)).status, 404, "absent chunk");
  assert.equal((await get(h, `${base}__vz__/sources`)).status, 404, "hidden key");
  assert.equal((await get(h, base)).status, 404, "directory");

  const download = await get(h, `${base}${ARCHIVE_KEY}`);
  assert.equal(download.headers.get("Content-Type"), "application/zip");
  assert.match(download.headers.get("Content-Disposition")!, /filename="float32_uncompressed_be.vzip"/);

  // The downloaded archive, served through the archive route, gives the same values.
  const viaArchive = await get(h, `archive/${encodeId("https://data.test/x.vzip")}/0/c/1/2`);
  assert.deepEqual(await viaArchive.arrayBuffer(), await (await get(h, `${base}0/c/1/2`)).arrayBuffer());
});

test("rejects an id that is not base64url", async () => {
  assert.equal((await get(handler(), "tiff/not*base64/zarr.json")).status, 400);
});

test("rejects methods other than GET and HEAD", async () => {
  const r = await get(handler(), `${tiff("svs_like_int16.tif")}zarr.json`, { method: "PUT", body: "x" });
  assert.equal(r.status, 405);
});

test("rejects an unsatisfiable range", async () => {
  const r = await get(handler(), `${tiff("svs_like_int16.tif")}zarr.json`, { headers: { Range: "bytes=99999-" } });
  assert.equal(r.status, 416);
  assert.match(r.headers.get("Content-Range")!, /^bytes \*\/\d+$/);
});

test("reports an unsupported TIFF", async () => {
  const r = await get(handler(), `${tiff("unsupported_lzw.tif")}zarr.json`);
  assert.equal(r.status, 422);
  assert.match(await r.text(), /unsupported compression 5/);
});

test("reports a TIFF that cannot be opened", async () => {
  assert.equal((await get(handler(), `${tiff("missing.tif")}zarr.json`)).status, 502);
});

test("reports a tile that cannot be fetched", async () => {
  fs.copyFileSync(new URL("svs_like_int16.tif", FIXTURES), new URL("gone.tif", FIXTURES));
  try {
    const r = await get(handler(), `${tiff("gone.tif")}0/c/0/0`);
    assert.equal(r.status, 502);
    assert.match(await r.text(), /vzip resolution error/);
  } finally {
    fs.rmSync(new URL("gone.tif", FIXTURES));
  }
});

test("ignores paths outside the routes", async () => {
  assert.equal((await get(handler(), "other/zarr.json")).status, 404);
});

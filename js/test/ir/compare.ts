// Comparing the Rust core's output (src/virtualize/ir) with the frozen TypeScript
// virtualizers' (conformance/reference): the hierarchy, outside the source
// metadata node, whose mirror differs by design.

import type { Range, Source } from "../../src/protobuf.ts";
import { REVISION } from "../../src/virtualize/common.ts";
import type { ArchiveDesc } from "../../src/writer.ts";

const hex = (b: Uint8Array) => Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");

/** A range with its data source resolved to bytes (sources are numbered differently). */
function resolve(r: Range, sources: Source[]): unknown {
  if ("data" in r) return ["bytes", hex(r.data)];
  if (r.source === 0) return ["source", Number(r.offset), Number(r.length)];
  const d = sources[r.source].data!;
  return ["bytes", hex(d.subarray(Number(r.offset), Number(r.offset + r.length)))];
}

/** The entries outside `vzip_source/`: documents parsed, other bytes as hex, ranges
 * resolved. A root that records `frozen` (the frozen reference's revision) reads as
 * recording the current one. */
export function hierarchy(desc: ArchiveDesc, frozen?: number): Map<string, unknown> {
  const out = new Map<string, unknown>();
  for (const e of desc.entries) {
    if (e.key.startsWith("vzip_source/")) continue;
    if ("ranges" in e) out.set(e.key, { ranges: e.ranges.map((r) => resolve(r, desc.sources)) });
    else if (e.key === "zarr.json") {
      const json = JSON.parse(new TextDecoder().decode(e.bytes));
      const prop = json.attributes?.vzip_virtualized;
      if (frozen !== undefined && prop?.revision === frozen) prop.revision = REVISION;
      out.set(e.key, { json, compress: !!e.compress });
    } else if (e.key.endsWith(".json")) out.set(e.key, { json: JSON.parse(new TextDecoder().decode(e.bytes)), compress: !!e.compress });
    else out.set(e.key, { bytes: hex(e.bytes), compress: !!e.compress });
  }
  return out;
}

import assert from "node:assert/strict";
import fs from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { directoryStore } from "../../conformance/directory_store.ts";
import { virtualizeStore } from "../../src/virtualize/index.ts";
import { OmeZarrError } from "../../src/virtualize/ome-zarr/virtualize.ts";

const FIXTURES = fileURLToPath(new URL("../fixtures/ome-zarr/", import.meta.url));

async function virtualize(name: string) {
  return virtualizeStore(directoryStore(FIXTURES + name, `https://data.test/ome-zarr/${name}/`));
}

function doc(v: Awaited<ReturnType<typeof virtualize>>, key: string) {
  const e = v.entries.find((x) => x.key === key);
  assert.ok(e && "bytes" in e, key);
  return JSON.parse(new TextDecoder().decode(e.bytes as Uint8Array));
}

const key = (path: string) => (path ? `${path}/zarr.json` : "zarr.json");

// store: summary members, {group path: [names of the attributes A keeps as source metadata, members
// of `ome` besides `version`, or null when the group is not an OME group]}, {array path: dimension_names or null}
const CASES: [string, object, { [p: string]: [string[], string[] | null] }, { [p: string]: string[] | null }][] = [
  ["ome_zarr_image_2d", { images: 1, chunks: 2, emptyChunks: 1 }, { "": [["_creator"], ["multiscales"]] },
    { "0": ["y", "x"], "1": ["y", "x"] }],
  ["ome_zarr_image_3d_translation", { images: 1, arrays: 3 }, { "": [[], ["multiscales"]] }, { "2": ["z", "y", "x"] }],
  ["ome_zarr_image_5d_omero", { images: 1, groups: 2 },
    { "": [["custom"], ["multiscales", "omero"]], extra: [["multiscales_like"], null] },
    { "0": ["t", "c", "z", "y", "x"], "extra/table": null }],
  ["ome_zarr_custom_axes", { images: 2 }, { nested: [[], ["multiscales"]] }, { s0: ["angle", "y", "x"], "nested/0": ["k", "y", "x"] }],
  ["ome_zarr_labels", { images: 2, labels: 1, groups: 4 },
    { labels: [[], ["labels"]], "labels/cells": [[], ["image-label", "multiscales"]], "labels/unlisted": [[], null] },
    { "labels/cells/1": ["c", "y", "x"] }],
  ["ome_zarr_labels_extra_level", { labels: 1, droppedLabelLevels: 1, arrays: 5 },
    { "labels/cells": [["multiscales"], ["image-label", "multiscales"]] },
    { "labels/cells/1": ["c", "y", "x"], "labels/cells/2": null }],
  ["ome_zarr_plate", { plates: 1, wells: 2, fields: 3, images: 3, groups: 8 },
    { "": [["_creator"], ["plate"]], A: [[], null], "A/1": [[], ["well"]] }, { "A/1/1/0": ["c", "y", "x"] }],
  ["ome_zarr_well_root", { wells: 1, fields: 2 }, { "": [[], ["well"]] }, { "f1/0": ["y", "x"] }],
  ["ome_zarr_bioformats2raw", { images: 2, omeXml: 1 },
    { "": [[], ["bioformats2raw.layout"]], OME: [[], ["series"]] }, { "1/0": ["z", "y", "x"] }],
  ["ome_zarr_bioformats2raw_plate", { plates: 1, fields: 1, omeXml: 1 },
    { "": [[], ["bioformats2raw.layout", "plate"]], "A/1": [[], ["well"]] },
    { "A/1/0/1": ["t", "c", "z", "y", "x"] }],
  ["ome_zarr_source_metadata", { wells: 1, fields: 2, otherObjects: 2 },
    { "": [[], ["well"]], "0": [["note"], ["multiscales", "omero"]], "1": [[], ["multiscales", "omero"]] },
    { "1/0": ["y", "x"] }],
];

// The OME members without a version, which `ome` gives back as they are (conventions/ome-zarr/README.md §5, §8).
const UNVERSIONED: { [storePath: string]: string[] } = {
  "ome_zarr_custom_axes nested": ["multiscales"], "ome_zarr_bioformats2raw_plate ": ["plate"],
  "ome_zarr_bioformats2raw_plate A/1": ["well"], "ome_zarr_source_metadata 0": ["omero"],
};

test("virtualizes the synthetic OME-Zarr 0.4 stores as 0.5", async () => {
  for (const [name, summary, groups, arrays] of CASES) {
    const v = await virtualize(name);
    assert.equal(v.format, "ome-zarr", name);
    assert.deepEqual({ ...v.summary, ...summary }, v.summary, name);
    for (const [path, [own, ome]] of Object.entries(groups)) {
      const attrs = doc(v, key(path)).attributes;
      // The attributes A that `ome` does not give back are copied under the convention, which the
      // root and any node with source metadata declare (conventions §2).
      const s = attrs.vzip_virtualized?.["ome-zarr"] ?? {};
      assert.deepEqual(Object.keys(s.attributes ?? {}).sort(), [...own].sort(), `${name} ${path}`);
      assert.deepEqual(s.unversioned ?? [], UNVERSIONED[`${name} ${path}`] ?? [], `${name} ${path}`);
      const outer = path === "" || Object.keys(s).length > 0 ? ["zarr_conventions", "vzip_virtualized"] : [];
      if (ome === null) {
        assert.deepEqual(Object.keys(attrs).sort(), outer.sort(), `${name} ${path}`);
        continue;
      }
      assert.deepEqual(Object.keys(attrs).sort(), [...outer, "ome"].sort(), `${name} ${path}`);
      assert.deepEqual(Object.keys(attrs.ome).sort(), [...ome, "version"].sort(), `${name} ${path}`);
      assert.equal(attrs.ome.version, "0.5");
      for (const m of attrs.ome.multiscales ?? []) assert.ok(!("version" in m), `${name} ${path}`);
      for (const k of ["omero", "image-label", "plate", "well"]) assert.ok(!("version" in (attrs.ome[k] ?? {})), `${name} ${path} ${k}`);
    }
    for (const [path, names] of Object.entries(arrays)) {
      assert.deepEqual(doc(v, key(path)).dimension_names ?? null, names, `${name} ${path}`);
    }
  }
  const i = await virtualize("ome_zarr_image_2d");
  assert.deepEqual(i.entries.filter((e) => "ranges" in e).map((e) => e.key), ["0/0.0", "1/0.0", "vzip_source/objects/notes.txt"]);
  // The empty chunk's key is listed with the empty objects (the Zarr v2 convention §5).
  assert.deepEqual(doc(i, "vzip_source/zarr.json").attributes.vzip_virtualized, { "ome-zarr": { empty: ["0/1.0"] } });
  const l = await virtualize("ome_zarr_labels");
  // Every OME member is given back by the inverse of §5: no source metadata.
  assert.deepEqual(doc(l, "labels/zarr.json").attributes, { ome: { version: "0.5", labels: ["cells"] } });
  assert.deepEqual(doc(l, "labels/cells/zarr.json").attributes.ome["image-label"].source, { image: "../../" });
  const x = await virtualize("ome_zarr_labels_extra_level");
  const lm = doc(x, "labels/cells/zarr.json").attributes.ome.multiscales[0];
  assert.deepEqual(lm.datasets.map((d: { path: string }) => d.path), ["0", "1"]);
  const b = await virtualize("ome_zarr_bioformats2raw");
  assert.deepEqual(doc(b, "OME/zarr.json").attributes.ome, { version: "0.5", series: ["0", "1"] });
  assert.equal(b.sources.at(-1)!.url, "https://data.test/ome-zarr/ome_zarr_bioformats2raw/OME/METADATA.ome.xml");
  const s = await virtualize("ome_zarr_source_metadata");
  assert.deepEqual(doc(s, "zarr.json").attributes.vzip_virtualized["ome-zarr"], { metadata: { creator: "a writer" } });
  assert.deepEqual(doc(s, "1/0/zarr.json").attributes.vzip_virtualized, { "ome-zarr": { metadata: { fill_value: null } } });
  const zero = s.entries.find((e) => e.key === "0/zarr.json") as { bytes: Uint8Array };
  assert.ok(new TextDecoder().decode(zero.bytes).includes('"note":{"big":18446744073709551615}'));
  assert.deepEqual(s.entries.filter((e) => e.key.startsWith("vzip_source/")).map((e) => e.key).sort(),
    ["vzip_source/objects/OME/METADATA.ome.xml", "vzip_source/objects/README.md", "vzip_source/zarr.json"]);
  // An empty OME-XML object is an empty object: its key is vzip_source's (conventions/ome-zarr/README.md §7).
  const e = await virtualize("ome_zarr_empty_xml");
  assert.equal((e.summary as { omeXml: number }).omeXml, 0);
  assert.deepEqual(doc(e, "vzip_source/zarr.json").attributes.vzip_virtualized, { "ome-zarr": { empty: ["OME/METADATA.ome.xml"] } });
  // Wells and images without a version name their members rather than copy them.
  const p = await virtualize("ome_zarr_plate_unversioned");
  assert.deepEqual(doc(p, "A/1/zarr.json").attributes.vzip_virtualized, { "ome-zarr": { unversioned: ["well"] } });
  assert.deepEqual(doc(p, "B/3/0/zarr.json").attributes.vzip_virtualized, { "ome-zarr": { unversioned: ["multiscales", "omero"] } });
});

const REJECTIONS: [string, RegExp][] = [
  ["has_ome", /already has an `ome` attribute/],
  ["version", /old: multiscales\[0\]: version "0.3" is not 0.4/],
  ["multiscales_empty", /g: multiscales is not a nonempty array/],
  ["axes_count", /axes is not 2 to 5 axis objects/],
  ["axes_name", /an axis name is not a string/],
  ["axes_duplicate", /are not distinct/],
  ["axes_type", /axis type 3 is not a string/],
  ["axes_order", /axis types \["space","channel","space"\]/],
  ["axes_two_others", /axis types \["channel","phase","space","space"\]/],
  ["axes_four_space", /axis types \["space","space","space","space"\]/],
  ["datasets_empty", /datasets is not a nonempty array/],
  ["datasets_path", /path "..\/1" is not a relative path/],
  ["datasets_missing", /9 is not an array/],
  ["datasets_rank", /r has 3 dimensions, not 2/],
  ["transforms_missing", /coordinateTransformations is not one or two/],
  ["transforms_order", /the first transformation is not a scale/],
  ["transforms_identity", /the first transformation is not a scale/],
  ["transforms_path", /the scale is not 2 numbers/],
  ["scale_length", /the scale is not 2 numbers/],
  ["translation_length", /the translation is not 2 numbers/],
  ["multiscale_transforms", /multiscales\[0\]: the scale is not 2 numbers/],
  ["scale_order", /is smaller than the previous level's/],
  ["level_axes_conflict", /p\/q\/lvl is a level of images with axes/],
  ["omero_channels", /omero: channels is not an array/],
  ["omero_color", /color "red" is not 6 hexadecimal digits/],
  ["omero_window", /window does not have numbers/],
  ["labels_not_image", /label "missing" is not the path of an image/],
  ["image_label_no_multiscales", /image-label: the group has no multiscales/],
  ["image_label_levels", /the label image has 1 levels, fewer than its image \/'s 2/],
  ["image_label_dtype", /label data type float32 is not an integer type/],
  ["image_label_color_duplicate", /label-values are not unique/],
  ["image_label_rgba", /rgba \[1,2,3,300\]/],
  ["image_label_value", /properties is not an array of objects with an integer label-value/],
  ["image_label_source", /source image "..\/..\/elsewhere" is not an image/],
  ["image_label_version", /image-label: version "0.3" is not 0.4/],
  ["plate_column_name", /a column name is not alphanumeric/],
  ["plate_row_duplicate", /row names are not unique/],
  ["plate_well_path", /well path "B\/2" is not row A\/column 2/],
  ["plate_well_index", /rowIndex or columnIndex is not an index/],
  ["plate_well_missing", /A\/2 is not a well/],
  ["plate_acquisition_id", /acquisition id -1/],
  ["plate_field_count", /field_count 0 is not a positive integer/],
  ["plate_version", /plate: version "0.3" is not 0.4/],
  ["well_image_path", /image path "f-0" is not alphanumeric/],
  ["well_image_missing", /A\/1\/7 is not an image/],
  ["well_image_duplicate", /image paths are not unique/],
  ["well_acquisition", /acquisition 5 is not one of the plate's/],
  ["well_acquisition_missing", /an image has no acquisition, and the plate has several/],
  ["bioformats2raw_layout", /bioformats2raw.layout is not 3/],
  ["bioformats2raw_numbering", /not numbered consecutively from 0/],
  ["bioformats2raw_series", /series is not an array of image paths/],
];

for (const [name, message] of REJECTIONS) {
  test(`rejects ome_zarr_reject_${name}`, async () => {
    await assert.rejects(virtualize(`ome_zarr_reject_${name}`), (e) => e instanceof OmeZarrError && message.test(e.message));
  });
}

test("every rejection fixture has a test", () => {
  const names = fs.readdirSync(FIXTURES).filter((n) => n.includes("_reject_")).map((n) => n.replace("ome_zarr_reject_", ""));
  assert.deepEqual(names.sort(), REJECTIONS.map(([n]) => n).sort());
});

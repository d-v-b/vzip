// The OME-Zarr profile (spec/virtualize.md, §11): an OME-Zarr 0.4 hierarchy
// on Zarr v2 storage as OME-Zarr 0.5 on Zarr v3, each chunk referenced in place.

import {
  asInt,
  canonicalIndex,
  compareKeys,
  has,
  isNumber,
  isObject,
  join,
  type Json,
  num,
  parentOf,
  readDocument,
  show,
  type Store,
  type StoreResult,
  without,
} from "../store.ts";
import { hierarchyOutput, prefetch, readHierarchy } from "../zarr2/virtualize.ts";

export class OmeZarrError extends Error {}

const reject = (message: string): never => {
  throw new OmeZarrError(message);
};

type Obj = { [k: string]: Json };

const OME_KEYS = ["multiscales", "omero", "labels", "image-label", "plate", "well", "bioformats2raw.layout"];
const VERSIONED = new Set(["omero", "image-label", "plate", "well"]);
const LABEL_TYPES = new Set(["int8", "uint8", "int16", "uint16", "int32", "uint32", "int64", "uint64"]);
const KINDS = /^t?o?s{2,3}$/;
const HEX6 = /^[0-9A-Fa-f]{6}$/;
const ALNUM = /^[A-Za-z0-9]+$/;

/** The member `k` of `o`, or undefined when it has none. */
const get = (o: Obj, k: string): Json | undefined => (has(o, k) ? o[k] : undefined);

/** A relative path (spec/virtualize/ome-zarr.md §3): one or more segments separated by `/`, none empty, `.` or `..`. */
export function relPath(p: unknown): p is string {
  return typeof p === "string" && p !== "" && p.split("/").every((s) => s !== "" && s !== "." && s !== "..");
}

const isInt = (v: unknown, lo?: number) => asInt(v, lo) !== undefined;

function multiscales04(a: Obj): boolean {
  const ms = get(a, "multiscales");
  return Array.isArray(ms) && ms.some((m) => isObject(m) && get(m, "version") === "0.4");
}

function version04(a: Obj, key: string): boolean {
  const v = get(a, key);
  return isObject(v) && get(v, "version") === "0.4";
}

/** Does the root of this Zarr v2 store declare OME-NGFF 0.4 content (§1.4)? */
export async function declares04(store: Store): Promise<boolean> {
  const objects = store.objects;
  prefetch(store); // the documents §10 reads, these among them
  const attrs = async (path: string): Promise<Obj | undefined> => {
    if (!objects.has(join(path, ".zgroup")) || !objects.has(join(path, ".zattrs"))) return undefined;
    const a = await readDocument(store, join(path, ".zattrs"), { keep: true }); // read again by readHierarchy
    return isObject(a) ? a : undefined;
  };
  if (objects.has(".zarray")) return false;
  const a = await attrs("");
  if (a === undefined) return false;
  if (multiscales04(a) || version04(a, "plate") || version04(a, "well")) return true;
  if (!has(a, "bioformats2raw.layout")) return false;
  let q: string | undefined;
  if (has(a, "plate")) {
    const plate = a.plate;
    const wells = isObject(plate) ? get(plate, "wells") : undefined;
    if (Array.isArray(wells) && wells.length > 0 && isObject(wells[0]) && relPath(get(wells[0], "path"))) {
      const w = wells[0].path as string;
      const wa = await attrs(w);
      const well = wa !== undefined ? get(wa, "well") : undefined;
      const images = isObject(well) ? get(well, "images") : undefined;
      if (Array.isArray(images) && images.length > 0 && isObject(images[0]) && relPath(get(images[0], "path"))) {
        q = join(w, images[0].path as string);
      }
    }
  } else {
    q = "0";
    const oa = await attrs("OME");
    const series = oa !== undefined ? get(oa, "series") : undefined;
    if (Array.isArray(series) && series.length > 0 && relPath(series[0])) q = series[0];
  }
  if (q === undefined) return false;
  const qa = await attrs(q);
  return qa !== undefined && multiscales04(qa);
}

/** `rel` resolved against the group path `base` (spec/virtualize/ome-zarr.md §4, image-label `source`), or undefined above the root. */
export function resolve(base: string, rel: string): string | undefined {
  const segs = base === "" ? [] : base.split("/");
  for (const s of rel.split("/")) {
    if (s === "" || s === ".") continue;
    if (s === "..") {
      if (segs.length === 0) return undefined;
      segs.pop();
    } else {
      segs.push(s);
    }
  }
  return segs.join("/");
}

/** The scale of a valid list of coordinate transformations (spec/virtualize/ome-zarr.md §4). */
function transforms(where: string, ts: Json | undefined, n: number): number[] {
  if (!Array.isArray(ts) || (ts.length !== 1 && ts.length !== 2) || !ts.every(isObject)) {
    reject(`${where}: coordinateTransformations is not one or two transformation objects`);
  }
  const [s, t] = ts as Obj[];
  if (get(s, "type") !== "scale") reject(`${where}: the first transformation is not a scale`);
  const scale = get(s, "scale");
  if (!(Array.isArray(scale) && scale.length === n && scale.every(isNumber))) reject(`${where}: the scale is not ${n} numbers`);
  if (t !== undefined) {
    if (get(t, "type") !== "translation") reject(`${where}: the second transformation is not a translation`);
    const tr = get(t, "translation");
    if (!(Array.isArray(tr) && tr.length === n && tr.every(isNumber))) reject(`${where}: the translation is not ${n} numbers`);
  }
  return (scale as Json[]).map(num);
}

function checkVersion(where: string, o: Obj): void {
  if (has(o, "version") && o.version !== "0.4") reject(`${where}: version ${show(o.version).slice(0, 20)} is not 0.4`);
}

const sorted = (xs: Iterable<string>) => [...xs].sort(compareKeys);
const lastSegment = (p: string) => p.slice(p.lastIndexOf("/") + 1);

export async function virtualizeOmeZarr(store: Store): Promise<StoreResult & { summary: object }> {
  const h = await readHierarchy(store); // spec/virtualize/ome-zarr.md §2: every rule of spec/virtualize/zarr2.md §2–§3
  const attrs = h.groups;
  const arrays = h.arrays;
  for (const path of sorted(attrs.keys())) {
    if (has(attrs.get(path)!, "ome")) reject(`${path || "/"}: the group already has an \`ome\` attribute`);
  }
  const collections = sorted([...attrs.keys()].filter((p) => has(attrs.get(p)!, "bioformats2raw.layout")));
  const seriesGroups = new Set(collections.map((c) => join(c, "OME")).filter((o) => attrs.has(o) && has(attrs.get(o)!, "series")));
  const omeGroups = sorted([...attrs.keys()].filter((p) => seriesGroups.has(p) || OME_KEYS.some((k) => has(attrs.get(p)!, k))));
  const images = new Set([...attrs.keys()].filter((p) => has(attrs.get(p)!, "multiscales")));

  // spec/virtualize/ome-zarr.md §4 Images.
  const levels: [string, number, number, string, string[]][] = []; // (image, multiscale, dataset, array, axis names)
  const multiscales = new Map<string, Obj[]>();
  for (const g of sorted(images)) {
    const ms = attrs.get(g)!.multiscales;
    const where = `${g || "/"}: multiscales`;
    if (!Array.isArray(ms) || ms.length === 0 || !ms.every(isObject)) reject(`${where} is not a nonempty array of objects`);
    multiscales.set(g, ms as Obj[]);
    (ms as Obj[]).forEach((m, i) => {
      const w = `${where}[${i}]`;
      checkVersion(w, m);
      const axes = get(m, "axes");
      if (!Array.isArray(axes) || axes.length < 2 || axes.length > 5 || !axes.every(isObject)) {
        reject(`${w}: axes is not 2 to 5 axis objects`);
      }
      const ax = axes as Obj[];
      if (!ax.every((a) => typeof get(a, "name") === "string")) reject(`${w}: an axis name is not a string`);
      const axisNames = ax.map((a) => a.name as string);
      if (new Set(axisNames).size !== axisNames.length) reject(`${w}: axis names ${show(axisNames)} are not distinct`);
      let kinds = "";
      for (const a of ax) {
        const t = get(a, "type");
        if (t !== undefined && t !== null && typeof t !== "string") reject(`${w}: axis type ${show(t).slice(0, 20)} is not a string`);
        kinds += t === "space" ? "s" : t === "time" ? "t" : "o";
      }
      if (!KINDS.test(kinds)) {
        reject(`${w}: axis types ${show(ax.map((a) => get(a, "type") ?? null))} are not [time], [channel or custom], then 2 or 3 space`);
      }
      const n = ax.length;
      const datasets = get(m, "datasets");
      if (!Array.isArray(datasets) || datasets.length === 0 || !datasets.every(isObject)) {
        reject(`${w}: datasets is not a nonempty array of objects`);
      }
      let prev: number[] | undefined;
      (datasets as Obj[]).forEach((d, j) => {
        const wd = `${w}.datasets[${j}]`;
        const p = get(d, "path");
        if (!relPath(p)) reject(`${wd}: path ${show(p).slice(0, 40)} is not a relative path`);
        const target = join(g, p as string);
        if (!arrays.has(target)) reject(`${wd}: ${target} is not an array`);
        const rank = arrays.get(target)!.shape.length;
        if (rank !== n) reject(`${wd}: ${target} has ${rank} dimensions, not ${n}`);
        const scale = transforms(wd, get(d, "coordinateTransformations"), n);
        if (prev !== undefined && scale.some((b, k) => b < prev![k])) {
          reject(`${wd}: the scale ${show(scale)} is smaller than the previous level's ${show(prev)}`);
        }
        prev = scale;
        levels.push([g, i, j, target, axisNames]);
      });
      if (has(m, "coordinateTransformations")) transforms(w, m.coordinateTransformations, n);
    });
  }

  // spec/virtualize/ome-zarr.md §4 omero.
  for (const g of omeGroups) {
    const a = attrs.get(g)!;
    if (!has(a, "omero")) continue;
    const o = a.omero;
    const w = `${g || "/"}: omero`;
    const channels = isObject(o) ? get(o, "channels") : undefined;
    if (!Array.isArray(channels) || !channels.every(isObject)) reject(`${w}: channels is not an array of objects`);
    for (const c of channels as Obj[]) {
      const color = get(c, "color");
      if (!(typeof color === "string" && HEX6.test(color))) reject(`${w}: channel color ${show(color).slice(0, 20)} is not 6 hexadecimal digits`);
      const win = get(c, "window");
      if (!isObject(win) || !["min", "max", "start", "end"].every((k) => isNumber(get(win, k)))) {
        reject(`${w}: a channel window does not have numbers min, max, start and end`);
      }
    }
  }

  // spec/virtualize/ome-zarr.md §4 Labels.
  const labelImages = new Set<string>();
  const kept = new Map<string, number>(); // `${label image}\0${multiscale}` -> datasets kept, when fewer than all
  const keptKey = (g: string, i: number) => `${g}\0${i}`;
  for (const g of omeGroups) {
    const a = attrs.get(g)!;
    if (has(a, "labels")) {
      const ls = a.labels;
      if (!Array.isArray(ls)) reject(`${g || "/"}: labels is not an array`);
      for (const x of ls as Json[]) {
        if (!relPath(x) || !images.has(join(g, x))) reject(`${g || "/"}: label ${show(x).slice(0, 40)} is not the path of an image`);
        labelImages.add(join(g, x as string));
      }
    }
    if (has(a, "image-label")) {
      const il = a["image-label"];
      const w = `${g || "/"}: image-label`;
      if (!isObject(il)) reject(`${w} is not an object`);
      const l = il as Obj;
      checkVersion(w, l);
      if (!images.has(g)) reject(`${w}: the group has no multiscales`);
      labelImages.add(g);
      for (const key of ["colors", "properties"]) {
        if (!has(l, key)) continue;
        const items = l[key];
        if (!Array.isArray(items) || !items.every((c) => isObject(c) && isInt(get(c, "label-value")))) {
          reject(`${w}: ${key} is not an array of objects with an integer label-value`);
        }
      }
      if (has(l, "colors")) {
        const colors = l.colors as Obj[];
        const values = colors.map((c) => c["label-value"]);
        if (new Set(values).size !== values.length) reject(`${w}: colors label-values are not unique`);
        for (const c of colors) {
          if (has(c, "rgba") && !(Array.isArray(c.rgba) && c.rgba.length === 4 && c.rgba.every((x) => asInt(x, 0, 255) !== undefined))) {
            reject(`${w}: rgba ${show(c.rgba).slice(0, 40)} is not four integers from 0 to 255`);
          }
        }
      }
      if (has(l, "source")) {
        const src = l.source;
        if (!isObject(src) || (has(src, "image") && typeof src.image !== "string")) reject(`${w}: source is not an object with a string image`);
      }
    }
  }
  for (const x of sorted(labelImages)) {
    const il = get(attrs.get(x)!, "image-label");
    const src = isObject(il) ? get(il, "source") : undefined;
    const rel = isObject(src) ? (get(src, "image") as string | undefined) : undefined;
    const source = resolve(x, rel ?? "../../");
    if (rel !== undefined && (source === undefined || !images.has(source))) reject(`${x}: the label's source image ${show(rel)} is not an image`);
    if (source !== undefined && images.has(source)) {
      const n = (multiscales.get(source)![0].datasets as Json[]).length;
      multiscales.get(x)!.forEach((m, i) => {
        const k = (m.datasets as Json[]).length;
        if (k < n) reject(`${x}: the label image has ${k} levels, fewer than its image ${source || "/"}'s ${n}`);
        if (k > n) kept.set(keptKey(x, i), n); // L7: the levels past the image's are dropped
      });
    }
    for (const [i, m] of multiscales.get(x)!.entries()) {
      for (const d of (m.datasets as Obj[]).slice(0, kept.get(keptKey(x, i)))) {
        const level = join(x, d.path as string);
        const dt = arrays.get(level)!.data_type as string;
        if (!LABEL_TYPES.has(dt)) reject(`${level}: label data type ${dt} is not an integer type`);
      }
    }
  }

  // spec/virtualize/ome-zarr.md §4 Plates and wells.
  const wells = new Set([...attrs.keys()].filter((p) => has(attrs.get(p)!, "well")));
  const wellImages = new Map<string, Obj[]>();
  for (const g of sorted(wells)) {
    const wl = attrs.get(g)!.well;
    const w = `${g || "/"}: well`;
    if (!isObject(wl)) reject(`${w} is not an object`);
    checkVersion(w, wl as Obj);
    const ims = get(wl as Obj, "images");
    if (!Array.isArray(ims) || !ims.every(isObject)) reject(`${w}: images is not an array of objects`);
    const paths = (ims as Obj[]).map((i) => get(i, "path"));
    for (const i of ims as Obj[]) {
      const p = get(i, "path");
      if (!(typeof p === "string" && ALNUM.test(p))) reject(`${w}: image path ${show(p).slice(0, 40)} is not alphanumeric`);
      if (!images.has(join(g, p as string))) reject(`${w}: ${join(g, p as string)} is not an image`);
      if (has(i, "acquisition") && !isInt(i.acquisition)) reject(`${w}: acquisition ${show(i.acquisition).slice(0, 20)} is not an integer`);
    }
    if (new Set(paths).size !== paths.length) reject(`${w}: image paths are not unique`);
    wellImages.set(g, ims as Obj[]);
  }
  const plates = sorted([...attrs.keys()].filter((p) => has(attrs.get(p)!, "plate")));
  for (const g of plates) {
    const pl = attrs.get(g)!.plate;
    const w = `${g || "/"}: plate`;
    if (!isObject(pl)) reject(`${w} is not an object`);
    const p = pl as Obj;
    checkVersion(w, p);
    const named: { [k: string]: string[] } = {};
    for (const key of ["columns", "rows"]) {
      const items = get(p, key);
      if (!Array.isArray(items) || !items.every(isObject)) reject(`${w}: ${key} is not an array of objects`);
      const ns = (items as Obj[]).map((c) => get(c, "name"));
      if (!ns.every((x) => typeof x === "string" && ALNUM.test(x))) reject(`${w}: a ${key.slice(0, -1)} name is not alphanumeric`);
      if (new Set(ns).size !== ns.length) reject(`${w}: ${key.slice(0, -1)} names are not unique`);
      named[key] = ns as string[];
    }
    if (has(p, "field_count") && asInt(p.field_count, 1) === undefined) reject(`${w}: field_count ${show(p.field_count).slice(0, 20)} is not a positive integer`);
    if (has(p, "name") && typeof p.name !== "string") reject(`${w}: name is not a string`);
    let ids: number[] | undefined;
    if (has(p, "acquisitions")) {
      const acq = p.acquisitions;
      if (!Array.isArray(acq) || !acq.every(isObject)) reject(`${w}: acquisitions is not an array of objects`);
      for (const a of acq as Obj[]) {
        if (asInt(get(a, "id"), 0) === undefined) reject(`${w}: acquisition id ${show(get(a, "id")).slice(0, 20)} is not an integer from 0`);
        if (has(a, "maximumfieldcount") && asInt(a.maximumfieldcount, 1) === undefined) reject(`${w}: maximumfieldcount is not a positive integer`);
        for (const k of ["name", "description"]) if (has(a, k) && typeof a[k] !== "string") reject(`${w}: acquisition ${k} is not a string`);
        for (const k of ["starttime", "endtime"]) if (has(a, k) && !isInt(a[k])) reject(`${w}: acquisition ${k} is not an integer`);
      }
      ids = (acq as Obj[]).map((a) => a.id as number);
      if (new Set(ids).size !== ids.length) reject(`${w}: acquisition ids are not unique`);
    }
    const ws = get(p, "wells");
    if (!Array.isArray(ws) || !ws.every(isObject)) reject(`${w}: wells is not an array of objects`);
    for (const x of ws as Obj[]) {
      const r = asInt(get(x, "rowIndex"), 0);
      const c = asInt(get(x, "columnIndex"), 0);
      const path = get(x, "path");
      if (r === undefined || c === undefined || r >= named.rows.length || c >= named.columns.length) {
        reject(`${w}: well rowIndex or columnIndex is not an index of rows or columns`);
      }
      if (path !== `${named.rows[r!]}/${named.columns[c!]}`) {
        reject(`${w}: well path ${show(path).slice(0, 40)} is not row ${named.rows[r!]}/column ${named.columns[c!]}`);
      }
      const wp = join(g, path as string);
      if (!wells.has(wp)) reject(`${w}: ${wp} is not a well`);
      if (ids !== undefined) {
        for (const i of wellImages.get(wp)!) {
          if (has(i, "acquisition")) {
            if (!ids.includes(i.acquisition as number)) reject(`${wp}: acquisition ${show(i.acquisition)} is not one of the plate's`);
          } else if (ids.length > 1) {
            reject(`${wp}: an image has no acquisition, and the plate has several`);
          }
        }
      }
    }
  }

  // spec/virtualize/ome-zarr.md §4 Collections (bioformats2raw.layout).
  for (const g of collections) {
    const a = attrs.get(g)!;
    if (asInt(a["bioformats2raw.layout"]) !== 3) reject(`${g || "/"}: bioformats2raw.layout is not 3`);
    const ome = join(g, "OME");
    if (seriesGroups.has(ome)) {
      const series = attrs.get(ome)!.series;
      if (!Array.isArray(series) || !series.every((s) => relPath(s) && images.has(join(g, s)))) {
        reject(`${ome}: series is not an array of image paths`);
      }
    } else if (!has(a, "plate")) {
      const numbered = [...images]
        .filter((p) => p !== g && parentOf(p) === g && canonicalIndex(lastSegment(p), 2 ** 53))
        .map((p) => Number(lastSegment(p)))
        .sort((x, y) => x - y);
      if (numbered.length === 0 || numbered.some((v, i) => v !== i)) reject(`${g || "/"}: the images are not numbered consecutively from 0`);
    }
  }

  // spec/virtualize/ome-zarr.md §4 I8 and spec/virtualize/ome-zarr.md §6: the axis names of the levels the output keeps.
  const names = new Map<string, string[]>();
  for (const [g, i, j, target, axisNames] of levels) {
    if (j >= (kept.get(keptKey(g, i)) ?? j + 1)) continue;
    if (!names.has(target)) names.set(target, axisNames);
    else if (show(names.get(target)) !== show(axisNames)) {
      reject(`${target} is a level of images with axes ${show(names.get(target))} and ${show(axisNames)}`);
    }
  }
  let dropped = 0;
  for (const [k, n] of kept) {
    const [g, i] = [k.slice(0, k.lastIndexOf("\0")), Number(k.slice(k.lastIndexOf("\0") + 1))];
    dropped += (multiscales.get(g)![i].datasets as Json[]).length - n;
  }

  // spec/virtualize/ome-zarr.md §5, spec/virtualize/ome-zarr.md §6 Output.
  const groups = new Map<string, Obj>();
  const omes = new Map<string, Json>();
  const unversioned = new Map<string, string[]>();
  const omeSet = new Set(omeGroups);
  for (const [path, a] of attrs) {
    if (!omeSet.has(path)) {
      groups.set(path, a);
      continue;
    }
    const keys = seriesGroups.has(path) ? [...OME_KEYS, "series"] : OME_KEYS;
    const ome: Obj = { version: "0.5" };
    const restored = new Set<string>(); // the OME members that the inverse of §5 gives back from `ome`
    const bare: string[] = []; // those of them without a version (spec/virtualize/ome-zarr.md §5, §8)
    for (const k of keys) {
      if (!has(a, k)) continue;
      let v = a[k];
      if (k === "multiscales") {
        const ms = v as Obj[];
        if (!ms.some((_, i) => kept.has(keptKey(path, i)))) {
          if (ms.every((m) => get(m, "version") === "0.4")) {
            restored.add(k);
          } else if (ms.every((m) => !has(m, "version"))) {
            restored.add(k);
            bare.push(k);
          }
        }
        v = ms.map((m, i) => {
          const out = without(m, ["version"]);
          const n = kept.get(keptKey(path, i));
          if (n !== undefined) out.datasets = (m.datasets as Json[]).slice(0, n);
          return out;
        });
      } else if (VERSIONED.has(k)) {
        if (get(v as Obj, "version") === "0.4") {
          restored.add(k);
        } else if (!has(v as Obj, "version")) {
          restored.add(k);
          bare.push(k);
        }
        v = without(v as Obj, ["version"]);
      } else {
        restored.add(k);
      }
      ome[k] = v;
    }
    // The attributes A keep what the inverse does not give back (spec/virtualize/ome-zarr.md §8).
    groups.set(path, without(a, [...restored]));
    omes.set(path, ome);
    if (bare.length) unversioned.set(path, bare);
  }
  const docs = new Map<string, Json>();
  for (const [path, doc] of arrays) {
    const names_ = names.get(path);
    docs.set(path, names_ === undefined ? (doc as unknown as Json) : { ...without(doc as unknown as Obj, ["attributes"]), dimension_names: names_, attributes: doc.attributes });
  }
  const xml: [string, number][] = collections
    .map((c) => join(join(c, "OME"), "METADATA.ome.xml"))
    .filter((k) => store.objects.has(k))
    .map((k) => [k, store.objects.get(k)!]);
  const [result, all, others] = hierarchyOutput(store, h, groups, docs, "ome-zarr", xml, omes, unversioned);
  const nonempty = all.filter(([, n]) => n > 0).length;
  let fields = 0;
  for (const v of wellImages.values()) fields += v.length;
  return {
    ...result,
    summary: {
      groups: attrs.size + h.implicit.size, arrays: arrays.size, chunks: nonempty, emptyChunks: all.length - nonempty,
      objects: store.objects.size, images: images.size, labels: labelImages.size, droppedLabelLevels: dropped, plates: plates.length,
      wells: wells.size, fields, omeXml: xml.filter(([, n]) => n > 0).length,
      otherObjects: others, listingRequests: store.requests,
    },
  };
}

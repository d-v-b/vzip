// A SAFE product's objects, product metadata and tile metadata (spec/virtualize/safe.md §2).

import { compareKeys, parseXml, type XmlElement } from "../store.ts";

/** The input is rejected by the SAFE profile (spec/virtualize/safe/profile.md §12.5). */
export class SafeError extends Error {}

const reject = (m: string): never => {
  throw new SafeError(m);
};

export const MAX_XML = 1 << 24;
const MAX_U32 = 0xffffffff;
const MARKER = "_$folder$";
const LEVELS: Record<string, [string, string, string]> = {
  "MTD_MSIL1C.xml": ["L1C", "Level-1C_User_Product", "Level-1C_Tile_ID"],
  "MTD_MSIL2A.xml": ["L2A", "Level-2A_User_Product", "Level-2A_Tile_ID"],
};
const GRANULE = "General_Info/Product_Info/Product_Organisation/Granule_List/Granule";
/** The names of PSD 14.2 (Level-2A, processing baselines 02.04–02.06) that stand for later ones (§2.1). */
const ALIASES: Record<string, string> = {
  Product_Info: "L2A_Product_Info",
  Product_Organisation: "L2A_Product_Organisation",
  IMAGE_FILE: "IMAGE_FILE_2A",
  PRODUCT_URI: "PRODUCT_URI_2A",
  Product_Image_Characteristics: "L2A_Product_Image_Characteristics",
  QUANTIFICATION_VALUES_LIST: "L1C_L2A_Quantification_Values_List",
  BOA_QUANTIFICATION_VALUE: "L2A_BOA_QUANTIFICATION_VALUE",
  AOT_QUANTIFICATION_VALUE: "L2A_AOT_QUANTIFICATION_VALUE",
  WVP_QUANTIFICATION_VALUE: "L2A_WVP_QUANTIFICATION_VALUE",
  Scene_Classification_List: "L2A_Scene_Classification_List",
  Scene_Classification_ID: "L2A_Scene_Classification_ID",
  SCENE_CLASSIFICATION_TEXT: "L2A_SCENE_CLASSIFICATION_TEXT",
  SCENE_CLASSIFICATION_INDEX: "L2A_SCENE_CLASSIFICATION_INDEX",
  TILE_ID: "TILE_ID_2A",
};
export const DECIMAL = /^[+-]?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$/;
const POSITIVE = /^[1-9][0-9]*$/;

// ---------------------------------------------------------------- XML elements (§2.1)

/** An element's local name: its name after the last `:`. */
export const local = (name: string) => name.slice(name.lastIndexOf(":") + 1);

/** The children of `el` whose local name is `name`, or its PSD 14.2 alias. */
export function kids(el: XmlElement, name: string): XmlElement[] {
  const alias = Object.hasOwn(ALIASES, name) ? ALIASES[name] : name;
  return el.children.filter((c) => {
    const n = local(c.name);
    return n === name || n === alias;
  });
}

/** The elements at the path `p` (local names separated by `/`) under `el`. */
export function path(el: XmlElement, p: string): XmlElement[] {
  let found = [el];
  for (const name of p.split("/")) found = found.flatMap((e) => kids(e, name));
  return found;
}

/** The value of the attribute `name` (as written), the first if repeated. */
export function attr(el: XmlElement, name: string): string | undefined {
  return el.attributes.find(([n]) => n === name)?.[1];
}

/** An element's text, without leading and trailing whitespace; undefined when it has a child element. */
export function text(el: XmlElement): string | undefined {
  if (el.children.length) return undefined;
  return el.text.join("").replace(/^[ \t\r\n]+|[ \t\r\n]+$/g, "");
}

function one(els: XmlElement[], what: string): XmlElement {
  if (els.length !== 1) reject(`${what}: ${els.length} elements where there must be one`);
  return els[0];
}

function layoutText(el: XmlElement, what: string): string {
  const t = text(el);
  return t === undefined ? reject(`${what} has a child element`) : t;
}

/** A product's metadata document: UTF-8, in the XML subset of spec/virtualize.md §1.5 (§2.1). */
export function parseDocument(data: Uint8Array, what: string): XmlElement {
  if (data[0] === 0xef && data[1] === 0xbb && data[2] === 0xbf) reject(`${what} starts with a byte order mark`);
  let s = "";
  try {
    s = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(data);
  } catch {
    reject(`${what} is not UTF-8`);
  }
  return parseXml(s, what, (m) => new SafeError(m));
}

// ---------------------------------------------------------------- objects (§2.1)

/** [the folder markers among `keys`, the empty directories] (§2.1). A folder marker is a key
 * `d_$folder$` with `d` not empty; `directories` are the keys, ending in `/`, of a zip file's
 * directory entries or of a listing's empty `d/` objects. The directories they name are empty
 * when no key (theirs and the markers' included) is under them. */
export function folders(keys: Iterable<string>, directories: string[] = []): [Set<string>, string[]] {
  const all = [...keys];
  const markers = new Set(all.filter((k) => k.endsWith(MARKER) && k.length > MARKER.length));
  const named = new Set([...[...markers].map((k) => k.slice(0, -MARKER.length)),
    ...directories.filter((d) => d.length > 1).map((d) => d.slice(0, -1))]);
  const parents = new Set<string>();
  for (const k of [...all, ...named]) for (let i = k.indexOf("/"); i > 0; i = k.indexOf("/", i + 1)) parents.add(k.slice(0, i));
  return [markers, [...named].filter((d) => !parents.has(d)).sort(compareKeys)];
}

export const isXml = (key: string) => key === "manifest.safe" || key.endsWith(".xml") || key.endsWith(".xsd");

// ---------------------------------------------------------------- the product (§2.2–§2.5)

export interface Image {
  text: string; // the image file F
  key: string; // its band file, F.jp2
  basename: string;
}

export interface Geocoding {
  code: string;
  sizes: Map<number, [number, number]>; // r -> [NROWS, NCOLS]
  positions: Map<number, [number, number, number, number]>; // r -> [ULX, ULY, XDIM, YDIM]
  element: XmlElement;
}

export interface Product {
  level: "L1C" | "L2A";
  metadataKey: string;
  metadata: XmlElement;
  granule: string;
  images: Image[];
  tileKey: string;
  tile: XmlElement;
  geocoding: Geocoding;
}

function u32(t: string, what: string): number {
  if (!POSITIVE.test(t) || t.length > 10 || Number(t) > MAX_U32) {
    reject(`${what} ${JSON.stringify(t.slice(0, 40))} is not a positive integer below 2^32`);
  }
  return Number(t);
}

/** The product metadata's key (§2.2). */
export function productLevel(sizes: Map<string, number>): string {
  const found = Object.keys(LEVELS).filter((k) => sizes.has(k));
  if (found.length !== 1) {
    reject("not a SAFE product of the compact format: the root has " + (found.length
      ? "both MTD_MSIL1C.xml and MTD_MSIL2A.xml"
      : "no MTD_MSIL1C.xml or MTD_MSIL2A.xml (products before December 2016 are not supported)"));
  }
  if (!sizes.has("manifest.safe")) reject("a SAFE product has no manifest.safe");
  return found[0];
}

/** The product (§2.2–§2.4): `sizes` maps each object's key to its size, and `whole(key)` reads an object. */
export async function readProduct(sizes: Map<string, number>, whole: (k: string) => Promise<Uint8Array>): Promise<Product> {
  const key = productLevel(sizes);
  const [level, rootName, tileName] = LEVELS[key];
  if (sizes.get(key)! > MAX_XML) reject(`${key} is larger than 2^24 bytes`);
  const root = parseDocument(await whole(key), key);
  if (local(root.name) !== rootName) reject(`${key}'s root element is not ${rootName}`);
  const files = path(root, GRANULE).flatMap((granule) => kids(granule, "IMAGE_FILE"));
  if (!files.length) reject(`${key}: no granule has an IMAGE_FILE`);
  const images: Image[] = [];
  const seen = new Set<string>();
  let g: string | undefined;
  for (const el of files) {
    const f = layoutText(el, `${key}: an IMAGE_FILE`);
    const parts = f.split("/");
    if (parts.length < 4 || parts[0] !== "GRANULE" || parts.includes("")) {
      reject(`${key}: IMAGE_FILE ${JSON.stringify(f.slice(0, 200))} is not GRANULE/<g>/<directory>/<file>`);
    }
    if (g === undefined) g = parts[1];
    else if (parts[1] !== g) reject(`${key}: the image files are in two granule directories`);
    if (seen.has(f)) reject(`${key}: IMAGE_FILE ${JSON.stringify(f.slice(0, 200))} is listed twice`);
    seen.add(f);
    if (parts[2] !== "IMG_DATA") continue; // a DEM, cloud or snow image of PSD 14.2: not a band file (§2.3)
    if (!sizes.has(f + ".jp2")) reject(`the band file ${f.slice(0, 200)}.jp2 is not in the product`);
    images.push({ text: f, key: f + ".jp2", basename: parts[parts.length - 1] });
  }
  if (!images.length) reject(`${key}: no image file is in IMG_DATA`);
  const tileKey = `GRANULE/${g}/MTD_TL.xml`;
  if (!sizes.has(tileKey)) reject(`the tile metadata ${tileKey.slice(0, 200)} is not in the product`);
  if (sizes.get(tileKey)! > MAX_XML) reject("the tile metadata is larger than 2^24 bytes");
  const tile = parseDocument(await whole(tileKey), "MTD_TL.xml");
  if (local(tile.name) !== tileName) reject(`the tile metadata's root element is not ${tileName}`);
  return {
    level: level as "L1C" | "L2A", metadataKey: key, metadata: root, granule: g!, images, tileKey, tile,
    geocoding: geocoding(tile),
  };
}

/** The tile geocoding (§2.4). */
function geocoding(tile: XmlElement): Geocoding {
  const tg = one(path(tile, "Geometric_Info/Tile_Geocoding"), "MTD_TL.xml: Geometric_Info/Tile_Geocoding");
  const code = layoutText(one(kids(tg, "HORIZONTAL_CS_CODE"), "Tile_Geocoding/HORIZONTAL_CS_CODE"), "HORIZONTAL_CS_CODE");
  if (!/^EPSG:[0-9]+$/.test(code)) reject(`HORIZONTAL_CS_CODE ${JSON.stringify(code.slice(0, 40))} is not EPSG: and digits`);
  const sizes = new Map<number, [number, number]>();
  const texts = new Map<number, string>();
  const sizeEls = kids(tg, "Size");
  if (!sizeEls.length) reject("Tile_Geocoding has no Size");
  for (const el of sizeEls) {
    const rt = attr(el, "resolution");
    if (rt === undefined) reject("a Size has no resolution");
    const r = u32(rt!, "a Size's resolution");
    if (sizes.has(r)) reject(`two Sizes have the resolution ${r}`);
    const rows = u32(layoutText(one(kids(el, "NROWS"), "Size/NROWS"), "NROWS"), "NROWS");
    const cols = u32(layoutText(one(kids(el, "NCOLS"), "Size/NCOLS"), "NCOLS"), "NCOLS");
    sizes.set(r, [rows, cols]);
    texts.set(r, rt!);
  }
  const positions = new Map<number, [number, number, number, number]>();
  const geos = kids(tg, "Geoposition");
  for (const [r, rt] of texts) {
    const el = one(geos.filter((g) => attr(g, "resolution") === rt), `Geoposition of resolution ${rt}`);
    const vals = ["ULX", "ULY", "XDIM", "YDIM"].map((name) => {
      const t = layoutText(one(kids(el, name), `Geoposition/${name}`), name);
      const v = DECIMAL.test(t) ? Number(t) : NaN;
      if (!Number.isFinite(v)) reject(`Geoposition ${name} ${JSON.stringify(t.slice(0, 40))} is not a finite decimal number`);
      return v;
    }) as [number, number, number, number];
    if (vals[2] === 0 || vals[3] === 0) reject("a Geoposition's XDIM or YDIM is 0");
    positions.set(r, vals);
  }
  return { code, sizes, positions, element: tg };
}

/** A band file's band name (§2.5). */
export function bandName(basename: string, r: number): string {
  let b = basename;
  const suffix = `_${r}m`;
  if (b.endsWith(suffix)) b = b.slice(0, -suffix.length);
  const name = b.slice(b.lastIndexOf("_") + 1);
  if (!/^[A-Za-z0-9]+$/.test(name)) {
    reject(`the band name ${JSON.stringify(name.slice(0, 40))} of ${JSON.stringify(basename.slice(0, 100))} is not ASCII letters and digits`);
  }
  return name;
}

/** The band file's resolution: the one Size of its image size (§2.5). */
export function resolution(geo: Geocoding, width: number, height: number, key: string): number {
  const found = [...geo.sizes].filter(([, [rows, cols]]) => rows === height && cols === width).map(([r]) => r);
  if (found.length !== 1) {
    reject(`the band file ${key.slice(0, 200)} (${width} × ${height}) matches ${found.length ? "several" : "no"} resolution of the tile geocoding`);
  }
  return found[0];
}

// The GeoZarr attributes and the source metadata of a SAFE product
// (conventions/safe/README.md §5, §6).

import { jsonNumber } from "../common.ts";
import type { XmlElement } from "../store.ts";
import { attr, DECIMAL, type Geocoding, kids, path, type Product, text } from "./product.ts";

type Json = unknown;
type Obj = Record<string, Json>;

// The zarr-conventions CMOs (§5), at tag v0.1.
export const PROJ = {
  uuid: "f17cb550-5864-4468-aeb7-f3180cfb622f",
  schema_url: "https://raw.githubusercontent.com/zarr-conventions/proj/refs/tags/v0.1/schema.json",
  spec_url: "https://github.com/zarr-conventions/proj/blob/v0.1/README.md",
  name: "proj",
  description: "Coordinate reference system information for geospatial data",
};
export const SPATIAL = {
  uuid: "689b58e2-cf7b-45e0-9fff-9cfc0883d6b4",
  schema_url: "https://raw.githubusercontent.com/zarr-conventions/spatial/refs/tags/v0.1/schema.json",
  spec_url: "https://github.com/zarr-conventions/spatial/blob/v0.1/README.md",
  name: "spatial",
  description: "Spatial coordinate information",
};
export const MULTISCALES = {
  uuid: "d35379db-88df-4056-af3a-620245f8e347",
  schema_url: "https://raw.githubusercontent.com/zarr-conventions/multiscales/refs/tags/v0.1/schema.json",
  spec_url: "https://github.com/zarr-conventions/multiscales/blob/v0.1/README.md",
  name: "multiscales",
  description: "Multiscale layout of zarr datasets",
};

const DEG = 'ANGLEUNIT["degree",0.0174532925199433]';
const M = 'LENGTHUNIT["metre",1]';

/** `proj:wkt2` for a WGS 84 / UTM zone's CRS code (§5.2), else undefined. */
export function wkt2(code: string): string | undefined {
  const m = /^EPSG:([1-9][0-9]{4})$/.exec(code);
  if (m === null) return undefined;
  const c = Number(m[1]);
  let z: number, h: string;
  if (c >= 32601 && c <= 32660) [z, h] = [c - 32600, "N"];
  else if (c >= 32701 && c <= 32760) [z, h] = [c - 32700, "S"];
  else return undefined;
  const members = ["Transit", "G730", "G873", "G1150", "G1674", "G1762", "G2139", "G2296"]
    .map((g) => `MEMBER["World Geodetic System 1984 (${g})"]`).join(",");
  return `PROJCRS["WGS 84 / UTM zone ${z}${h}",BASEGEOGCRS["WGS 84",ENSEMBLE["World Geodetic System 1984 ensemble",` +
    `${members},ELLIPSOID["WGS 84",6378137,298.257223563,${M}],ENSEMBLEACCURACY[2.0]],` +
    `PRIMEM["Greenwich",0,${DEG}],ID["EPSG",4326]],` +
    `CONVERSION["UTM zone ${z}${h}",METHOD["Transverse Mercator",ID["EPSG",9807]],` +
    `PARAMETER["Latitude of natural origin",0,${DEG},ID["EPSG",8801]],` +
    `PARAMETER["Longitude of natural origin",${6 * z - 183},${DEG},ID["EPSG",8802]],` +
    `PARAMETER["Scale factor at natural origin",0.9996,SCALEUNIT["unity",1],ID["EPSG",8805]],` +
    `PARAMETER["False easting",500000,${M},ID["EPSG",8806]],` +
    `PARAMETER["False northing",${h === "N" ? 0 : 10000000},${M},ID["EPSG",8807]]],` +
    `CS[Cartesian,2],AXIS["(E)",east,ORDER[1],${M}],AXIS["(N)",north,ORDER[2],${M}],ID["EPSG",${c}]]`;
}

// ---------------------------------------------------------------- the grids (§5.1)

/** [transform, shape, bounding box] of resolution r (§5.1). */
export function gridOf(geo: Geocoding, r: number): [number[], number[], number[]] {
  const [rows, cols] = geo.sizes.get(r)!;
  const [ulx, uly, xdim, ydim] = geo.positions.get(r)!;
  const x1 = ulx + xdim * cols, y1 = uly + ydim * rows;
  return [[xdim, 0, ulx, 0, ydim, uly], [rows, cols],
    [Math.min(ulx, x1), Math.min(uly, y1), Math.max(ulx, x1), Math.max(uly, y1)]];
}

function crs(geo: Geocoding): Obj {
  const w = wkt2(geo.code);
  return w === undefined ? { "proj:code": geo.code } : { "proj:code": geo.code, "proj:wkt2": w };
}

/** A resolution group's attributes (§5.2). */
export function groupAttributes(geo: Geocoding, r: number): Obj {
  const [transform, shape, bbox] = gridOf(geo, r);
  return {
    zarr_conventions: [PROJ, SPATIAL], ...crs(geo), "spatial:dimensions": ["y", "x"],
    "spatial:transform": transform, "spatial:shape": shape, "spatial:bbox": bbox, "spatial:registration": "pixel",
  };
}

/** The root's GeoZarr CMOs and members (§5.3). */
export function rootGeozarr(product: Product, resolutions: number[]): [Obj[], Obj] {
  const geo = product.geocoding;
  const members: Obj = { ...crs(geo), "spatial:bbox": gridOf(geo, resolutions[0])[2] };
  if (product.level !== "L2A") return [[PROJ, SPATIAL], members];
  members["spatial:dimensions"] = ["y", "x"];
  members["spatial:registration"] = "pixel";
  members.multiscales = {
    layout: resolutions.map((r) => ({ asset: `r${r}m`, "spatial:shape": gridOf(geo, r)[1], "spatial:transform": gridOf(geo, r)[0] })),
  };
  return [[MULTISCALES, PROJ, SPATIAL], members];
}

// ---------------------------------------------------------------- values (§6.1)

const MAX_SAFE = BigInt(Number.MAX_SAFE_INTEGER);

/** A text as a value (§6.1): a decimal number as a JSON number, else the string. */
export function typed(t: string): Json {
  if (!DECIMAL.test(t)) return t;
  if (/[.eE]/.test(t)) return jsonNumber(Number(t));
  const sign = t[0] === "-" ? "-" : "";
  const digits = t.replace(/^[+-]/, "").replace(/^0+/, "") || "0";
  if (digits.length > 16 || BigInt(digits) > MAX_SAFE) return sign + digits;
  return Number(sign + digits) + 0; // + 0: -0 is 0
}

function value(els: XmlElement[]): Json {
  if (els.length !== 1) return undefined;
  const t = text(els[0]);
  return t === undefined ? undefined : typed(t);
}

function string(els: XmlElement[]): string | undefined {
  return els.length === 1 ? text(els[0]) : undefined;
}

/** A measure (§6.1): {value, unit} when the element has a unit, else its value. */
function measure(els: XmlElement[]): Json {
  const v = value(els);
  if (v === undefined) return undefined;
  const unit = attr(els[0], "unit");
  return unit === undefined ? v : { value: v, unit };
}

/** An index attribute read as an integer: one to nine digits. */
function index(t: string | undefined): number | undefined {
  return t !== undefined && /^[0-9]{1,9}$/.test(t) ? Number(t) : undefined;
}

const byIndex = (els: XmlElement[], name: string, i: number) => els.filter((e) => index(attr(e, name)) === i);

function put(out: Obj, key: string, v: Json): void {
  if (v === undefined) return;
  if (Array.isArray(v) && v.length === 0) return;
  if (v !== null && typeof v === "object" && !Array.isArray(v) && Object.keys(v).length === 0) return;
  out[key] = v;
}

/** The most elements of a list copied as records (§6.2, §6.3). */
export const MAX_RECORDS = 64;

/** One record per element, or undefined (absent) when there are more than MAX_RECORDS. */
function records(els: XmlElement[], names: string[]): Obj[] | undefined {
  if (els.length > MAX_RECORDS) return undefined;
  return els.map((el) => {
    const rec: Obj = {};
    for (const n of names) put(rec, n, value(kids(el, n)));
    return rec;
  });
}

function characteristics(product: Product): XmlElement | undefined {
  const found = path(product.metadata, "General_Info/Product_Image_Characteristics");
  return found.length === 1 ? found[0] : undefined;
}

/** A band array's source metadata (§6.2). */
export function bandMetadata(product: Product, imageText: string, name: string, siz: string): Obj {
  const s: Obj = { IMAGE_FILE: imageText };
  const x = characteristics(product);
  const l2a = product.level === "L2A";
  const m = /^B([0-9])([0-9])$/.exec(name);
  const pb = m ? "B" + (m[1] === "0" ? m[2] : m[1] + m[2]) : name === "B8A" ? "B8A" : undefined;
  if (x !== undefined && pb !== undefined) {
    const infos = path(x, "Spectral_Information_List/Spectral_Information")
      .filter((e) => attr(e, "physicalBand") === pb && index(attr(e, "bandId")) !== undefined);
    if (infos.length === 1) {
      const info = infos[0];
      const i = index(attr(info, "bandId"))!;
      s.bandId = i;
      s.physicalBand = pb;
      put(s, "RESOLUTION", value(kids(info, "RESOLUTION")));
      const wl = kids(info, "Wavelength");
      if (wl.length === 1) {
        const w: Obj = {};
        for (const n of ["MIN", "MAX", "CENTRAL"]) put(w, n, measure(kids(wl[0], n)));
        put(s, "Wavelength", w);
      }
      put(s, "PHYSICAL_GAINS", value(byIndex(kids(x, "PHYSICAL_GAINS"), "bandId", i)));
      put(s, "SOLAR_IRRADIANCE", measure(byIndex(path(x, "Reflectance_Conversion/Solar_Irradiance_List/SOLAR_IRRADIANCE"), "bandId", i)));
      if (l2a) {
        put(s, "BOA_QUANTIFICATION_VALUE", measure(path(x, "QUANTIFICATION_VALUES_LIST/BOA_QUANTIFICATION_VALUE")));
        put(s, "BOA_ADD_OFFSET", value(byIndex(path(x, "BOA_ADD_OFFSET_VALUES_LIST/BOA_ADD_OFFSET"), "band_id", i)));
      } else {
        put(s, "QUANTIFICATION_VALUE", measure(kids(x, "QUANTIFICATION_VALUE")));
        put(s, "RADIO_ADD_OFFSET", value(byIndex(path(x, "Radiometric_Offset_List/RADIO_ADD_OFFSET"), "band_id", i)));
      }
    }
  }
  if (x !== undefined && l2a) {
    if (name === "AOT" || name === "WVP") {
      put(s, `${name}_QUANTIFICATION_VALUE`, measure(path(x, `QUANTIFICATION_VALUES_LIST/${name}_QUANTIFICATION_VALUE`)));
    } else if (name === "SCL") {
      put(s, "Scene_Classification_List", records(path(x, "Scene_Classification_List/Scene_Classification_ID"),
        ["SCENE_CLASSIFICATION_TEXT", "SCENE_CLASSIFICATION_INDEX"]));
    }
  }
  s.siz = siz;
  return s;
}

/** The root's source metadata (§6.3). */
export function rootMetadata(product: Product): Obj {
  const s: Obj = {};
  const info = path(product.metadata, "General_Info/Product_Info");
  for (const n of ["PRODUCT_URI", "PROCESSING_LEVEL", "PRODUCT_TYPE", "PROCESSING_BASELINE"]) {
    put(s, n, string(info.flatMap((e) => kids(e, n))));
  }
  const x = characteristics(product);
  if (x !== undefined) {
    put(s, "Special_Values", records(kids(x, "Special_Values"), ["SPECIAL_VALUE_TEXT", "SPECIAL_VALUE_INDEX"]));
    put(s, "U", value(path(x, "Reflectance_Conversion/U")));
  }
  for (const n of ["TILE_ID", "SENSING_TIME"]) put(s, n, string(path(product.tile, `General_Info/${n}`)));
  for (const n of ["HORIZONTAL_CS_NAME", "HORIZONTAL_CS_CODE"]) put(s, n, string(kids(product.geocoding.element, n)));
  return s;
}

// ---------------------------------------------------------------- vzip_source's budget (§6.3)

export const BUDGET = 65536;
const utf8 = new TextEncoder();

/** The size of a value's JSON, as JSON.stringify writes it without whitespace, in UTF-8. */
export const jsonSize = (v: Json) => utf8.encode(JSON.stringify(v)).length;

/** The largest XML document kept as text. */
export const MAX_TEXT = 1 << 16;
/** The candidates read together. */
const READ_AHEAD = 16;

/** The XML documents kept as text, with their text values (§6.3): `xmlSizes` maps each XML
 * document's key to its size, `readTexts(keys)` reads the text values of candidates (documents
 * of at most 65536 bytes), `lists` holds the other members of `S` (`empty`, `empty_dirs`,
 * `ignored`), which never move, and `order` compares keys. The candidates are read in the
 * order they are taken, at most READ_AHEAD at a time, and no longer once the next one cannot
 * fit (§12.2): `S` with only its texts and `lists`, J bytes of JSON, and a candidate of n
 * bytes as text is more than J + n bytes. */
export async function chooseTexts(
  xmlSizes: Map<string, number>, readTexts: (keys: string[]) => Promise<Json[]>, lists: [string, string[]][],
  order: (a: string, b: string) => number,
): Promise<Map<string, Json>> {
  const keySize = new Map([...xmlSizes.keys()].map((k) => [k, jsonSize(k)]));
  const fixed = lists.filter(([, v]) => v.length)
    .map(([n, v]) => jsonSize(n) + 1 + 2 + v.reduce((a, k) => a + jsonSize(k), 0) + v.length - 1);
  const nameXml = jsonSize("xml"), nameArrays = jsonSize("xml_arrays");
  const texts = new Map<string, Json>();
  let xmlTotal = 0;
  let arraysTotal = [...keySize.values()].reduce((a, b) => a + b, 0) + keySize.size - 1;
  let nArrays = keySize.size;
  const size = (xt: number, nText: number, at: number, na: number) => {
    const members = [...fixed];
    if (nText) members.push(nameXml + 1 + 2 + xt);
    if (na) members.push(nameArrays + 1 + 2 + at);
    return 2 + members.reduce((a, b) => a + b, 0) + Math.max(0, members.length - 1);
  };
  // Could the candidate k still become text?
  const fits = (k: string) => size(xmlTotal, texts.size, 0, 0) + xmlSizes.get(k)! <= BUDGET;
  const candidates = [...xmlSizes.keys()].filter((k) => xmlSizes.get(k)! <= MAX_TEXT)
    .sort((a, b) => xmlSizes.get(a)! - xmlSizes.get(b)! || order(a, b));
  let i = 0;
  while (i < candidates.length && fits(candidates[i])) {
    const batch: string[] = [];
    while (i + batch.length < candidates.length && batch.length < READ_AHEAD && fits(candidates[i + batch.length])) {
      batch.push(candidates[i + batch.length]);
    }
    const values = await readTexts(batch);
    for (const [j, k] of batch.entries()) {
      i++;
      if (!fits(k)) break;
      const xt = xmlTotal + keySize.get(k)! + 1 + jsonSize(values[j]) + (texts.size ? 1 : 0);
      const at = arraysTotal - keySize.get(k)! - (nArrays > 1 ? 1 : 0);
      if (size(xt, texts.size + 1, at, nArrays - 1) <= BUDGET) {
        texts.set(k, values[j]);
        [xmlTotal, arraysTotal, nArrays] = [xt, at, nArrays - 1];
      }
    }
  }
  return texts;
}

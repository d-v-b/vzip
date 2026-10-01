// Strict JSON parsing for the conformance harness, from impls/typescript/src/json.ts.

// A strict RFC 8259 JSON parser that rejects duplicate member names and keeps
// track of whether a number was written as an integer (HARNESS.md).

export type JNum = { readonly $num: true; raw: string; int: bigint | null };
export type JValue = null | boolean | string | JNum | JValue[] | { [k: string]: JValue };

export class JsonError extends Error {}

export function isNum(v: unknown): v is JNum {
  return typeof v === "object" && v !== null && (v as JNum).$num === true;
}

export function isObj(v: unknown): v is { [k: string]: JValue } {
  return typeof v === "object" && v !== null && !Array.isArray(v) && !isNum(v);
}

export function parseJson(text: string): JValue {
  let i = 0;
  const ws = () => {
    while (i < text.length && " \t\n\r".includes(text[i])) i++;
  };
  const fail = (m: string): never => {
    throw new JsonError(`${m} at offset ${i}`);
  };
  const value = (depth: number): JValue => {
    if (depth > 512) fail("nesting too deep");
    ws();
    const c = text[i];
    if (c === "{") {
      i++;
      const obj: { [k: string]: JValue } = Object.create(null);
      const seen = new Set<string>();
      ws();
      if (text[i] === "}") {
        i++;
        return obj;
      }
      for (;;) {
        ws();
        if (text[i] !== '"') fail("expected member name");
        const k = str();
        if (seen.has(k)) fail(`duplicate member name ${JSON.stringify(k)}`);
        seen.add(k);
        ws();
        if (text[i] !== ":") fail("expected ':'");
        i++;
        obj[k] = value(depth + 1);
        ws();
        if (text[i] === ",") {
          i++;
          continue;
        }
        if (text[i] === "}") {
          i++;
          return obj;
        }
        fail("expected ',' or '}'");
      }
    }
    if (c === "[") {
      i++;
      const arr: JValue[] = [];
      ws();
      if (text[i] === "]") {
        i++;
        return arr;
      }
      for (;;) {
        arr.push(value(depth + 1));
        ws();
        if (text[i] === ",") {
          i++;
          continue;
        }
        if (text[i] === "]") {
          i++;
          return arr;
        }
        fail("expected ',' or ']'");
      }
    }
    if (c === '"') return str();
    if (text.startsWith("true", i)) {
      i += 4;
      return true;
    }
    if (text.startsWith("false", i)) {
      i += 5;
      return false;
    }
    if (text.startsWith("null", i)) {
      i += 4;
      return null;
    }
    const m = /^-?(?:0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?/.exec(text.slice(i, i + 400));
    if (!m) fail("unexpected character");
    i += m![0].length;
    const isInt = m![1] === undefined && m![2] === undefined;
    return { $num: true, raw: m![0], int: isInt ? BigInt(m![0]) : null };
  };
  const str = (): string => {
    i++; // opening quote
    let out = "";
    for (;;) {
      if (i >= text.length) fail("unterminated string");
      const c = text[i];
      const code = c.charCodeAt(0);
      if (c === '"') {
        i++;
        return out;
      }
      if (code < 0x20) fail("control character in string");
      if (c === "\\") {
        const e = text[i + 1];
        i += 2;
        switch (e) {
          case '"': out += '"'; break;
          case "\\": out += "\\"; break;
          case "/": out += "/"; break;
          case "b": out += "\b"; break;
          case "f": out += "\f"; break;
          case "n": out += "\n"; break;
          case "r": out += "\r"; break;
          case "t": out += "\t"; break;
          case "u": {
            const h = text.slice(i, i + 4);
            if (!/^[0-9a-fA-F]{4}$/.test(h)) fail("bad \\u escape");
            out += String.fromCharCode(parseInt(h, 16));
            i += 4;
            break;
          }
          default:
            fail("bad escape");
        }
        continue;
      }
      out += c;
      i++;
    }
  };
  const v = value(0);
  ws();
  if (i !== text.length) fail("trailing data");
  return v;
}

/** Decodes a JSON file's bytes strictly as UTF-8. */
export function decodeJsonBytes(b: Buffer): string {
  const s = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(b);
  return s;
}

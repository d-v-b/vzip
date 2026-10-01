// A small strict JSON parser that keeps numbers as text, so that `1` and `1.0` can be told apart.

export class JNum {
  text: string;
  constructor(text: string) {
    this.text = text;
  }
  get isInteger(): boolean {
    return /^-?(0|[1-9][0-9]*)$/.test(this.text);
  }
  toBigInt(): bigint {
    return BigInt(this.text);
  }
}

export type JValue = null | boolean | string | JNum | JValue[] | { [k: string]: JValue };

export function parseJson(src: string): JValue {
  let i = 0;
  const ws = () => {
    while (i < src.length && " \t\n\r".includes(src[i])) i++;
  };
  const fail = (m: string): never => {
    throw new SyntaxError(`JSON: ${m} at offset ${i}`);
  };
  const value = (): JValue => {
    ws();
    const c = src[i];
    if (c === "{") {
      i++;
      const obj: { [k: string]: JValue } = Object.create(null);
      ws();
      if (src[i] === "}") {
        i++;
        return obj;
      }
      for (;;) {
        ws();
        if (src[i] !== '"') fail("expected string key");
        const k = str();
        ws();
        if (src[i] !== ":") fail("expected ':'");
        i++;
        obj[k] = value();
        ws();
        if (src[i] === ",") {
          i++;
          continue;
        }
        if (src[i] === "}") {
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
      if (src[i] === "]") {
        i++;
        return arr;
      }
      for (;;) {
        arr.push(value());
        ws();
        if (src[i] === ",") {
          i++;
          continue;
        }
        if (src[i] === "]") {
          i++;
          return arr;
        }
        fail("expected ',' or ']'");
      }
    }
    if (c === '"') return str();
    if (src.startsWith("true", i)) {
      i += 4;
      return true;
    }
    if (src.startsWith("false", i)) {
      i += 5;
      return false;
    }
    if (src.startsWith("null", i)) {
      i += 4;
      return null;
    }
    const m = /^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?/.exec(src.slice(i));
    if (m && m[0].length > 0 && m[0] !== "-") {
      i += m[0].length;
      return new JNum(m[0]);
    }
    return fail("unexpected character");
  };
  const str = (): string => {
    i++; // opening quote
    let out = "";
    for (;;) {
      if (i >= src.length) fail("unterminated string");
      const c = src[i++];
      if (c === '"') return out;
      if (c === "\\") {
        const e = src[i++];
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
            const h = src.slice(i, i + 4);
            if (!/^[0-9a-fA-F]{4}$/.test(h)) fail("bad \\u escape");
            out += String.fromCharCode(parseInt(h, 16));
            i += 4;
            break;
          }
          default:
            fail("bad escape");
        }
      } else {
        if (c.charCodeAt(0) < 0x20) fail("control character in string");
        out += c;
      }
    }
  };
  const v = value();
  ws();
  if (i !== src.length) fail("trailing data");
  return v;
}

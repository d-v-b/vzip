// Minimal XML element/attribute scanner for OME-XML (VIRTUALIZE.md §3.2).
// Builds an element tree; text content is ignored.

import { reject } from "./util.ts";

export type XmlElement = {
  name: string; // local name (namespace prefix removed)
  attrs: Map<string, string>;
  children: XmlElement[];
};

export function decodeEntities(s: string): string {
  return s.replace(/&(#x[0-9a-fA-F]+|#[0-9]+|lt|gt|amp|quot|apos);/g, (_m, g: string) => {
    if (g === "lt") return "<";
    if (g === "gt") return ">";
    if (g === "amp") return "&";
    if (g === "quot") return '"';
    if (g === "apos") return "'";
    const cp = g[1] === "x" ? parseInt(g.slice(2), 16) : parseInt(g.slice(1), 10);
    if (cp > 0x10ffff) reject(`bad character reference &${g};`);
    return String.fromCodePoint(cp);
  });
}

function localName(qname: string): string {
  const i = qname.indexOf(":");
  return i >= 0 ? qname.slice(i + 1) : qname;
}

export function parseXml(text: string): XmlElement {
  const root: XmlElement = { name: "#document", attrs: new Map(), children: [] };
  const stack: XmlElement[] = [root];
  let i = 0;
  const n = text.length;
  while (i < n) {
    const lt = text.indexOf("<", i);
    if (lt < 0) break;
    if (text.startsWith("<!--", lt)) {
      const e = text.indexOf("-->", lt + 4);
      if (e < 0) reject("unterminated XML comment");
      i = e + 3;
    } else if (text.startsWith("<![CDATA[", lt)) {
      const e = text.indexOf("]]>", lt + 9);
      if (e < 0) reject("unterminated CDATA");
      i = e + 3;
    } else if (text.startsWith("<?", lt)) {
      const e = text.indexOf("?>", lt + 2);
      if (e < 0) reject("unterminated processing instruction");
      i = e + 2;
    } else if (text.startsWith("<!", lt)) {
      // DOCTYPE, possibly with an internal subset in [...]
      let j = lt + 2;
      let depth = 0;
      while (j < n) {
        const ch = text[j];
        if (ch === "[") depth++;
        else if (ch === "]") depth--;
        else if (ch === ">" && depth <= 0) break;
        j++;
      }
      i = j + 1;
    } else if (text[lt + 1] === "/") {
      const e = text.indexOf(">", lt);
      if (e < 0) reject("unterminated end tag");
      const name = localName(text.slice(lt + 2, e).trim());
      const top = stack[stack.length - 1];
      if (stack.length <= 1 || top.name !== name) reject(`mismatched XML end tag </${name}>`);
      stack.pop();
      i = e + 1;
    } else {
      // start tag
      let j = lt + 1;
      const nameStart = j;
      while (j < n && !/[\s/>]/.test(text[j])) j++;
      const el: XmlElement = { name: localName(text.slice(nameStart, j)), attrs: new Map(), children: [] };
      let selfClose = false;
      for (;;) {
        while (j < n && /\s/.test(text[j])) j++;
        if (j >= n) reject("unterminated start tag");
        if (text[j] === ">") {
          j++;
          break;
        }
        if (text[j] === "/" && text[j + 1] === ">") {
          selfClose = true;
          j += 2;
          break;
        }
        const an = j;
        while (j < n && !/[\s=/>]/.test(text[j])) j++;
        const aname = text.slice(an, j);
        while (j < n && /\s/.test(text[j])) j++;
        if (text[j] !== "=") reject(`attribute ${aname} without value`);
        j++;
        while (j < n && /\s/.test(text[j])) j++;
        const q = text[j];
        if (q !== '"' && q !== "'") reject(`unquoted attribute ${aname}`);
        const e = text.indexOf(q, j + 1);
        if (e < 0) reject("unterminated attribute value");
        if (!el.attrs.has(aname)) el.attrs.set(aname, decodeEntities(text.slice(j + 1, e)));
        j = e + 1;
      }
      stack[stack.length - 1].children.push(el);
      if (!selfClose) stack.push(el);
      i = j;
    }
  }
  return root;
}

export function findFirst(el: XmlElement, name: string): XmlElement | undefined {
  for (const c of el.children) {
    if (c.name === name) return c;
    const f = findFirst(c, name);
    if (f) return f;
  }
  return undefined;
}

export function findAll(el: XmlElement, name: string, out: XmlElement[] = []): XmlElement[] {
  for (const c of el.children) {
    if (c.name === name) out.push(c);
    findAll(c, name, out);
  }
  return out;
}

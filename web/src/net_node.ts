// `#net` under Node (package.json "imports"): requests whose connections are
// checked by rule 3 of spec §8.7, as the Python reader does. Each host name is
// resolved here, every address is checked, and the socket connects only to an
// address that passed, so a name whose resolution changes between check and
// connection (DNS rebinding) cannot reach a refused address; `Host` and the
// TLS server name stay the URL's host. A kept-alive connection's peer is
// checked again before each request on it. Redirects are followed here, each
// target checked by the policy before it is requested, at most 5 (§6.2). A
// response's body streams: a caller that cancels it unread (such as a 200 that is
// the whole object) closes the connection without reading it.

import dns from "node:dns";
import http from "node:http";
import https from "node:https";
import net from "node:net";
import type { Socket } from "node:net";
import { Readable } from "node:stream";
import type { CheckedFetch } from "./http.ts";
import { isUriReference } from "./uri.ts";

/** Resolves host names (replaceable in tests). */
export const resolver = {
  lookup: (host: string): Promise<{ address: string; family: number }[]> =>
    dns.promises.lookup(host, { all: true, verbatim: true }),
};

const REDIRECTS = new Set([301, 302, 303, 307, 308]);

/** The proxy Node's `fetch` would use for `url` (NODE_USE_ENV_PROXY with HTTP_PROXY,
 * HTTPS_PROXY and NO_PROXY, in either case), if any. */
export function envProxy(url: URL, env: NodeJS.ProcessEnv = process.env): string | undefined {
  if (!["1", "true"].includes((env.NODE_USE_ENV_PROXY ?? "").toLowerCase())) return undefined;
  const get = (name: string) => env[name.toLowerCase()] ?? env[name.toUpperCase()];
  const proxy = url.protocol === "https:" ? get("https_proxy") : get("http_proxy");
  if (!proxy) return undefined;
  const host = url.hostname.toLowerCase().replace(/^\[|\]$/g, "");
  const port = url.port || (url.protocol === "https:" ? "443" : "80");
  for (const raw of (get("no_proxy") ?? "").split(/[\s,]+/)) {
    const entry = raw.trim().toLowerCase();
    if (entry === "") continue;
    if (entry === "*") return undefined;
    const m = entry.match(/^(?:\*?\.)?(\[[^\]]*\]|[^:]+)(?::(\d+))?$/);
    if (!m) continue;
    const name = m[1].replace(/^\[|\]$/g, "");
    if (m[2] !== undefined && m[2] !== port) continue;
    if (host === name || host.endsWith(`.${name}`)) return undefined;
  }
  return proxy;
}

class CheckError extends Error {}

export function checkedFetch(
  check: (url: string, address: string) => void,
  policy: { allowUncheckedProxy?: boolean },
): CheckedFetch {
  // Connections are pooled per archive, and every one is checked.
  const agents = {
    "http:": new http.Agent({ keepAlive: true }),
    "https:": new https.Agent({ keepAlive: true }),
  };

  const send = (url: URL, headers: Record<string, string>, signal?: AbortSignal) =>
    new Promise<{ status: number; headers: Headers; locations: string[]; body: http.IncomingMessage }>((resolve, reject) => {
      const target = url.href;
      const lookup: net.LookupFunction = (hostname, options, callback) => {
        resolver.lookup(hostname).then(
          (addresses) => {
            const ok: { address: string; family: number }[] = [];
            let refused: unknown;
            for (const a of addresses) {
              try {
                check(target, a.address);
                ok.push(a);
              } catch (e) {
                refused ??= e;
              }
            }
            if (ok.length === 0) {
              (callback as (e: Error) => void)(new CheckError((refused as Error)?.message ?? `${hostname}: no address`));
            } else if ((options as { all?: boolean }).all) {
              (callback as (e: null, a: typeof ok) => void)(null, ok);
            } else {
              (callback as (e: null, a: string, f: number) => void)(null, ok[0].address, ok[0].family);
            }
          },
          (e: Error) => (callback as (e: Error) => void)(e),
        );
      };
      const literal = url.hostname.replace(/^\[|\]$/g, "");
      if (net.isIP(literal)) {
        try {
          check(target, literal); // net.connect does not look literals up
        } catch (e) {
          reject(e);
          return;
        }
      }
      const request = (url.protocol === "https:" ? https : http).request(url, {
        method: "GET",
        headers,
        agent: agents[url.protocol as "http:" | "https:"],
        lookup,
        signal,
      });
      request.on("socket", (socket: Socket) => {
        // A kept-alive socket is already connected: check its peer before the request is written.
        if (socket.remoteAddress !== undefined) {
          try {
            check(target, socket.remoteAddress);
          } catch (e) {
            request.destroy(new CheckError((e as Error).message));
          }
        }
      });
      request.on("error", reject);
      request.on("response", (response) => {
        const h = new Headers();
        const locations: string[] = [];
        const raw = response.rawHeaders;
        for (let i = 0; i + 1 < raw.length; i += 2) {
          h.append(raw[i], raw[i + 1]);
          if (raw[i].toLowerCase() === "location") locations.push(raw[i + 1]);
        }
        resolve({ status: response.statusCode ?? 0, headers: h, locations, body: response });
      });
      request.end();
    });

  return async (url, init, allow) => {
    let current = new URL(url);
    for (let redirects = 0; ; redirects++) {
      const proxy = envProxy(current);
      if (proxy !== undefined) {
        if (!policy.allowUncheckedProxy) {
          throw new Error(
            `${current.href}: the request would go through the proxy ${proxy}, and the reader cannot check ` +
              "the address the proxy connects to (spec §8.7); set allowUncheckedProxy: true to send it anyway, " +
              "or exempt the host with NO_PROXY",
          );
        }
        // Check what the name resolves to here: literal addresses and honest names, not rebinding.
        const host = current.hostname.replace(/^\[|\]$/g, "");
        for (const a of net.isIP(host) ? [{ address: host }] : await resolver.lookup(host)) check(current.href, a.address);
        return fetch(current.href, { ...init, redirect: "follow" }); // Node's fetch uses the proxy
      }
      const r = await send(current, init.headers, init.signal);
      if (!REDIRECTS.has(r.status)) {
        const empty = [101, 204, 205, 304].includes(r.status);
        if (empty) r.body.resume();
        const body = empty ? null : (Readable.toWeb(r.body) as unknown as ReadableStream<Uint8Array>);
        const response = new Response(body, {
          status: r.status,
          headers: r.headers,
        });
        Object.defineProperty(response, "url", { value: current.href });
        Object.defineProperty(response, "redirected", { value: redirects > 0 });
        return response;
      }
      r.body.resume(); // a redirect's body is drained, so its connection can be reused
      if (r.locations.length !== 1) throw new Error(`${current.href}: HTTP ${r.status} with ${r.locations.length} Location fields`);
      if (!isUriReference(r.locations[0])) throw new Error(`redirect with an invalid Location: ${JSON.stringify(r.locations[0])}`);
      const next = new URL(r.locations[0], current);
      next.hash = "";
      if (next.protocol !== "http:" && next.protocol !== "https:") {
        throw new Error(`redirect to a non-http URL: ${next.href}`);
      }
      allow?.(next.href); // before the request to it (§6.2)
      if (redirects === 5) throw new Error(`${url}: more than 5 redirects`);
      current = next;
    }
  };
}

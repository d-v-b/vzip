// `#irwasm` in a browser (package.json "imports"): the Rust core's wasm32 build,
// which build.mjs copies next to the bundles (dist/vzip_ir.wasm). It is fetched
// relative to the running script (a service worker's location, or a page's), or
// from the URL `setIrWasmUrl` gives, and compiled while it streams.

let url: string | URL | undefined;

/** Where to fetch the module from, in place of `vzip_ir.wasm` next to the script. */
export function setIrWasmUrl(u: string | URL): void {
  url = u;
}

/** Compiles the module. */
export async function compileIr(): Promise<WebAssembly.Module> {
  const at = url ?? new URL("vzip_ir.wasm", (globalThis as { location?: { href: string } }).location?.href);
  const response = await fetch(at);
  if (!response.ok) throw new Error(`cannot fetch the IR core's wasm module ${String(at)}: HTTP ${response.status}`);
  if ((response.headers.get("Content-Type") ?? "").split(";")[0].trim() === "application/wasm") {
    return WebAssembly.compileStreaming(response);
  }
  return WebAssembly.compile(await response.arrayBuffer());
}

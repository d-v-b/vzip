// Serves the demo (dist/) at / and the built Neuroglancer fork at
// /neuroglancer/, on one origin so the service worker controls both.
// The fork (https://github.com/d-v-b/neuroglancer, branch vzip) is read from
// $NEUROGLANCER, by default a clone next to this repository (../neuroglancer),
// after `npm run build` there.
// Usage: node js/demo/serve.mjs [port]

import fs from "node:fs";
import http from "node:http";
import path from "node:path";

const here = path.dirname(new URL(import.meta.url).pathname);
const roots = [
  ["/neuroglancer/", path.join(process.env.NEUROGLANCER ?? path.join(here, "../../../neuroglancer"), "dist/client")],
  ["/", path.join(here, "../dist")],
];
const TYPES = {
  ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
  ".map": "application/json", ".wasm": "application/wasm", ".json": "application/json",
  ".svg": "image/svg+xml", ".png": "image/png",
};

const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://localhost");
  const [mount, root] = roots.find(([m]) => url.pathname.startsWith(m));
  let file = path.join(root, decodeURIComponent(url.pathname.slice(mount.length)));
  if (!file.startsWith(root)) return res.writeHead(403).end();
  if (fs.existsSync(file) && fs.statSync(file).isDirectory()) file = path.join(file, "index.html");
  if (!fs.existsSync(file)) return res.writeHead(404).end("not found");
  res.writeHead(200, { "Content-Type": TYPES[path.extname(file)] ?? "application/octet-stream" });
  fs.createReadStream(file).pipe(res);
});
server.listen(Number(process.argv[2] ?? 8080), "127.0.0.1", () => {
  console.log(`http://127.0.0.1:${server.address().port}/`);
});

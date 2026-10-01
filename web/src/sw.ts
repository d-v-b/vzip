// Service worker: serves vzip archives and virtualized TIFFs under
// `<scope>vz/` as plain HTTP (see server.ts). Archives are kept in memory
// and rebuilt if the worker restarts.

import { makeHandler } from "./server.ts";

declare const self: ServiceWorkerGlobalScope;

const prefix = new URL("vz/", self.registration.scope).href;
const handle = makeHandler({ prefix });

self.addEventListener("install", (event) => event.waitUntil(self.skipWaiting()));
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("message", (event) => {
  if (event.data === "claim") event.waitUntil(self.clients.claim());
});
self.addEventListener("fetch", (event) => {
  if (event.request.url.startsWith(prefix)) event.respondWith(handle(event.request));
});

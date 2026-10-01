// Retires a service worker that is no longer used.
//
// Publish this file at the URL of the old worker's script. Browsers that
// still have the old worker fetch it when they next check for updates (on a
// visit within its scope), install it in place of the old one, and it then
// unregisters itself. It handles no requests.
//
// The demo site (d-v-b/vzip-demo) serves it at /vzip-demo/vzip-sw.js: the
// site's first deploy put the TIFF-to-Zarr demo, and its worker, at the root,
// before the demo moved to tiff-to-zarr/.

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => {
  event.waitUntil(self.registration.unregister());
});

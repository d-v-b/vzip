// Page-side helpers for the vzip service worker.

import { ARCHIVE_KEY, encodeId } from "./server.ts";

/**
 * Registers the service worker and waits until it controls this page.
 * Returns the URL prefix it serves (`<scope>vz/`).
 */
export async function registerVzipWorker(scriptUrl: string | URL = "vzip-sw.js"): Promise<string> {
  const registration = await navigator.serviceWorker.register(scriptUrl);
  await navigator.serviceWorker.ready;
  if (navigator.serviceWorker.controller === null) {
    await new Promise((resolve) =>
      navigator.serviceWorker.addEventListener("controllerchange", resolve, { once: true }),
    );
  }
  return new URL("vz/", registration.scope).href;
}

/** The Zarr URL of the virtualized TIFF at `url` (a directory URL, ending in "/"). */
export function tiffZarrUrl(prefix: string, url: string): string {
  return `${prefix}tiff/${encodeId(new URL(url).href)}/`;
}

/** The Zarr URL of the contents of the .vzip archive at `url`. */
export function archiveZarrUrl(prefix: string, url: string): string {
  return `${prefix}archive/${encodeId(new URL(url).href)}/`;
}

/** Where the archive behind a Zarr URL can be downloaded. */
export function archiveDownloadUrl(zarrUrl: string): string {
  return zarrUrl + ARCHIVE_KEY;
}

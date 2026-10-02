// Page-side helpers for the vzip service worker.

import { ARCHIVE_KEY, encodeId } from "./server.ts";

/**
 * Registers the service worker and waits until it controls this page.
 * Returns the URL prefix it serves (`<scope>vz/`).
 *
 * Another worker may control the page at first, for example one registered
 * with a broader scope. Until this worker takes over, requests under the
 * prefix would bypass it, so this waits for this worker specifically.
 */
export async function registerVzipWorker(
  scriptUrl: string | URL = "vzip-sw.js",
  timeoutMs = 10000,
): Promise<string> {
  const script = new URL(scriptUrl, location.href).href;
  const registration = await navigator.serviceWorker.register(script);
  const container = navigator.serviceWorker;
  const ours = () => container.controller?.scriptURL === script;
  if (!ours()) {
    await new Promise<void>((resolve, reject) => {
      const finish = (error?: Error) => {
        clearTimeout(timer);
        container.removeEventListener("controllerchange", check);
        if (error) reject(error);
        else resolve();
      };
      const check = () => {
        if (ours()) finish();
      };
      const timer = setTimeout(
        () => finish(new Error("the vzip service worker did not take control of this page; reload it and try again")),
        timeoutMs,
      );
      container.addEventListener("controllerchange", check);
      // A new worker takes control when it activates; one that was already
      // active does when asked.
      container.ready.then((r) => r.active?.postMessage("claim"));
      check();
    });
  }
  return new URL("vz/", registration.scope).href;
}

/** The Zarr URL of the virtualized TIFF or ND2 file at `url` (a directory URL, ending in "/"). */
export function imageZarrUrl(prefix: string, url: string): string {
  return `${prefix}image/${encodeId(new URL(url).href)}/`;
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

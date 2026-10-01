/**
 * @license
 * Copyright 2026 Google Inc.
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/**
 * @file The `vzip://` URL scheme, in the style of Neuroglancer's legacy
 * `zarr://<url>` datasource syntax:
 *
 *   `vzip://<archive-url>/<path>`  ==  `<archive-url>|vzip:<path>`
 *
 * The archive URL ends at the first path segment whose name ends in `.vzip`;
 * the rest is a key path inside the archive. For example,
 * `vzip://https://example.com/data.vzip/scale0/zarr.json`. This keeps the
 * boundary unambiguous when Neuroglancer appends paths to a URL string. An
 * archive whose name does not end in `.vzip` can be opened with the pipeline
 * form `<archive-url>|vzip:<path>`, or as `vzip://<archive-url>` (no path).
 */

import type {
  BaseKvStoreProvider,
  KvStoreContext,
} from "#src/kvstore/context.js";
import type { UrlWithParsedScheme } from "#src/kvstore/url.js";

/** Rewrites `vzip://<archive-url>` as `<archive-url>|vzip:`. */
export function expandVzipSchemeUrl(url: UrlWithParsedScheme): string {
  const suffix = url.suffix ?? "";
  if (!suffix.startsWith("//") || suffix.length === 2) {
    throw new Error(
      `Invalid URL ${JSON.stringify(url.url)}, expected \`vzip://<archive-url>\`, ` +
        "for example `vzip://https://example.com/data.vzip`",
    );
  }
  const rest = suffix.substring(2);
  if (!/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(rest)) {
    throw new Error(
      `Invalid URL ${JSON.stringify(url.url)}: the archive URL must have a scheme, ` +
        "for example `vzip://https://example.com/data.vzip`",
    );
  }
  // The first segment ending in ".vzip", followed by "/" or the end.
  const m = rest.match(/^(.*?\.vzip)(?:\/(.*))?$/i);
  if (m === null) return `${rest}|vzip:`;
  return `${m[1]}|vzip:${m[2] ?? ""}`;
}

export function vzipSchemeProvider(context: {
  kvStoreContext: KvStoreContext;
}): BaseKvStoreProvider {
  return {
    scheme: "vzip",
    description: "vzip archive (vzip://<archive-url>)",
    getKvStore(url) {
      return context.kvStoreContext.getKvStore(expandVzipSchemeUrl(url));
    },
  };
}

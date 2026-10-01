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

import type { KvStoreAdapterProvider } from "#src/kvstore/context.js";
import type { SharedKvStoreContext } from "#src/kvstore/frontend.js";
import { frontendOnlyKvStoreProviderRegistry } from "#src/kvstore/frontend.js";
import { KvStoreFileHandle } from "#src/kvstore/index.js";
import { ensureNoQueryOrFragmentParameters } from "#src/kvstore/url.js";
import { registerAutoDetect } from "#src/kvstore/vzip/auto_detect.js";
import { VzipKvStore } from "#src/kvstore/vzip/frontend.js";
import { vzipSchemeProvider } from "#src/kvstore/vzip/scheme.js";

function vzipProvider(
  sharedKvStoreContext: SharedKvStoreContext,
): KvStoreAdapterProvider {
  return {
    scheme: "vzip",
    description: "vzip archive (ZIP with byte-range references)",
    getKvStore(parsedUrl, base) {
      ensureNoQueryOrFragmentParameters(parsedUrl);
      return {
        store: new VzipKvStore(
          sharedKvStoreContext,
          new KvStoreFileHandle(base.store, base.path),
        ),
        path: decodeURIComponent(parsedUrl.suffix ?? ""),
      };
    },
  };
}

frontendOnlyKvStoreProviderRegistry.registerKvStoreAdapterProvider(
  vzipProvider,
);

// `vzip://<archive-url>` is shorthand for `<archive-url>|vzip:`.
frontendOnlyKvStoreProviderRegistry.registerBaseKvStoreProvider(
  vzipSchemeProvider,
);

registerAutoDetect(frontendOnlyKvStoreProviderRegistry.autoDetectRegistry);

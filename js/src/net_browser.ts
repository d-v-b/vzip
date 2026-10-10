// `#net` in a browser (package.json "imports"): `fetch` hides the address it
// connects to, so rule 3 of spec §8.7 is checked on hosts as written
// (archive.ts), and Private Network Access covers the rest.

import type { CheckedFetch } from "./http.ts";

/** No checked fetch here: the reader uses the platform's `fetch`. */
export function checkedFetch(
  _check: (url: string, address: string) => void,
  _policy: { allowUncheckedProxy?: boolean },
): CheckedFetch | undefined {
  return undefined;
}

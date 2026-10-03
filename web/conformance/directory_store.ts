// A local directory as a store (VIRTUALIZE.md §1.5), under Node: its regular
// files at any depth, without symbolic links or names that are not UTF-8.

import fs from "node:fs";
import { addListed, checkStoreUrl, type Store } from "../src/virtualize/store.ts";

/** The directory `root`, read as the store served at `storeUrl` (ending in "/"). */
export function directoryStore(root: string, storeUrl: string): Store {
  checkStoreUrl(storeUrl);
  const listed: [string, number][] = [];
  const decoder = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });
  const walk = (dir: string, rel: string) => {
    for (const raw of fs.readdirSync(dir, { encoding: "buffer" })) {
      let name: string;
      try {
        name = decoder.decode(raw);
      } catch {
        continue;
      }
      const full = `${dir}/${name}`;
      const st = fs.lstatSync(full);
      const key = rel === "" ? name : `${rel}/${name}`;
      if (st.isDirectory()) walk(full, key);
      else if (st.isFile()) listed.push([key, st.size]);
    }
  };
  walk(root, "");
  const store: Store = {
    url: storeUrl,
    objects: new Map(),
    listed: 0,
    requests: 0,
    async read(key) {
      const data = new Uint8Array(fs.readFileSync(`${root}/${key}`));
      if (data.length !== store.objects.get(key)) throw new Error(`${key}: the file changed while it was read`);
      return data;
    },
  };
  addListed(store, listed, "", new Set());
  return store;
}

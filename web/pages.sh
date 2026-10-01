#!/bin/bash
# Builds the TIFF-to-Zarr demo and publishes it to the demo site
# (https://d-v-b.github.io/vzip-demo/, the gh-pages branch of d-v-b/vzip-demo),
# which hosts each demo in its own directory:
#   <demo>/               the demo page (web/dist)
#   <demo>/neuroglancer/  the Neuroglancer fork (neuroglancer/dist/client)
# The demo page and Neuroglancer share one origin and one service worker
# scope, as the demo needs. Only <demo>/ and the root index.html (a list of
# the demos, from each directory's <title> and meta description) change.
#
# Usage: web/pages.sh [demo dir] [remote url]
#   (defaults: tiff-to-zarr, https://github.com/d-v-b/vzip-demo.git)

set -euo pipefail
cd "$(dirname "$0")/.."
demo="${1:-tiff-to-zarr}"
remote_url="${2:-https://github.com/d-v-b/vzip-demo.git}"
commit="$(git rev-parse --short HEAD)"

(cd neuroglancer && npm run build)
node web/build.mjs

site="$(mktemp -d)"
trap 'rm -rf "$site"' EXIT
if git ls-remote --exit-code --heads "$remote_url" gh-pages > /dev/null; then
  git clone --quiet --depth 1 --branch gh-pages "$remote_url" "$site"
else
  git -C "$site" init --quiet --initial-branch gh-pages
fi
rm -rf "${site:?}/$demo"
mkdir -p "$site/$demo/neuroglancer"
cp -R web/dist/. "$site/$demo/"
cp -R neuroglancer/dist/client/. "$site/$demo/neuroglancer/"
find "$site/$demo" -name '*.map' -delete
touch "$site/.nojekyll"
node web/pages_index.mjs "$site"

git -C "$site" add -A
git -C "$site" -c user.name="$(git config user.name)" -c user.email="$(git config user.email)" \
  commit --quiet -m "chore(pages): build ${demo} from d-v-b/vzip@${commit}"
git -C "$site" push --quiet "$remote_url" gh-pages
echo "published ${demo}/ (from ${commit})"

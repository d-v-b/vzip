#!/bin/bash
# Builds the image-to-Zarr demo and publishes it to the demo site
# (https://d-v-b.github.io/vzip-demo/, the gh-pages branch of d-v-b/vzip-demo),
# which hosts each demo in its own directory:
#   <demo>/               the demo page (js/dist)
#   <demo>/neuroglancer/  the Neuroglancer fork (d-v-b/neuroglancer, branch vzip)
# The demo page and Neuroglancer share one origin and one service worker
# scope, as the demo needs. Only <demo>/ and the root index.html (a list of
# the demos, from each directory's <title> and meta description) change.
#
# The fork is built from $NEUROGLANCER, by default a clone next to this
# repository (../neuroglancer) with its dependencies installed.
#
# Usage: js/demo/pages.sh [demo dir] [remote url]
#   (defaults: image-to-zarr, https://github.com/d-v-b/vzip-demo.git)
#
# Retired demo directories (MOVED, below) keep a page that redirects to their
# replacement, with the query and fragment, and a worker that unregisters
# itself (js/demo/retired-sw.js), so old links and installed workers keep
# working.

set -euo pipefail
cd "$(dirname "$0")/../.."
demo="${1:-image-to-zarr}"
remote_url="${2:-https://github.com/d-v-b/vzip-demo.git}"
commit="$(git rev-parse --short HEAD)"
neuroglancer="${NEUROGLANCER:-$(cd .. && pwd)/neuroglancer}"
ng_commit="$(git -C "$neuroglancer" rev-parse --short HEAD)"

(cd "$neuroglancer" && npm run build)
(cd js && npm install --no-audit --no-fund --silent && node build.mjs)

site="$(mktemp -d)"
trap 'rm -rf "$site"' EXIT
if git ls-remote --exit-code --heads "$remote_url" gh-pages > /dev/null; then
  git clone --quiet --depth 1 --branch gh-pages "$remote_url" "$site"
else
  git -C "$site" init --quiet --initial-branch gh-pages
fi
rm -rf "${site:?}/$demo"
mkdir -p "$site/$demo/neuroglancer"
cp -R js/dist/. "$site/$demo/"
cp -R "$neuroglancer/dist/client/." "$site/$demo/neuroglancer/"
find "$site/$demo" -name '*.map' -delete
touch "$site/.nojekyll"
# The first deploy served this demo's worker at the site root. Browsers that
# installed it get this replacement, which unregisters it (see the file).
cp js/demo/retired-sw.js "$site/vzip-sw.js"
# tiff-to-zarr/ became image-to-zarr/ when the demo learned ND2.
MOVED=("tiff-to-zarr:image-to-zarr")
for move in "${MOVED[@]}"; do
  old="${move%%:*}" new="${move##*:}"
  rm -rf "${site:?}/$old"
  mkdir -p "$site/$old"
  cp js/demo/retired-sw.js "$site/$old/vzip-sw.js"
  sed "s|@TARGET@|../$new/|g" js/demo/moved.html > "$site/$old/index.html"
done
node js/demo/pages_index.mjs "$site"

git -C "$site" add -A
git -C "$site" -c user.name="$(git config user.name)" -c user.email="$(git config user.email)" \
  commit --quiet -m "chore(pages): build ${demo} from d-v-b/vzip@${commit} and d-v-b/neuroglancer@${ng_commit}"
git -C "$site" push --quiet "$remote_url" gh-pages
echo "published ${demo}/ (from ${commit})"

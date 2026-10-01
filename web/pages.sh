#!/bin/bash
# Builds the demo site and pushes it to the gh-pages branch:
#   /               the demo (web/dist)
#   /neuroglancer/  the Neuroglancer fork (neuroglancer/dist/client)
# Both share one origin and one service worker scope, as the demo needs.
#
# The site goes to its own public repository, since d-v-b/vzip is private.
#
# Usage: web/pages.sh [remote url]   (default: https://github.com/d-v-b/vzip-demo.git)

set -euo pipefail
cd "$(dirname "$0")/.."
remote_url="${1:-https://github.com/d-v-b/vzip-demo.git}"
commit="$(git rev-parse --short HEAD)"

(cd neuroglancer && npm run build)
node web/build.mjs

site="$(mktemp -d)"
trap 'rm -rf "$site"' EXIT
cp -R web/dist/. "$site"/
mkdir "$site/neuroglancer"
cp -R neuroglancer/dist/client/. "$site/neuroglancer/"
find "$site" -name '*.map' -delete
touch "$site/.nojekyll"
git -C "$site" init --quiet --initial-branch gh-pages
git -C "$site" add -A
git -C "$site" -c user.name="$(git config user.name)" -c user.email="$(git config user.email)" \
  commit --quiet -m "chore(pages): build demo from ${commit}"
git -C "$site" push --force --quiet "$remote_url" gh-pages
echo "pushed gh-pages (demo from ${commit})"

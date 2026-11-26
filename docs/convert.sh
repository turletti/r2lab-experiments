#!/bin/sh
# Convert the notebooks into Markdown pages for Sphinx (docs/notebooks/<name>.md), their links to the guides
# (docs/guide/) included.
# Run from the repository root, before sphinx-build (Read the Docs does).
set -e

for file in notebooks/*/test.ipynb; do
    name=$(basename "$(dirname "$file")")
    jupyter nbconvert --to markdown "$file" --output-dir docs/notebooks --output "$name"
done
# The notebooks link to the guides from their folder (../../docs/guide/), the pages from docs/notebooks/.
sed -i.bak 's#](\.\./\.\./docs/guide/#](../guide/#g' docs/notebooks/*.md
rm -f docs/notebooks/*.md.bak

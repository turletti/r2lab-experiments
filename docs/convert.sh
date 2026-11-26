#!/bin/sh
# Convert the notebooks into Markdown pages for Sphinx (docs/notebooks/<name>.md).
# Run from the repository root, before sphinx-build (Read the Docs does).
set -e

for file in notebooks/*/test.ipynb; do
    name=$(basename "$(dirname "$file")")
    jupyter nbconvert --to markdown "$file" --output-dir docs/notebooks --output "$name"
done

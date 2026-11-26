"""Sphinx configuration for the r2lab-experiments documentation (same layout as NADOR's)."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

# -- Project information -----------------------------------------------------
project = "R2Lab experiments with NADOR and IFRAN"
release = version = meta["version"]
author = copyright = "Inria"

# -- General configuration ---------------------------------------------------
root_doc = "index"
extensions = ["myst_parser"]
source_suffix = {".rst": "restructuredtext", ".md": "markdown"}
myst_heading_anchors = 3
# Literal blocks of the guides (diagrams, log lines) are not Python
highlight_language = "none"
# The converted notebooks skip heading levels and contain shell lines (%pip ...) that the Python highlighter cannot
# color; both are cosmetic, and silencing them keeps `-W` useful for real errors.
suppress_warnings = ["myst.xref_missing", "myst.header", "misc.highlighting_failure"]
exclude_patterns = ["_build"]

# -- HTML output -------------------------------------------------------------
html_title = project
html_short_title = "r2lab-experiments"
html_theme = "furo"

"""Sphinx configuration for the r2lab-experiments documentation (same layout as NADOR's)."""

import tomllib
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

# -- Project information -----------------------------------------------------
project = "R2Lab experiments with NADOR and IFRAN"
release = version = meta["version"]
author = "Inria"
copyright = f"{date.today().year}, {author}"

# -- General configuration ---------------------------------------------------
root_doc = "index"
extensions = ["myst_parser"]
source_suffix = {".md": "markdown"}
myst_heading_anchors = 3
# The converted notebooks skip heading levels and contain shell lines (%pip ...) that the Python highlighter cannot
# color; both are cosmetic, and silencing them keeps `-W` useful for real errors.
suppress_warnings = ["myst.xref_missing", "myst.header", "misc.highlighting_failure"]
exclude_patterns = ["_build"]

# -- HTML output -------------------------------------------------------------
html_title = project
html_short_title = "r2lab-experiments"
html_theme = "furo"

"""
Write the CSI and handover guides of docs/guide/ from the NADOR release the notebooks use (pyproject.toml).

NADOR documents the CSI in its guide (docs/guide/csi.md) and the F1 handover in its guide (docs/guide/handover.md)
and in the docstrings of nador.handover (the procedure, then how OAI and OCUDU implement it and log it). This script
copies them, unchanged, from the NADOR tag of that release, and adds how the notebooks trigger the handover (the
docstring of r2lab_experiments.handover). The pages are committed, so that the notebooks link to them in Jupyter
and on GitHub as on Read the Docs: run it again when the NADOR requirement changes.

Usage, from the root of the repository: python docs/nador_guides.py
"""

import ast
import re
import subprocess
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / "docs" / "guide"
NADOR_URL = "https://github.com/ifran-project/nador"


def nador_version() -> str:
    """Version of the nador==X requirement of pyproject.toml."""
    dependencies = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    return next(re.fullmatch(r"nador==(\S+)", d).group(1) for d in dependencies if d.startswith("nador=="))


def docstring(path: Path) -> str:
    """Module docstring of a Python file, read without importing it."""
    return ast.get_docstring(ast.parse(path.read_text(encoding="utf-8")))


def rst_page(title: str, origin: str, body: str) -> str:
    """reStructuredText page: title (above the sections of the docstring), origin note, docstring."""
    line = "*" * len(title)
    return f"{line}\n{title}\n{line}\n\n.. note::\n   {origin}\n\n{body}\n"


def main() -> None:
    version = nador_version()
    tag = f"v{version}"
    note = f"From NADOR {version} ({NADOR_URL}/tree/{tag}), written by docs/nador_guides.py: do not edit."
    with tempfile.TemporaryDirectory() as tmp:
        clone = ["git", "-c", "advice.detachedHead=false", "clone", "--quiet", "--depth", "1", "--branch", tag]
        subprocess.run([*clone, f"{NADOR_URL}.git", tmp], check=True)
        nador = Path(tmp)
        GUIDE.mkdir(parents=True, exist_ok=True)
        for name in ("csi", "handover"):
            text = (nador / "docs" / "guide" / f"{name}.md").read_text(encoding="utf-8")
            title, _, rest = text.partition("\n")
            (GUIDE / f"{name}.md").write_text(f"{title}\n\n```{{note}}\n{note}\n```\n{rest}", encoding="utf-8")
        handover = nador / "src" / "nador" / "handover"
        pages = {
            "handover-procedure": ("The F1 handover procedure", handover / "__init__.py"),
            "handover-oai": ("F1 handover with OAI", handover / "oai.py"),
            "handover-ocudu": ("F1 handover with OCUDU", handover / "ocudu.py"),
        }
        for name, (title, path) in pages.items():
            origin = f"Documentation of ``nador.handover{'' if path.stem == '__init__' else '.' + path.stem}``. {note}"
            (GUIDE / f"{name}.rst").write_text(rst_page(title, origin, docstring(path)), encoding="utf-8")
    trigger = ROOT / "src" / "r2lab_experiments" / "handover.py"
    origin = "Documentation of ``r2lab_experiments.handover``, written by docs/nador_guides.py: do not edit."
    (GUIDE / "handover-trigger.rst").write_text(
        rst_page("Triggering the handover from the notebooks", origin, docstring(trigger)), encoding="utf-8"
    )


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Check that built EPUBs carry real Pele-rendered diagrams (stdlib only).

For each EPUB given on the command line:

- at least one ``images/mermaid_N.svg`` item must exist;
- every such SVG must parse as XML, have no ``var(``, ``--_`` or ``color-mix``
  left in any attribute value (EPUB readers lack CSS custom properties), and
  start with an opaque background ``<rect>`` covering its viewBox;
- every diagram SVG must be referenced by an ``<img>`` in some chapter, and
  every such reference must resolve to an item in the EPUB;
- no chapter may contain the ``SVG not found`` placeholder.

Exit 0 when every EPUB passes, 1 when any check fails, 2 on usage errors.
Used by the build-epub CI job and the release workflow.
"""

from __future__ import annotations

import argparse
import posixpath
import re
import sys

# Parses SVG items of an EPUB this repo has just built from its own Markdown
# with the pinned, checksum-verified Pele bundle, not untrusted input.
import xml.etree.ElementTree as ET  # nosec B405
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

DIAGRAM_ITEM_RE = re.compile(r"(?:^|/)images/mermaid_\d+\.svg$")
DIAGRAM_NAME_RE = re.compile(r"^mermaid_\d+\.svg$")
IMG_SRC_RE = re.compile(
    r"<img\b[^>]*?\bsrc\s*=\s*([\"'])(.*?)\1", re.IGNORECASE | re.DOTALL
)
PLACEHOLDER = "SVG not found"
FORBIDDEN_IN_ATTRIBUTES = ("var(", "--_", "color-mix")
TRANSPARENT_FILLS = {"", "none", "transparent"}


@dataclass
class EpubReport:
    """What one EPUB contains and everything wrong with it."""

    diagrams: int = 0
    references: int = 0
    placeholders: int = 0
    errors: list[str] = field(default_factory=list)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def check_svg(name: str, data: bytes) -> list[str]:
    """Return the problems with one diagram SVG item."""
    try:
        root = ET.fromstring(data)  # nosec B314
    except ET.ParseError as exc:
        return [f"{name} is not valid XML: {exc}"]
    if _local_name(root.tag) != "svg":
        return [f"{name} root element is <{_local_name(root.tag)}>, not <svg>"]

    problems = [
        f"{name} has unflattened CSS {token!r} in <{_local_name(element.tag)} {attr}>"
        for element in root.iter()
        for attr, value in element.attrib.items()
        for token in FORBIDDEN_IN_ATTRIBUTES
        if token in value
    ]

    first = root[0] if len(root) else None
    if first is None or _local_name(first.tag) != "rect":
        problems.append(f"{name} has no background <rect> as its first child")
    elif first.get("fill", "").strip().lower() in TRANSPARENT_FILLS:
        problems.append(f"{name} background <rect> is not opaque")
    else:
        view_box = (root.get("viewBox") or "").split()
        box = [first.get(k) for k in ("x", "y", "width", "height")]
        if len(view_box) == 4 and box != view_box:
            problems.append(
                f"{name} background <rect> {box} does not cover viewBox {view_box}"
            )
    return problems


def inspect_epub(path: Path) -> EpubReport:
    """Collect diagram counts and problems for one EPUB."""
    report = EpubReport()
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            diagrams = {n for n in names if DIAGRAM_ITEM_RE.search(n)}
            report.diagrams = len(diagrams)
            if not diagrams:
                report.errors.append(
                    "no Pele-rendered diagrams (images/mermaid_N.svg) found"
                )
            for name in sorted(diagrams):
                report.errors.extend(check_svg(name, zf.read(name)))

            referenced: set[str] = set()
            for doc in names:
                if not doc.endswith((".xhtml", ".html")):
                    continue
                text = zf.read(doc).decode("utf-8", errors="replace")
                if PLACEHOLDER in text:
                    report.placeholders += text.count(PLACEHOLDER)
                    report.errors.append(
                        f"{doc} contains an {PLACEHOLDER!r} placeholder"
                    )
                for _quote, src in IMG_SRC_RE.findall(text):
                    if not DIAGRAM_NAME_RE.match(posixpath.basename(src)):
                        continue
                    report.references += 1
                    target = posixpath.normpath(
                        posixpath.join(posixpath.dirname(doc), src)
                    )
                    if target not in diagrams:
                        report.errors.append(f"{doc} references missing diagram {src}")
                    referenced.add(target)
            report.errors.extend(
                f"{name} is never referenced by a chapter"
                for name in sorted(diagrams - referenced)
            )
    except (OSError, zipfile.BadZipFile) as exc:
        report.errors.append(f"cannot read EPUB: {exc}")
    return report


def check_epub(path: Path) -> list[str]:
    """Return every problem found in `path` (empty when it passes)."""
    return inspect_epub(path).errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check that EPUBs embed flattened Pele diagram SVGs."
    )
    parser.add_argument("epubs", nargs="+", type=Path, help="EPUB file(s) to check")
    args = parser.parse_args(argv)

    failed = False
    for path in args.epubs:
        report = inspect_epub(path)
        if report.errors:
            failed = True
            for error in report.errors:
                print(f"❌ {path.name}: {error}")
        else:
            print(
                f"✅ {path.name}: {report.diagrams} Pele-rendered diagram(s), "
                f"{report.references} reference(s), "
                f"{report.placeholders} placeholder(s)"
            )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

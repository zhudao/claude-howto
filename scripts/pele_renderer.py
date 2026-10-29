"""Render Mermaid sources to SVG with the pinned Pele bundle (stdlib only).

Shared by ``check_mermaid.py`` (validation) and ``build_epub.py`` (EPUB
images). All diagrams of a run go to ONE Node.js process running
``pele_render.mjs``, which imports the sha256-verified bundle from
``vendor_assets.ensure_pele_bundle``.

Two failure classes keep environment problems apart from broken diagrams:

- ``PeleUnavailableError``: Node.js is missing or too old, or the bundle could
  not be downloaded or verified. Callers decide whether that is fatal.
- ``PeleHelperError``: the helper crashed, timed out or returned malformed
  output. Always a real failure.

A diagram Pele rejects is not an exception: its ``PeleResult.error`` is set.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess  # nosec B404
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Make the sibling vendor_assets module importable regardless of cwd.
sys.path.insert(0, str(Path(__file__).parent))

from vendor_assets import PELE_ESM_BUNDLE, PELE_VERSION, ensure_pele_bundle

MIN_NODE_MAJOR = 20
HELPER = Path(__file__).with_name("pele_render.mjs")
NODE_INSTALL_HINT = f"Install Node.js >= {MIN_NODE_MAJOR} (https://nodejs.org/)"

_NODE_VERSION_RE = re.compile(r"^v?(\d+)\.")


class PeleUnavailableError(RuntimeError):
    """Pele cannot run here: no suitable Node.js, or no verified bundle."""


class PeleHelperError(RuntimeError):
    """The Node.js helper crashed, timed out or returned malformed output."""


@dataclass(frozen=True)
class PeleDiagramError:
    """Why Pele rejected one diagram (codes from PeleError, or ``internal``)."""

    code: str
    message: str
    line: int | None = None
    column: int | None = None

    def describe(self) -> str:
        """Return ``[code] message (diagram line L, column C)``."""
        text = f"[{self.code}] {self.message}"
        if self.line is not None:
            text += f" (diagram line {self.line}"
            if self.column is not None:
                text += f", column {self.column}"
            text += ")"
        return text


@dataclass(frozen=True)
class PeleResult:
    """Outcome for one source: an SVG and its diagram type, or an error."""

    svg: str | None
    diagram_type: str | None
    error: PeleDiagramError | None = None


def find_node() -> str:
    """Return the path of a ``node`` binary that is at least MIN_NODE_MAJOR."""
    node = shutil.which("node")
    if node is None:
        raise PeleUnavailableError(f"node not found on PATH. {NODE_INSTALL_HINT}")
    try:
        # Fixed argv (resolved node binary + flag), no shell.
        proc = subprocess.run(  # nosec B603
            [node, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PeleUnavailableError(
            f"could not run {node} --version: {exc}. {NODE_INSTALL_HINT}"
        ) from exc
    version = proc.stdout.strip()
    match = _NODE_VERSION_RE.match(version)
    if proc.returncode != 0 or match is None:
        raise PeleUnavailableError(
            f"could not read the Node.js version from {node} "
            f"(got {version!r}). {NODE_INSTALL_HINT}"
        )
    if int(match.group(1)) < MIN_NODE_MAJOR:
        raise PeleUnavailableError(
            f"Node.js {version} is too old for Pele {PELE_VERSION}. {NODE_INSTALL_HINT}"
        )
    return node


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _parse_result(item: Any, index: int) -> PeleResult:
    if not isinstance(item, dict):
        raise PeleHelperError(f"result {index} is not an object: {item!r}")
    error = item.get("error")
    if error is not None:
        if not isinstance(error, dict):
            raise PeleHelperError(f"result {index} has a malformed error: {error!r}")
        return PeleResult(
            svg=None,
            diagram_type=None,
            error=PeleDiagramError(
                code=str(error.get("code", "internal")),
                message=str(error.get("message", "")),
                line=_optional_int(error.get("line")),
                column=_optional_int(error.get("column")),
            ),
        )
    svg = item.get("svg")
    if not isinstance(svg, str) or not svg.lstrip().startswith("<svg"):
        raise PeleHelperError(f"result {index} has no SVG output")
    diagram_type = item.get("type")
    return PeleResult(
        svg=svg,
        diagram_type=diagram_type if isinstance(diagram_type, str) else None,
    )


def render_diagrams(
    sources: Sequence[str],
    logger: logging.Logger,
    *,
    options: dict[str, Any] | None = None,
    timeout: float = 120,
) -> list[PeleResult]:
    """Render every source with Pele, returning one result per source in order.

    Raises PeleUnavailableError when Node.js or the bundle is missing, and
    PeleHelperError when the helper fails as a whole.
    """
    node = find_node()
    try:
        bundle_dir = ensure_pele_bundle(logger)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PeleUnavailableError(
            f"could not fetch or verify the Pele {PELE_VERSION} bundle: {exc}. "
            "The first run needs network access; later runs use the cache in "
            "scripts/.vendor-cache/pele/."
        ) from exc

    payload = json.dumps({"diagrams": list(sources), "options": options or {}})
    cmd = [node, str(HELPER), str(bundle_dir / PELE_ESM_BUNDLE)]
    logger.debug(f"Rendering {len(sources)} diagram(s) with Pele {PELE_VERSION}")
    try:
        # Fixed argv: resolved node binary, the repo's helper script and the
        # checksum-verified bundle path. No shell; sources travel on stdin.
        proc = subprocess.run(  # nosec B603
            cmd,
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PeleHelperError(f"Pele helper timed out after {timeout}s") from exc
    except OSError as exc:
        raise PeleHelperError(f"could not start the Pele helper: {exc}") from exc

    if proc.returncode != 0:
        raise PeleHelperError(
            f"Pele helper exited with status {proc.returncode}: "
            f"{proc.stderr.strip() or '(no stderr)'}"
        )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise PeleHelperError(f"Pele helper returned invalid JSON: {exc}") from exc
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        raise PeleHelperError("Pele helper output has no results list")
    if len(results) != len(sources):
        raise PeleHelperError(
            f"Pele helper returned {len(results)} result(s) for "
            f"{len(sources)} diagram(s)"
        )
    return [_parse_result(item, index) for index, item in enumerate(results)]

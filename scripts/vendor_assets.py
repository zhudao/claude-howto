"""Download and self-host third-party assets (Tailwind, Pele, Inter font).

Replaces the previous CDN-based approach so the rendered site has no external
runtime dependencies. Assets are cached under `scripts/.vendor-cache/` and
copied into the built site's `assets/vendor/` directory.

Pele (https://github.com/obsidianmd/pele) renders the Mermaid diagrams. Its
bundle is pinned to an exact version and every file is sha256-verified each
time it is used, so a stale or corrupted cache is re-downloaded rather than
trusted. The website, the Mermaid validator and the EPUB builder all share the
same cached copy.

The Tailwind standalone CLI (a Go binary, no Node.js required) is downloaded
on first use and reused thereafter. We pin to Tailwind v3 because the templates
use v3-style runtime configuration; Tailwind v4 deprecated that config format.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import shutil
import stat
import subprocess  # nosec B404 — used only to invoke the bundled Tailwind CLI
import urllib.request
from pathlib import Path

TAILWIND_VERSION = "v3.4.19"
PELE_VERSION = "1.6.0"
PELE_BASE_URL = f"https://cdn.jsdelivr.net/npm/pele@{PELE_VERSION}"
# Relative path inside the npm package -> expected sha256 of that file.
PELE_FILES: dict[str, str] = {
    "dist/pele.js": "d1ebc1889f3069d7759fe5db34c4ad4b4823285edbee2b537d0ef3d505a76d8d",
    "dist/pele.global.js": (
        "b82d1bcd435302779a945e1e0f4a54ea56fb26e010ce3f2122f4216fcfc095d7"
    ),
    "LICENSE": "311bccc30de6f7b9cbfb64232bb0c911ccb2191abe316550813ae25b2c7925b9",
}
# The cache keeps each file under its base name, except the ES module build:
# with no package.json "type": "module" beside it, Node.js 20.0-20.18 and
# 22.0-22.6 load a bare .js file as CommonJS and fail on `export`. A .mjs
# name loads as an ES module on every Node.js >= 20.
PELE_ESM_BUNDLE = "pele.mjs"
_PELE_CACHE_NAMES = {"dist/pele.js": PELE_ESM_BUNDLE}
GOOGLE_FONTS_CSS_URL = (
    "https://fonts.googleapis.com/css2?"
    "family=Fraunces:ital,opsz,wght@0,9..144,300..700;1,9..144,300..600"
    "&family=Inter:wght@400;500;600;700;800"
    "&family=JetBrains+Mono:wght@400;500;600"
    "&family=Manrope:wght@400;500;600;700"
    "&display=swap"
)
GOOGLE_FONTS_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

VENDOR_CACHE_DIR_NAME = ".vendor-cache"
TAILWIND_BIN_NAME = "tailwindcss"


def _vendor_cache_dir() -> Path:
    return Path(__file__).parent / VENDOR_CACHE_DIR_NAME


def _detect_tailwind_asset_name() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "darwin":
        return (
            "tailwindcss-macos-arm64" if machine == "arm64" else "tailwindcss-macos-x64"
        )
    if system == "linux":
        if machine in ("aarch64", "arm64"):
            return "tailwindcss-linux-arm64"
        if machine.startswith("armv7"):
            return "tailwindcss-linux-armv7"
        return "tailwindcss-linux-x64"
    if system == "windows":
        return "tailwindcss-windows-x64.exe"
    raise RuntimeError(f"Unsupported platform for Tailwind CLI: {system}/{machine}")


def _download(url: str, dest: Path, headers: dict[str, str] | None = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"Refusing to fetch non-HTTP URL: {url}")
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req) as resp, dest.open("wb") as out:  # nosec B310 — scheme validated above
        shutil.copyfileobj(resp, out)


def ensure_tailwind_binary(logger: logging.Logger) -> Path:
    """Download the Tailwind standalone CLI if not already cached."""
    cache = _vendor_cache_dir()
    bin_path = cache / TAILWIND_BIN_NAME
    if bin_path.exists() and os.access(bin_path, os.X_OK):
        return bin_path

    asset = _detect_tailwind_asset_name()
    url = f"https://github.com/tailwindlabs/tailwindcss/releases/download/{TAILWIND_VERSION}/{asset}"
    logger.info(f"Downloading Tailwind CLI {TAILWIND_VERSION} ({asset})")
    _download(url, bin_path)
    bin_path.chmod(bin_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return bin_path


def build_tailwind_css(
    output_css: Path,
    template_dir: Path,
    site_dir: Path,
    logger: logging.Logger,
) -> None:
    """Compile a single CSS file using the Tailwind standalone CLI."""
    bin_path = ensure_tailwind_binary(logger)
    config = template_dir / "tailwind.config.js"
    input_css = template_dir / "tailwind.input.css"
    if not config.exists():
        raise RuntimeError(f"Tailwind config not found: {config}")
    if not input_css.exists():
        raise RuntimeError(f"Tailwind input CSS not found: {input_css}")

    output_css.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(bin_path),
        "--config",
        str(config),
        "--input",
        str(input_css),
        "--output",
        str(output_css),
        "--minify",
    ]
    logger.info(f"Compiling Tailwind CSS → {output_css}")
    # Run from the project root so relative `content` globs in the config
    # resolve against the built site directory.
    cwd = site_dir.parent if site_dir.parent.exists() else Path.cwd()
    env = os.environ.copy()
    env["TAILWIND_SITE_DIR"] = str(site_dir)
    env["TAILWIND_TEMPLATE_DIR"] = str(template_dir)
    result = subprocess.run(  # nosec B603 — cmd is a fixed argv list, no shell, no user input
        cmd,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Tailwind CLI failed (exit {result.returncode}):\n{result.stderr}"
        )
    if result.stderr.strip():
        logger.debug(f"Tailwind CLI: {result.stderr.strip()}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_pele_bundle(logger: logging.Logger) -> Path:
    """Return the cache dir holding the pinned, sha256-verified Pele bundle.

    Every file is verified on every call, not only after a download: CI
    restores caches by prefix, so a cached copy can be stale or truncated. A
    file whose digest does not match is downloaded again into a temporary file
    next to it, verified, and only then moved into place. A download that
    does not match raises ``RuntimeError`` and leaves no file behind.
    """
    cache = _vendor_cache_dir() / "pele" / PELE_VERSION
    cache.mkdir(parents=True, exist_ok=True)
    for rel_path, expected in PELE_FILES.items():
        dest = cache / _PELE_CACHE_NAMES.get(rel_path, Path(rel_path).name)
        if dest.is_file() and _sha256_file(dest) == expected:
            continue
        logger.info(f"Downloading Pele {PELE_VERSION}: {rel_path}")
        part = dest.with_name(dest.name + ".part")
        try:
            _download(f"{PELE_BASE_URL}/{rel_path}", part)
            actual = _sha256_file(part)
            if actual != expected:
                raise RuntimeError(
                    f"Checksum mismatch for Pele {PELE_VERSION} {rel_path}: "
                    f"expected sha256 {expected}, got {actual}"
                )
            part.replace(dest)
        finally:
            part.unlink(missing_ok=True)
    return cache


def fetch_pele(target_dir: Path, logger: logging.Logger) -> Path:
    """Copy Pele's browser bundle and its LICENSE into `target_dir`.

    Returns the path of the copied ``pele.global.js``. The LICENSE (MIT, plus
    the ISC notice for the Lucide icon used by the zoom controls) ships next
    to the bundle.
    """
    cache = ensure_pele_bundle(logger)
    target_dir.mkdir(parents=True, exist_ok=True)
    for name in ("pele.global.js", "LICENSE"):
        shutil.copy2(cache / name, target_dir / name)
    return target_dir / "pele.global.js"


_FONT_URL_RE = re.compile(r"url\((https://fonts\.gstatic\.com/[^)]+)\)")


def fetch_fonts(target_dir: Path, logger: logging.Logger) -> Path:
    """Download Google Fonts CSS + WOFF2 files, rewrite URLs to relative paths."""
    cache = _vendor_cache_dir() / "fonts"
    cache.mkdir(parents=True, exist_ok=True)
    # Key the cached CSS on the request URL so adding/removing font families
    # invalidates the cache instead of silently serving the old stylesheet.
    css_key = hashlib.sha256(GOOGLE_FONTS_CSS_URL.encode("utf-8")).hexdigest()[:10]
    css_cache = cache / f"fonts-{css_key}.css"
    files_cache = cache / "files"
    files_cache.mkdir(parents=True, exist_ok=True)

    if not css_cache.exists():
        logger.info("Downloading Google Fonts CSS")
        _download(
            GOOGLE_FONTS_CSS_URL, css_cache, headers={"User-Agent": GOOGLE_FONTS_UA}
        )

    css = css_cache.read_text(encoding="utf-8")
    url_map: dict[str, str] = {}
    for url in _FONT_URL_RE.findall(css):
        # Stable filename derived from the gstatic path so duplicates collapse.
        name = url.rsplit("/", 1)[-1]
        local = files_cache / name
        if not local.exists():
            logger.debug(f"Fetching font file: {name}")
            _download(url, local)
        url_map[url] = f"files/{name}"

    rewritten = css
    for url, rel in url_map.items():
        rewritten = rewritten.replace(url, rel)

    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "fonts.css").write_text(rewritten, encoding="utf-8")

    target_files = target_dir / "files"
    target_files.mkdir(parents=True, exist_ok=True)
    for f in files_cache.iterdir():
        if f.is_file():
            shutil.copy2(f, target_files / f.name)
    return target_dir / "fonts.css"


def write_vendor_manifest(target_dir: Path, fonts_count: int) -> None:
    """Record what was vendored — useful for debugging."""
    manifest = {
        "tailwind": TAILWIND_VERSION,
        "pele": PELE_VERSION,
        "fonts": {
            "family": ["Fraunces", "Inter", "JetBrains Mono", "Manrope"],
            "files": fonts_count,
        },
    }
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

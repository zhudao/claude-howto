"""Tests for check_epub_diagrams.py (the CI proof that EPUB diagrams are real)."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from check_epub_diagrams import check_epub, main

GOOD_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 148 160" '
    'style="max-width:100%;font-family:Arial, sans-serif" fill="#1f1f1f">'
    '<rect x="0" y="0" width="148" height="160" fill="#fff"/>'
    '<rect x="-50" y="-24" width="100" height="48" fill="#f3f3f3" rx="4"/>'
    '<text x="0" y="5.6">claude --model: opus uses var(--x) in a label</text>'
    "</svg>"
)


def _chapter(*srcs: str, extra: str = "") -> str:
    imgs = "".join(f'<img alt="Diagram" src="{src}"/>' for src in srcs)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>T</title></head>'
        f"<body><p>&nbsp;Intro</p>{imgs}{extra}</body></html>"
    )


def _epub(
    path: Path,
    svgs: dict[str, str] | None = None,
    chapters: dict[str, str] | None = None,
) -> Path:
    """Write a minimal EPUB-shaped zip with ebooklib's EPUB/ layout."""
    svgs = {"mermaid_1.svg": GOOD_SVG} if svgs is None else svgs
    chapters = (
        {"chap_01.xhtml": _chapter("images/mermaid_1.svg")}
        if chapters is None
        else chapters
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("EPUB/content.opf", "<package/>")
        for name, svg in svgs.items():
            zf.writestr(f"EPUB/images/{name}", svg)
        for name, html in chapters.items():
            zf.writestr(f"EPUB/{name}", html)
    return path


class TestCheckEpub:
    def test_good_epub_passes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        epub = _epub(
            tmp_path / "guide.epub",
            svgs={"mermaid_1.svg": GOOD_SVG, "mermaid_2.svg": GOOD_SVG},
            chapters={
                "chap_01.xhtml": _chapter(
                    "images/mermaid_1.svg", "images/mermaid_2.svg"
                ),
                "chap_02.xhtml": _chapter("images/mermaid_1.svg"),
            },
        )

        assert check_epub(epub) == []
        assert main([str(epub)]) == 0
        assert (
            "✅ guide.epub: 2 Pele-rendered diagram(s), 3 reference(s), "
            "0 placeholder(s)"
        ) in capsys.readouterr().out

    def test_single_quoted_src_is_recognised(self, tmp_path: Path) -> None:
        chapter = _chapter().replace(
            "</body>", "<img alt='D' src='images/mermaid_1.svg'/></body>"
        )
        epub = _epub(tmp_path / "g.epub", chapters={"chap_01.xhtml": chapter})

        assert check_epub(epub) == []

    def test_placeholder_fails(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        epub = _epub(
            tmp_path / "guide.epub",
            chapters={
                "chap_01.xhtml": _chapter(
                    "images/mermaid_1.svg",
                    extra="<p><em>[SVG not found: images/mermaid_2.svg]</em></p>",
                )
            },
        )

        assert any("SVG not found" in e for e in check_epub(epub))
        assert main([str(epub)]) == 1
        assert "❌ guide.epub:" in capsys.readouterr().out

    def test_unreferenced_svg_fails(self, tmp_path: Path) -> None:
        epub = _epub(
            tmp_path / "g.epub",
            svgs={"mermaid_1.svg": GOOD_SVG, "mermaid_2.svg": GOOD_SVG},
        )

        errors = check_epub(epub)
        assert errors == ["EPUB/images/mermaid_2.svg is never referenced by a chapter"]

    def test_dangling_reference_fails(self, tmp_path: Path) -> None:
        epub = _epub(
            tmp_path / "g.epub",
            chapters={
                "chap_01.xhtml": _chapter(
                    "images/mermaid_1.svg", "images/mermaid_9.svg"
                )
            },
        )

        errors = check_epub(epub)
        assert errors == [
            "EPUB/chap_01.xhtml references missing diagram images/mermaid_9.svg"
        ]

    @pytest.mark.parametrize(
        "leftover",
        [
            'fill="var(--_s)"',
            'stroke="var(--pele-border,#8a8a8a)"',
            'stroke="color-mix(in srgb,#e6e6e6 50%,#fff)"',
        ],
        ids=["alias", "pele-var", "color-mix"],
    )
    def test_leftover_css_variable_in_attribute_fails(
        self, tmp_path: Path, leftover: str
    ) -> None:
        svg = GOOD_SVG.replace('fill="#f3f3f3"', leftover)
        epub = _epub(tmp_path / "g.epub", svgs={"mermaid_1.svg": svg})

        errors = check_epub(epub)
        assert errors
        assert all("unflattened CSS" in e for e in errors)

    def test_css_like_label_text_is_not_flagged(self, tmp_path: Path) -> None:
        """Only attribute values are checked; label text may contain var(."""
        assert "var(" in GOOD_SVG
        epub = _epub(tmp_path / "g.epub")

        assert check_epub(epub) == []

    def test_no_diagrams_fails(self, tmp_path: Path) -> None:
        epub = _epub(tmp_path / "g.epub", svgs={}, chapters={"c.xhtml": _chapter()})

        errors = check_epub(epub)
        assert errors == ["no Pele-rendered diagrams (images/mermaid_N.svg) found"]

    @pytest.mark.parametrize(
        ("svg", "reason"),
        [
            (
                GOOD_SVG.replace(
                    '<rect x="0" y="0" width="148" height="160" fill="#fff"/>', ""
                ),
                "does not cover viewBox",
            ),
            (
                GOOD_SVG.replace(
                    '<rect x="0" y="0" width="148" height="160" fill="#fff"/>',
                    '<g><rect x="0" y="0" width="148" height="160" fill="#fff"/></g>',
                ),
                "no background <rect>",
            ),
            (
                GOOD_SVG.replace(
                    'height="160" fill="#fff"', 'height="160" fill="none"'
                ),
                "not opaque",
            ),
        ],
        ids=["missing", "not-first-child", "transparent"],
    )
    def test_missing_background_rect_fails(
        self, tmp_path: Path, svg: str, reason: str
    ) -> None:
        epub = _epub(tmp_path / "g.epub", svgs={"mermaid_1.svg": svg})

        errors = check_epub(epub)
        assert len(errors) == 1
        assert reason in errors[0]

    def test_invalid_svg_xml_fails(self, tmp_path: Path) -> None:
        epub = _epub(tmp_path / "g.epub", svgs={"mermaid_1.svg": "<svg><g></svg>"})

        assert any("not valid XML" in e for e in check_epub(epub))

    def test_missing_file_fails(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main([str(tmp_path / "absent.epub")]) == 1
        assert "cannot read EPUB" in capsys.readouterr().out

    def test_one_bad_epub_fails_the_run(self, tmp_path: Path) -> None:
        good = _epub(tmp_path / "good.epub")
        bad = _epub(tmp_path / "bad.epub", svgs={}, chapters={})

        assert main([str(good), str(bad)]) == 1

    def test_no_arguments_is_usage_error(self) -> None:
        with pytest.raises(SystemExit) as exc:
            main([])
        assert exc.value.code == 2

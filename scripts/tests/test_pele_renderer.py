"""Tests for the Pele rendering glue (pele_renderer.py + pele_render.mjs)."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import pele_renderer
from pele_renderer import (
    HELPER,
    PeleHelperError,
    PeleUnavailableError,
    find_node,
    render_diagrams,
)

# Read once at import time on purpose: fixtures that clear CI for other tests
# must not turn the "fail instead of skip in CI" rule below into a skip.
_IN_CI = os.environ.get("CI", "").strip().lower() in {"1", "true", "yes"}

# A stand-in for dist/pele.js that speaks the same protocol as the real
# bundle: render() returns {svg, type, width, height} or throws an object
# carrying a PeleError-style code/line/column.
STUB_BUNDLE = """\
export function render(source, options) {
  if (source.includes('SYNTAX_ERROR')) {
    throw { code: 'syntax', message: 'Unexpected token', line: 2, column: 3 };
  }
  if (source.includes('CRASH')) {
    throw new TypeError('boom');
  }
  const kind = source.startsWith('sequenceDiagram') ? 'sequence' : 'flowchart';
  return { svg: '<svg xmlns="http://www.w3.org/2000/svg"/>', type: kind,
           width: 1, height: 1, links: [] };
}
"""


@pytest.fixture
def logger() -> logging.Logger:
    return logging.getLogger("test_pele_renderer")


@pytest.fixture
def real_node() -> str:
    """A real Node.js binary; skip locally without one, but fail in CI."""
    node = shutil.which("node")
    if node is None:
        if _IN_CI:
            pytest.fail("node is required in CI to test the Pele helper")
        pytest.skip("node is not installed")
    return node


def _completed(
    stdout: str = "", returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["node"], returncode=returncode, stdout=stdout, stderr=stderr
    )


# =============================================================================
# find_node
# =============================================================================


class TestFindNode:
    def test_missing_node(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pele_renderer.shutil, "which", lambda _name: None)
        with pytest.raises(PeleUnavailableError, match=r"Node\.js >= 20"):
            find_node()

    def test_node_too_old(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pele_renderer.shutil, "which", lambda _name: "/x/node")
        monkeypatch.setattr(
            pele_renderer.subprocess,
            "run",
            lambda *_a, **_k: _completed("v18.19.1\n"),
        )
        with pytest.raises(PeleUnavailableError, match="too old"):
            find_node()

    def test_node_20_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pele_renderer.shutil, "which", lambda _name: "/x/node")
        monkeypatch.setattr(
            pele_renderer.subprocess,
            "run",
            lambda *_a, **_k: _completed("v20.1.0\n"),
        )
        assert find_node() == "/x/node"

    def test_unreadable_version(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pele_renderer.shutil, "which", lambda _name: "/x/node")
        monkeypatch.setattr(
            pele_renderer.subprocess,
            "run",
            lambda *_a, **_k: _completed("", returncode=1),
        )
        with pytest.raises(PeleUnavailableError, match="version"):
            find_node()


# =============================================================================
# render_diagrams (helper process mocked)
# =============================================================================


class TestRenderDiagramsMocked:
    @pytest.fixture
    def env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        """Stub node discovery and the bundle so only the protocol is tested."""
        monkeypatch.setattr(pele_renderer, "find_node", lambda: "/x/node")
        monkeypatch.setattr(
            pele_renderer, "ensure_pele_bundle", lambda _logger: tmp_path
        )
        calls: dict[str, Any] = {}
        return calls

    def _fake_run(
        self, monkeypatch: pytest.MonkeyPatch, calls: dict[str, Any], **result: Any
    ) -> None:
        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            calls["cmd"] = cmd
            calls["input"] = kwargs.get("input")
            if isinstance(result.get("raise"), BaseException):
                raise result["raise"]
            return _completed(
                result.get("stdout", ""),
                result.get("returncode", 0),
                result.get("stderr", ""),
            )

        monkeypatch.setattr(pele_renderer.subprocess, "run", fake_run)

    def test_parses_results_in_order(
        self,
        env: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        logger: logging.Logger,
    ) -> None:
        payload = {
            "results": [
                {"svg": "<svg/>", "type": "flowchart", "width": 1, "height": 1},
                {
                    "error": {
                        "code": "syntax",
                        "message": "Unexpected token",
                        "line": 2,
                        "column": 3,
                    }
                },
            ]
        }
        self._fake_run(monkeypatch, env, stdout=json.dumps(payload))

        results = render_diagrams(["graph TD\n A-->B", "graph TD\n A-->"], logger)

        assert env["cmd"] == ["/x/node", str(HELPER), str(tmp_path / "pele.mjs")]
        assert json.loads(env["input"])["diagrams"] == [
            "graph TD\n A-->B",
            "graph TD\n A-->",
        ]
        assert results[0].svg == "<svg/>"
        assert results[0].diagram_type == "flowchart"
        assert results[0].error is None
        assert results[1].svg is None
        assert results[1].error is not None
        assert results[1].error.code == "syntax"
        assert results[1].error.line == 2
        assert results[1].error.describe() == (
            "[syntax] Unexpected token (diagram line 2, column 3)"
        )

    def test_wrong_result_count(
        self, env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, logger: Any
    ) -> None:
        self._fake_run(monkeypatch, env, stdout=json.dumps({"results": []}))
        with pytest.raises(PeleHelperError, match="0 result"):
            render_diagrams(["graph TD\n A-->B"], logger)

    def test_invalid_json(
        self, env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, logger: Any
    ) -> None:
        self._fake_run(monkeypatch, env, stdout="not json")
        with pytest.raises(PeleHelperError, match="invalid JSON"):
            render_diagrams(["graph TD\n A-->B"], logger)

    def test_missing_svg_is_malformed(
        self, env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, logger: Any
    ) -> None:
        self._fake_run(
            monkeypatch, env, stdout=json.dumps({"results": [{"type": "flowchart"}]})
        )
        with pytest.raises(PeleHelperError, match="no SVG"):
            render_diagrams(["graph TD\n A-->B"], logger)

    def test_nonzero_exit(
        self, env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, logger: Any
    ) -> None:
        self._fake_run(monkeypatch, env, returncode=1, stderr="SyntaxError: x")
        with pytest.raises(PeleHelperError, match="status 1: SyntaxError"):
            render_diagrams(["graph TD\n A-->B"], logger)

    def test_timeout(
        self, env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, logger: Any
    ) -> None:
        self._fake_run(
            monkeypatch, env, **{"raise": subprocess.TimeoutExpired("node", 5)}
        )
        with pytest.raises(PeleHelperError, match="timed out"):
            render_diagrams(["graph TD\n A-->B"], logger, timeout=5)

    @pytest.mark.parametrize(
        "error",
        [
            RuntimeError("Checksum mismatch for Pele"),
            OSError("network is unreachable"),
        ],
    )
    def test_bundle_failure_is_unavailable(
        self,
        monkeypatch: pytest.MonkeyPatch,
        logger: logging.Logger,
        error: Exception,
    ) -> None:
        monkeypatch.setattr(pele_renderer, "find_node", lambda: "/x/node")

        def broken_bundle(_logger: logging.Logger) -> Path:
            raise error

        monkeypatch.setattr(pele_renderer, "ensure_pele_bundle", broken_bundle)
        with pytest.raises(PeleUnavailableError, match="bundle"):
            render_diagrams(["graph TD\n A-->B"], logger)


# =============================================================================
# Real helper protocol (Node required, stub bundle, no network)
# =============================================================================


class TestHelperWithNode:
    def test_round_trip_with_stub_bundle(
        self,
        real_node: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        logger: logging.Logger,
    ) -> None:
        (tmp_path / "pele.mjs").write_text(STUB_BUNDLE, encoding="utf-8")
        monkeypatch.setattr(
            pele_renderer, "ensure_pele_bundle", lambda _logger: tmp_path
        )

        results = render_diagrams(
            [
                "graph TD\n  A --> B",
                "graph TD\n  SYNTAX_ERROR",
                "sequenceDiagram\n  A->>B: hi",
                "graph TD\n  CRASH",
            ],
            logger,
        )

        assert [r.diagram_type for r in results] == [
            "flowchart",
            None,
            "sequence",
            None,
        ]
        assert results[0].svg is not None
        assert results[0].svg.startswith("<svg")
        syntax = results[1].error
        assert syntax is not None
        assert (syntax.code, syntax.line, syntax.column) == ("syntax", 2, 3)
        crash = results[3].error
        assert crash is not None
        assert crash.code == "internal"
        assert "boom" in crash.message

    def test_helper_rejects_missing_argument(self, real_node: str) -> None:
        proc = subprocess.run(
            [real_node, str(HELPER)],
            input="{}",
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert proc.returncode == 2
        assert "pele.mjs" in proc.stderr

    def test_bundle_loads_as_esm_without_syntax_detection(
        self,
        real_node: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        logger: logging.Logger,
    ) -> None:
        # Node.js 20.0-20.18 and 22.0-22.6 have no ES module syntax
        # detection: a bare .js bundle loads as CommonJS and fails on
        # `export`. Turn detection off to get that behaviour on newer Node.
        flag = "--no-experimental-detect-module"
        probe = subprocess.run(
            [real_node, flag, "-e", ""], capture_output=True, timeout=60, check=False
        )
        if probe.returncode != 0:
            pytest.skip(f"this Node.js does not accept {flag}")
        monkeypatch.setenv("NODE_OPTIONS", flag)
        bundle = tmp_path / pele_renderer.PELE_ESM_BUNDLE
        bundle.write_text(STUB_BUNDLE, encoding="utf-8")
        monkeypatch.setattr(
            pele_renderer, "ensure_pele_bundle", lambda _logger: tmp_path
        )

        results = render_diagrams(["graph TD\n  A --> B"], logger)

        assert bundle.suffix == ".mjs"
        assert results[0].error is None
        assert results[0].diagram_type == "flowchart"

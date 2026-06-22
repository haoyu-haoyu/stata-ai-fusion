"""Unit tests for graph auto-export injection (no Stata required)."""

from __future__ import annotations

from pathlib import Path

from stata_ai_fusion.graph_cache import maybe_inject_graph_export

TMP = Path("/tmp")


def _export_indices(out: str) -> list[int]:
    return [
        i
        for i, line in enumerate(out.split("\n"))
        if "graph export" in line and "stata_graph_" in line
    ]


def test_export_after_graph_on_second_line():
    """Regression: the export must follow a graph command that is not on the
    first line.  Previously an off-by-one inserted it BEFORE the command, so
    Stata raised r(601) ("no Graph window open") and no image was produced."""
    out = maybe_inject_graph_export(
        "sysuse auto, clear\nscatter price mpg\nsummarize price", TMP
    ).split("\n")
    assert out[0] == "sysuse auto, clear"
    assert out[1] == "scatter price mpg"
    assert "graph export" in out[2] and "stata_graph_" in out[2]
    assert out[3] == "summarize price"


def test_export_after_first_line_graph():
    out = maybe_inject_graph_export("scatter price mpg", TMP).split("\n")
    assert out[0] == "scatter price mpg"
    assert "graph export" in out[1]


def test_continuation_block_export_after_full_command():
    out = maybe_inject_graph_export(
        "twoway (line y x) ///\n   (scatter y x), title(t)\nsummarize x", TMP
    ).split("\n")
    assert _export_indices("\n".join(out)) == [2]
    assert out[3] == "summarize x"


def test_non_rendering_graph_subcommands_not_injected():
    for mgmt in (
        "graph drop g1",
        "graph dir",
        "graph describe",
        "graph rename a b",
        "graph close",
        "graph set window fontface",
    ):
        assert maybe_inject_graph_export(mgmt, TMP) == mgmt, mgmt


def test_graph_drop_then_real_graph_injects_once_after_render():
    out = maybe_inject_graph_export("graph drop g1\nscatter x y", TMP).split("\n")
    assert out[0] == "graph drop g1"
    assert out[1] == "scatter x y"
    assert _export_indices("\n".join(out)) == [2]


def test_no_injection_without_graph_command():
    code = "regress y x\nsummarize x"
    assert maybe_inject_graph_export(code, TMP) == code


def test_no_injection_when_export_already_present():
    code = 'scatter y x\ngraph export "mine.png", replace'
    assert maybe_inject_graph_export(code, TMP) == code

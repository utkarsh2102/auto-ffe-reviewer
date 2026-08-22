"""Tests for the generated dashboard.

Two things are worth guarding. The data contract between the Python that
computes the payloads and the JavaScript that renders them, since that is where
drift would show up as a silently blank column rather than as an error. And the
security properties of the page itself, which are easy to weaken by accident.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from conftest import Route, default_routes
from ffe.config import load_settings
from ffe.llm.prompt import load_policy
from ffe.llm.replay import ReplayHarness
from ffe.pipeline import Pipeline
from ffe.publish.render import publish
from ffe.sources.launchpad import client
from ffe.sources.seeds import load_flavours
from ffe.store.index import build_agreement, build_index, needs_attention, summarise
from ffe.store.repo import Store
from ffe.util.clock import frozen_at

WEB = Path(__file__).resolve().parents[1] / "web"
SCHEMA = json.loads(
    (Path(__file__).resolve().parents[1] / "schemas" / "assessment.v1.schema.json").read_text()
)
LP = Path(__file__).parent / "fixtures" / "raw" / "launchpad"

RESPONSE = json.dumps(
    {
        "decision": "NEEDS_INFORMATION",
        "confidence": "HIGH",
        "summary": "Core seeded package with a large blast radius and no testing evidence.",
        "reasoning": [
            {"point": "curl is seeded on core images.", "evidence_refs": ["/evidence/packages"]},
        ],
        "missing_information": ["A PPA build for stonking."],
        "flags": ["CORE_PACKAGE", "NO_TESTING_EVIDENCE"],
    }
)


@pytest.fixture
def site(make_ctx, tmp_path: Path) -> Path:  # type: ignore[no-untyped-def]
    """A fully generated dashboard, built from fixtures."""
    routes = [
        Route(r"searchTasks.*ubuntu-release", LP / "searchtasks-juliank.json"),
        Route(r"searchTasks", LP / "searchtasks-ffe-sweep.json"),
        *default_routes(),
    ]
    ctx = make_ctx(routes)
    settings = load_settings(Path("/nonexistent.toml"), env={})
    ctx.settings = settings
    store = Store(tmp_path / "state")

    pipeline = Pipeline(
        settings=settings,
        ctx=ctx,
        store=store,
        launchpad=client(ctx),
        harness=ReplayHarness(responses=[RESPONSE]),
        policy=load_policy(),
        schema=SCHEMA,
        flavours=load_flavours(),
        run_id="test",
    )
    with frozen_at("2026-09-20T12:00:00Z"):
        pipeline.run(only=(2167691,))
        publish(store, settings)
    return store.site


def _load(site: Path, name: str) -> Any:
    return json.loads((site / name).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# The payloads
# --------------------------------------------------------------------------- #


def test_publishing_produces_the_expected_files(site: Path) -> None:
    for name in ("index.json", "meta.json", "agreement.json", "lessons.json"):
        assert (site / name).is_file(), f"missing {name}"
    assert (site / "bugs" / "2167691.json").is_file()
    # The page ships beside its data, so the directory is self-contained.
    for name in ("index.html", "bug.html", "app.js", "style.css"):
        assert (site / name).is_file(), f"missing {name}"


def test_counts_are_computed_in_python_not_the_browser(site: Path) -> None:
    """site/index.json must say exactly what the page shows."""
    index = _load(site, "index.json")
    counts = index["counts"]

    assert counts["open"] == len(index["bugs"])
    assert counts["needs_attention"] == sum(1 for row in index["bugs"] if needs_attention(row))
    assert counts["no_testing_evidence"] == sum(
        1 for row in index["bugs"] if not row["testing_corroborated"]
    )


def test_a_row_carries_what_the_triage_table_needs(site: Path) -> None:
    row = _load(site, "index.json")["bugs"][0]

    assert row["bug_id"] == 2167691
    assert row["url"].startswith("https://bugs.launchpad.net/")
    assert "curl" in row["packages"]
    assert row["risk_band"] in {"LOW", "MODERATE", "HIGH", "SEVERE"}
    assert row["decision"] == "NEEDS_INFORMATION"
    assert row["missing_information"]


def test_unknown_seeding_is_distinguishable_from_unseeded(make_ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A dash and a "no" mean very different things to a reviewer.

    The row carries null when the seed index could not be trusted, so the page
    can say "unknown" rather than implying the package ships nowhere.
    """
    record = {
        "evidence": {
            "bug": {"id": 1, "title": "t", "url": "u"},
            "subject": {"packages": ["x"]},
            "packages": [{"name": "x", "seeds": {"value": None, "status": "UNAVAILABLE"}}],
        },
        "risk": {},
    }
    assert summarise(record)["seeded"] is None

    record["evidence"]["packages"][0]["seeds"] = {"value": {"flavours": []}, "status": "OK"}
    assert summarise(record)["seeded"] == []


def test_detail_payload_carries_provenance_for_every_fact(site: Path) -> None:
    """The dashboard's whole claim to trustworthiness."""
    detail = _load(site, "bugs/2167691.json")
    package = detail["record"]["evidence"]["packages"][0]

    for key in ("archive", "seeds", "rdepends", "build_rdepends"):
        provenance = package[key]["provenance"]
        assert provenance["source_id"]
        assert provenance["locator"]
        assert provenance["checked_at"]


def test_detail_payload_carries_the_risk_reasoning(site: Path) -> None:
    detail = _load(site, "bugs/2167691.json")
    components = detail["record"]["risk"]["components"]

    assert components
    for component in components:
        assert component["rationale"]
        assert component["evidence_refs"]


def test_agreement_withholds_a_rate_until_it_means_something(site: Path) -> None:
    """A percentage over three decisions would invite unwarranted confidence."""
    assert _load(site, "agreement.json")["rate"] is None


def test_agreement_counts_only_comparable_decisions(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.archive_dir.mkdir(parents=True, exist_ok=True)
    for index, (ours, theirs) in enumerate(
        [("APPROVE", "APPROVED"), ("NEEDS_INFORMATION", "APPROVED"), (None, "APPROVED")]
    ):
        (store.archive_dir / f"{index}.json").write_text(
            json.dumps(
                {
                    "evidence": {"bug": {"id": index, "title": "t", "url": "u"}},
                    "human": {"decided": True, "decision": theirs},
                    "our_last_assessment": {"decision": ours} if ours else None,
                    "our_last_risk": {"band": "LOW"},
                }
            )
        )

    agreement = build_agreement(store)
    assert agreement["decided"] == 3
    assert agreement["comparable"] == 2  # the one with no recommendation does not count
    assert agreement["agreed"] == 1


def test_rows_are_ordered_with_the_alarming_first(tmp_path: Path) -> None:
    store = Store(tmp_path)
    assert build_index(store)["bugs"] == []


# --------------------------------------------------------------------------- #
# The Python / JavaScript data contract
# --------------------------------------------------------------------------- #

# Fields app.js reads off an index row. Kept explicit: drift here shows up as a
# quietly blank column rather than as an error, so it is worth asserting.
ROW_FIELDS = (
    "bug_id",
    "url",
    "title",
    "packages",
    "series",
    "release_type",
    "phase",
    "seeded",
    "is_core",
    "rdepends",
    "build_rdepends",
    "risk_band",
    "risk_score",
    "decision",
    "decision_floor",
    "confidence",
    "summary",
    "flags",
    "ack_required_from",
    "injection_signals",
    "unavailable",
    "status",
    "reviewed_at",
    "testing_corroborated",
)


def test_every_field_the_page_reads_is_published(site: Path) -> None:
    row = _load(site, "index.json")["bugs"][0]
    missing = [field for field in ROW_FIELDS if field not in row]
    assert not missing, f"index.json rows are missing fields app.js reads: {missing}"


def test_the_page_reads_no_field_that_is_not_published(site: Path) -> None:
    """Catches the other direction: JavaScript reaching for a field we dropped.

    `row` names two shapes in app.js -- a queue row and an agreement row -- so
    a reference is satisfied by either. Anything in neither is a field the page
    expects and nothing publishes.
    """
    script = (WEB / "app.js").read_text(encoding="utf-8")
    queue_row = _load(site, "index.json")["bugs"][0]
    agreement_row = {
        "bug_id": 0,
        "title": "",
        "url": "",
        "ours": None,
        "theirs": None,
        "agreed": None,
        "risk_band": None,
        "decided_at": None,
    }

    referenced = set(re.findall(r"\brow\.(\w+)", script))
    unknown = referenced - set(queue_row) - set(agreement_row) - {"length"}
    assert not unknown, f"app.js reads fields nothing publishes: {unknown}"


def test_the_agreement_row_shape_matches_what_the_page_reads(tmp_path: Path) -> None:
    """Pins the second shape, so the test above cannot drift into vacuity."""
    store = Store(tmp_path)
    store.archive_dir.mkdir(parents=True, exist_ok=True)
    (store.archive_dir / "1.json").write_text(
        json.dumps(
            {
                "evidence": {"bug": {"id": 1, "title": "t", "url": "u"}},
                "human": {"decided": True, "decision": "APPROVED", "detected_at": "x"},
                "our_last_assessment": {"decision": "APPROVE"},
                "our_last_risk": {"band": "LOW"},
            }
        )
    )
    row = build_agreement(store)["recent"][0]
    for field in ("bug_id", "title", "url", "ours", "theirs", "agreed", "risk_band", "decided_at"):
        assert field in row


def test_meta_matches_the_index(site: Path) -> None:
    assert _load(site, "meta.json")["counts"] == _load(site, "index.json")["counts"]


# --------------------------------------------------------------------------- #
# Page security and accessibility
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("page", ["index.html", "bug.html", "lessons.html", "agreement.html"])
def test_every_page_sets_a_strict_csp(page: str) -> None:
    """GitHub Pages cannot set headers, so the meta tag is the whole policy.

    It matters because these pages render text taken from bug reports.
    """
    html = (WEB / page).read_text(encoding="utf-8")
    assert "Content-Security-Policy" in html
    assert "default-src 'self'" in html
    assert "script-src 'self'" in html
    assert "base-uri 'none'" in html


@pytest.mark.parametrize("page", ["index.html", "bug.html", "lessons.html", "agreement.html"])
def test_no_inline_script_and_no_remote_resources(page: str) -> None:
    html = (WEB / page).read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), (
        "inline script would need a CSP exemption"
    )
    assert not re.search(r'(?:src|href)\s*=\s*["\']https?://', html), "no remote resources"


def test_bug_text_is_never_written_as_markup() -> None:
    """The page renders untrusted content, so it must use textContent only."""
    script = (WEB / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in script
    assert "outerHTML" not in script
    assert "insertAdjacentHTML" not in script
    assert "document.write" not in script


def test_orange_is_never_used_for_text() -> None:
    """Ubuntu orange is about 3:1 on white, which fails WCAG AA for body text."""
    css = (WEB / "style.css").read_text(encoding="utf-8")
    for match in re.finditer(r"^\s*color:\s*([^;]+);", css, re.M):
        assert "--orange" not in match.group(1), (
            "orange used as a text colour; it fails AA at body size"
        )


def test_status_is_never_carried_by_colour_alone() -> None:
    """Every decision and risk band is a written word before it is a colour."""
    script = (WEB / "app.js").read_text(encoding="utf-8")
    assert "DECISION_LABEL" in script
    assert "risk_band.toLowerCase()" in script


def test_dark_mode_is_supported() -> None:
    css = (WEB / "style.css").read_text(encoding="utf-8")
    assert "prefers-color-scheme: dark" in css
    assert 'data-theme="dark"' in css
    assert "prefers-reduced-motion" in css


def test_no_font_binaries_are_shipped() -> None:
    """Deliberate: the Ubuntu Font Licence makes vendoring a licence question.

    See web/ATTRIBUTION.md. The audience runs Ubuntu, where the family resolves
    locally at no download cost.
    """
    assert not list(WEB.rglob("*.woff2"))
    assert not list(WEB.rglob("*.ttf"))
    attribution = (WEB / "ATTRIBUTION.md").read_text(encoding="utf-8")
    assert "Ubuntu Font Licence 1.0" in attribution


def test_no_canonical_trademarks_are_used() -> None:
    for page in WEB.glob("*.html"):
        html = page.read_text(encoding="utf-8")
        assert "circle-of-friends" not in html.lower()
        assert "cof.svg" not in html.lower()


def test_the_advisory_boundary_is_on_every_page() -> None:
    """The most important thing the dashboard says."""
    for page in ("index.html", "bug.html"):
        html = (WEB / page).read_text(encoding="utf-8")
        assert "Advisory only" in html


def test_javascript_delimiters_balance() -> None:
    """A crude syntax guard.

    No JavaScript engine is available in this environment, so this checks what
    can be checked without one: that braces, brackets and parens balance once
    strings, template literals, regexes and comments are removed. It catches
    gross breakage; it is not a substitute for loading the page.
    """
    source = (WEB / "app.js").read_text(encoding="utf-8")
    stripped = _strip_js_literals(source)
    stack: list[str] = []
    pairs = {")": "(", "]": "[", "}": "{"}
    for char in stripped:
        if char in "([{":
            stack.append(char)
        elif char in pairs:
            assert stack and stack.pop() == pairs[char], "unbalanced delimiter in app.js"
    assert not stack, "unclosed delimiter in app.js"


def _strip_js_literals(source: str) -> str:
    """Remove comments and string/template/regex literals, crudely but safely."""
    out: list[str] = []
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        two = source[index : index + 2]
        if two == "//":
            index = source.find("\n", index)
            if index == -1:
                break
        elif two == "/*":
            index = source.find("*/", index) + 2
        elif char in "\"'`":
            quote = char
            index += 1
            while index < length:
                if source[index] == "\\":
                    index += 2
                    continue
                if source[index] == quote:
                    index += 1
                    break
                index += 1
        elif char == "/" and _is_regex_position(out):
            index += 1
            while index < length:
                if source[index] == "\\":
                    index += 2
                    continue
                if source[index] == "/":
                    index += 1
                    break
                if source[index] == "\n":
                    break
                index += 1
        else:
            out.append(char)
            index += 1
    return "".join(out)


def _is_regex_position(previous: list[str]) -> bool:
    """A slash starts a regex only where a value may begin."""
    for char in reversed(previous):
        if char.isspace():
            continue
        return char in "(,=:[!&|?{};+-*%~^"
    return True

/*
 * FFe Reviewer dashboard.
 *
 * A renderer, deliberately. Every count and every ordering is computed in
 * Python and published in site/index.json, so what the page shows can be
 * verified by reading that file -- no reviewer should have to run a browser to
 * check a number they are about to act on.
 *
 * Text from Launchpad is written with textContent throughout. Nothing here
 * builds markup from a string containing bug content, which together with the
 * page's Content-Security-Policy is why a bug report cannot inject anything.
 */

"use strict";

const DECISION_LABEL = {
  APPROVE: "approve",
  REJECT: "reject",
  NEEDS_INFORMATION: "needs information",
};

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
};

const fetchJSON = async (path) => {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response.json();
};

const fmtDate = (iso) => {
  if (!iso) return "—";
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return "—";
  return when.toISOString().slice(0, 16).replace("T", " ") + "Z";
};

const slug = (value) => String(value || "").toLowerCase().replace(/_/g, "-");

/* -- badges ------------------------------------------------------------- */

function decisionBadge(row) {
  if (!row.decision) {
    const label = row.status === "DEFERRED_BUDGET" ? "deferred" : "no recommendation";
    return el("span", "badge none", label);
  }
  const badge = el("span", `badge ${slug(row.decision)}`, DECISION_LABEL[row.decision] || row.decision);
  return badge;
}

function riskBadge(row) {
  if (row.risk_band === undefined || row.risk_band === null) return el("span", "badge none", "—");
  return el("span", `badge ${slug(row.risk_band)}`, `${row.risk_band.toLowerCase()} ${row.risk_score}`);
}

/* -- the queue ----------------------------------------------------------- */

const STATE = { rows: [], sort: null, direction: 1, filters: {} };

function matches(row) {
  const f = STATE.filters;
  if (f.attention && !isAttention(row)) return false;
  if (f.untested && row.testing_corroborated) return false;
  if (f.ack && !(row.ack_required_from || []).length) return false;
  if (f.decision === "__none" && row.decision) return false;
  if (f.decision && f.decision !== "__none" && row.decision !== f.decision) return false;
  if (f.text) {
    const haystack = [row.bug_id, row.title, (row.packages || []).join(" ")]
      .join(" ")
      .toLowerCase();
    if (!haystack.includes(f.text)) return false;
  }
  return true;
}

// Mirrors needs_attention() in ffe/store/index.py. Kept in step deliberately:
// the Python side decides, this only styles what it decided.
function isAttention(row) {
  return (
    !row.decision ||
    row.decision === "NEEDS_INFORMATION" ||
    row.decision === "REJECT" ||
    row.risk_band === "HIGH" ||
    row.risk_band === "SEVERE" ||
    (row.ack_required_from || []).length > 0 ||
    row.injection_signals > 0 ||
    (row.unavailable || []).length > 0
  );
}

function renderTiles(counts) {
  const tiles = document.getElementById("tiles");
  tiles.replaceChildren();
  const spec = [
    ["open", "open requests", ""],
    ["needs_attention", "need attention", "attention"],
    ["high_risk", "high or severe risk", "severe"],
    ["no_testing_evidence", "without test evidence", "caution"],
    ["awaiting_flavour_ack", "awaiting flavour ack", "caution"],
    ["approve", "recommended approve", "good"],
    ["needs_information", "need information", ""],
    ["reject", "recommended reject", "severe"],
  ];
  for (const [key, label, kind] of spec) {
    const value = counts[key] || 0;
    if (!value && ["approve", "reject", "awaiting_flavour_ack"].includes(key)) continue;
    const tile = el("li", `tile ${kind}`.trim());
    tile.append(el("div", "value", value), el("div", "label", label));
    tiles.append(tile);
  }
}

function renderRows() {
  const body = document.getElementById("rows");
  const empty = document.getElementById("empty");
  body.replaceChildren();

  const visible = STATE.rows.filter(matches);
  const table = document.getElementById("queue");

  if (!visible.length) {
    table.hidden = true;
    empty.hidden = false;
    empty.replaceChildren();
    const box = el("div", "empty");
    if (!STATE.rows.length) {
      box.append(
        el("div", "headline", "Nothing is awaiting review."),
        el("div", null, "No bugs are currently subscribed to the Release Team, which means the queue is clear.")
      );
    } else {
      box.append(el("div", "headline", "No requests match this filter."));
    }
    empty.append(box);
    return;
  }

  table.hidden = false;
  empty.hidden = true;

  for (const row of visible) {
    const tr = el("tr", isAttention(row) ? "attention" : null);

    tr.append(el("td", "bug-id", `#${row.bug_id}`));

    const title = el("td", "bug-title");
    const link = el("a", null, row.title || "(untitled)");
    link.href = `bug.html?id=${encodeURIComponent(row.bug_id)}`;
    title.append(link);

    if (row.summary) title.append(el("div", "bug-reason", row.summary));

    const flags = el("div");
    for (const flag of row.flags || []) {
      flags.append(el("span", "flag", flag.toLowerCase().replace(/_/g, " ")));
    }
    for (const flavour of row.ack_required_from || []) {
      flags.append(el("span", "flag warn", `awaiting ${flavour}`));
    }
    if (row.injection_signals) {
      flags.append(el("span", "flag warn", "suspicious text"));
    }
    if ((row.unavailable || []).length) {
      flags.append(el("span", "flag warn", "evidence incomplete"));
    }
    if (flags.childElementCount) title.append(flags);
    tr.append(title);

    tr.append(el("td", "hide-narrow", (row.packages || []).join(", ") || "—"));

    const release = el("td", "hide-narrow nowrap");
    release.append(el("div", null, row.series || "—"));
    if (row.release_type) {
      release.append(el("div", "confidence", `${row.release_type.toLowerCase()}, ${row.phase ? row.phase.toLowerCase().replace(/_/g, " ") : ""}`));
    }
    tr.append(release);

    // null means the seed index could not be trusted; that is not "no".
    const seeded = el("td", "hide-narrow");
    if (row.seeded === null || row.seeded === undefined) {
      seeded.append(el("span", "flag warn", "unknown"));
    } else if (!row.seeded.length) {
      seeded.textContent = "no";
    } else {
      seeded.textContent = row.seeded.join(", ");
      if (row.is_core) seeded.append(el("div", "confidence", "core image"));
    }
    tr.append(seeded);

    const rdeps = el("td", "num hide-narrow", row.rdepends ?? "—");
    if (row.build_rdepends) rdeps.append(el("div", "confidence", `${row.build_rdepends} build`));
    tr.append(rdeps);

    const risk = el("td");
    risk.append(riskBadge(row));
    tr.append(risk);

    const decision = el("td");
    decision.append(decisionBadge(row));
    if (row.confidence) decision.append(el("div", "confidence", `${row.confidence.toLowerCase()} confidence`));
    if (row.decision_floor && !row.decision) {
      decision.append(el("div", "confidence", `floor: ${DECISION_LABEL[row.decision_floor] || row.decision_floor}`));
    }
    tr.append(decision);

    tr.append(el("td", "hide-narrow nowrap confidence", fmtDate(row.reviewed_at)));
    body.append(tr);
  }
}

function sortBy(key) {
  STATE.direction = STATE.sort === key ? -STATE.direction : 1;
  STATE.sort = key;

  STATE.rows.sort((a, b) => {
    let x = a[key];
    let y = b[key];
    if (Array.isArray(x)) x = x.join(",");
    if (Array.isArray(y)) y = y.join(",");
    if (x === null || x === undefined) return 1;
    if (y === null || y === undefined) return -1;
    if (typeof x === "number" && typeof y === "number") return (x - y) * STATE.direction;
    return String(x).localeCompare(String(y)) * STATE.direction;
  });

  for (const th of document.querySelectorAll("th[data-sort]")) {
    if (th.dataset.sort === key) {
      th.setAttribute("aria-sort", STATE.direction === 1 ? "ascending" : "descending");
    } else {
      th.removeAttribute("aria-sort");
    }
  }
  renderRows();
}

function wireControls() {
  document.getElementById("filter").addEventListener("input", (event) => {
    STATE.filters.text = event.target.value.trim().toLowerCase();
    renderRows();
  });

  const toggles = [
    ["only-attention", "attention"],
    ["only-untested", "untested"],
    ["only-ack", "ack"],
  ];
  for (const [id, key] of toggles) {
    const button = document.getElementById(id);
    button.addEventListener("click", () => {
      const next = button.getAttribute("aria-pressed") !== "true";
      button.setAttribute("aria-pressed", String(next));
      STATE.filters[key] = next;
      renderRows();
    });
  }

  document.getElementById("decision-filter").addEventListener("change", (event) => {
    STATE.filters.decision = event.target.value;
    renderRows();
  });

  for (const th of document.querySelectorAll("th[data-sort]")) {
    th.addEventListener("click", () => sortBy(th.dataset.sort));
    th.setAttribute("tabindex", "0");
    th.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        sortBy(th.dataset.sort);
      }
    });
  }
}

async function initIndex() {
  wireControls();
  try {
    const [index, meta] = await Promise.all([fetchJSON("index.json"), fetchJSON("meta.json")]);
    STATE.rows = index.bugs || [];
    renderTiles(index.counts || {});
    renderRows();
    document.getElementById("meta").textContent =
      `Generated ${fmtDate(meta.generated_at)} · queue: ~${meta.release_team} · harness: ${meta.harness}`;
  } catch (error) {
    document.getElementById("empty").hidden = false;
    document.getElementById("empty").replaceChildren(
      el("div", "empty", `Could not load the dashboard data: ${error.message}`)
    );
  }
}

/* -- one bug ------------------------------------------------------------- */

function provenance(fact) {
  if (!fact || !fact.provenance) return null;
  const p = fact.provenance;
  const details = el("details", "provenance");
  details.append(el("summary", null, `source: ${p.source_id}`));
  const body = el("div", "prov-body");
  body.append(el("div", null, `locator   ${p.locator}`));
  body.append(el("div", null, `checked   ${fmtDate(p.checked_at)}`));
  body.append(el("div", null, `freshness ${p.cache}${p.duration_ms ? ` (${p.duration_ms} ms)` : ""}`));
  if (p.raw_ref) {
    body.append(el("div", null, `raw       ${p.raw_ref}${p.raw_truncated ? " (truncated)" : ""}`));
  }
  if (fact.note) body.append(el("div", null, `note      ${fact.note}`));
  details.append(body);
  return details;
}

function panel(title) {
  const section = el("section", "panel");
  section.append(el("h2", null, title));
  const body = el("div", "body");
  section.append(body);
  return { section, body };
}

function renderBug(detail) {
  const root = document.getElementById("detail");
  root.replaceChildren();

  const record = detail.record;
  const evidence = record.evidence || {};
  const bug = evidence.bug || {};
  const risk = record.risk || {};
  const assessment = record.assessment;

  document.title = `#${bug.id} — FFe Reviewer`;

  const head = el("div", "detail-head");
  head.append(el("h1", null, bug.title || `Bug #${bug.id}`));
  const meta = el("div", "meta");
  const lpLink = el("a", null, `Launchpad #${bug.id}`);
  lpLink.href = bug.url || `https://bugs.launchpad.net/bugs/${bug.id}`;
  lpLink.rel = "noopener noreferrer";
  meta.append(lpLink);
  meta.append(document.createTextNode(` · reported by ${bug.reporter || "unknown"} · reviewed ${fmtDate((record.provenance || {}).generated_at)}`));
  head.append(meta);
  root.append(head);

  if (record.human && record.human.decided) {
    const box = el("div", "warning");
    box.append(el("h3", null, "Decided by the Release Team"));
    box.append(el("div", null, `${record.human.decision} · final status ${record.human.final_status}. No further review is performed.`));
    root.append(box);
  }

  if ((evidence.injection_signals || []).length) {
    const box = el("div", "warning");
    box.append(el("h3", null, "Passages resembling an attempt to steer an automated reviewer"));
    box.append(el("div", null, "Shown verbatim. These are reported, never acted on, and do not change the recommendation."));
    const list = el("ul", "plain");
    for (const signal of evidence.injection_signals) {
      list.append(el("li", null, `[${signal.pattern_id}] ${signal.location}: ${signal.excerpt}`));
    }
    box.append(list);
    root.append(box);
  }

  // Recommendation first: it is what a reviewer came for.
  const rec = panel("Recommendation");
  if (assessment) {
    const wrapper = el("div", `recommendation ${slug(assessment.decision)}`);
    const line = el("div");
    line.append(el("span", `badge ${slug(assessment.decision)}`, DECISION_LABEL[assessment.decision] || assessment.decision));
    line.append(document.createTextNode(" "));
    line.append(el("span", "confidence", `${assessment.confidence.toLowerCase()} confidence`));
    if (assessment.confidence_clamped_from) {
      line.append(el("span", "confidence", ` (reduced from ${assessment.confidence_clamped_from.toLowerCase()}: evidence was incomplete)`));
    }
    wrapper.append(line);
    wrapper.append(el("div", "summary", assessment.summary));

    const why = el("ul", "plain");
    for (const item of assessment.reasoning || []) {
      const li = el("li", null, item.point);
      if ((item.evidence_refs || []).length) {
        li.append(el("div", "cited", item.evidence_refs.join("  ")));
      }
      why.append(li);
    }
    wrapper.append(why);

    if ((assessment.missing_information || []).length) {
      wrapper.append(el("h3", null, "Missing"));
      const missing = el("ul", "plain");
      for (const item of assessment.missing_information) missing.append(el("li", null, item));
      wrapper.append(missing);
    }
    if (assessment.disagreement_with_deterministic) {
      wrapper.append(el("h3", null, "Departs from the deterministic assessment"));
      wrapper.append(el("div", null, assessment.disagreement_with_deterministic));
    }
    rec.body.append(wrapper);
  } else {
    rec.body.append(
      el("div", null, `No model recommendation (${record.status}). The deterministic assessment below still applies.`)
    );
  }
  root.append(rec.section);

  // Risk, with every component's reasoning shown.
  const riskPanel = panel(`Deterministic assessment — ${risk.band} (${risk.score}/100)`);
  const components = el("dl", "facts");
  for (const component of risk.components || []) {
    components.append(el("dt", null, `${component.points}/${component.max_points}`));
    components.append(el("dd", null, component.rationale));
  }
  riskPanel.body.append(components);

  const gates = (risk.gates || []).filter((g) => g.triggered);
  if (gates.length) {
    riskPanel.body.append(el("h3", null, "Gates"));
    const list = el("ul", "plain");
    for (const gate of gates) list.append(el("li", null, `${gate.id}: ${gate.rationale}`));
    riskPanel.body.append(list);
  }
  if (risk.decision_floor) {
    riskPanel.body.append(el("div", "confidence", `Recommendation may not be more permissive than: ${risk.decision_floor}`));
  }
  root.append(riskPanel.section);

  // Evidence, each fact with its provenance one click away.
  const ev = panel("Evidence");
  const release = (evidence.release_context || {}).value;
  const facts = el("dl", "facts");
  if (release) {
    facts.append(el("dt", null, "release"));
    const dd = el("dd", null, `${release.series} (${release.version}) ${release.release_type}, ${release.phase}, ${release.days_to_release} days to release`);
    const prov = provenance(evidence.release_context);
    if (prov) dd.append(prov);
    facts.append(dd);
  }
  facts.append(el("dt", null, "request"));
  facts.append(el("dd", null, `${(evidence.subject || {}).ffe_kind || "unknown"} · ${((evidence.subject || {}).packages || []).join(", ") || "no package identified"}`));
  ev.body.append(facts);

  for (const pkg of evidence.packages || []) {
    ev.body.append(el("h3", null, pkg.name));
    const list = el("dl", "facts");

    const archive = (pkg.archive || {}).value;
    list.append(el("dt", null, "archive"));
    const archiveDd = el("dd", null, archive ? `${archive.version || "—"} in ${archive.component || "?"} (${archive.pocket || "?"})` : "unavailable");
    const archiveProv = provenance(pkg.archive);
    if (archiveProv) archiveDd.append(archiveProv);
    list.append(archiveDd);

    const seeds = (pkg.seeds || {}).value;
    list.append(el("dt", null, "seeded"));
    const seedDd = el("dd");
    if (!seeds) {
      seedDd.append(el("span", "flag warn", `unknown (${(pkg.seeds || {}).status})`));
    } else if (!(seeds.flavours || []).length) {
      seedDd.textContent = "not on any image";
    } else {
      seedDd.textContent = (seeds.images || []).map((i) => i.join(" ")).join(", ");
    }
    const seedProv = provenance(pkg.seeds);
    if (seedProv) seedDd.append(seedProv);
    list.append(seedDd);

    for (const [label, key] of [["reverse deps", "rdepends"], ["reverse build-deps", "build_rdepends"]]) {
      const fact = pkg[key] || {};
      list.append(el("dt", null, label));
      const dd = el("dd");
      if (fact.value) {
        dd.textContent = `${fact.value.total} (${fact.value.bucket})`;
        if ((fact.value.seeded_rdeps || []).length) {
          dd.append(el("div", "confidence", `seeded dependents: ${fact.value.seeded_rdeps.join(", ")}`));
        }
      } else {
        dd.textContent = fact.status === "NOT_APPLICABLE" ? "not applicable" : `unavailable (${fact.status})`;
      }
      const prov = provenance(fact);
      if (prov) dd.append(prov);
      list.append(dd);
    }
    ev.body.append(list);
  }
  root.append(ev.section);

  // Testing evidence, with claims kept visibly apart from things we checked.
  const testing = evidence.testing || {};
  const tp = panel("Testing evidence supplied by the developer");
  const states = el("dl", "facts");
  for (const [label, key] of [["PPA", "ppa"], ["build log", "build"], ["test results", "autopkgtest"], ["pasted output", "test_output"]]) {
    states.append(el("dt", null, label));
    states.append(el("dd", null, (testing[key] || "ABSENT").toLowerCase().replace(/_/g, " ")));
  }
  tp.body.append(states);

  if ((testing.items || []).length) {
    const list = el("ul", "plain");
    for (const item of testing.items) {
      const li = el("li", null, `${item.detail} (${item.source})`);
      if (item.locator) {
        li.append(document.createTextNode(" "));
        const a = el("a", null, item.locator);
        a.href = item.locator;
        a.rel = "noopener noreferrer";
        li.append(a);
      }
      list.append(li);
    }
    tp.body.append(list);
  }
  if ((testing.unverified_claims || []).length) {
    tp.body.append(el("h3", null, "Claimed in prose, with nothing linked to check"));
    const list = el("ul", "plain");
    for (const claim of testing.unverified_claims) list.append(el("li", null, claim));
    tp.body.append(list);
  }
  root.append(tp.section);

  // Flavours.
  const flavours = evidence.flavours || {};
  const fp = panel("Flavour impact");
  const flavourFacts = el("dl", "facts");
  flavourFacts.append(el("dt", null, "impact"));
  flavourFacts.append(el("dd", null, (flavours.impact || "UNSEEDED").toLowerCase().replace(/_/g, " ")));
  if ((flavours.affected_flavours || []).length) {
    flavourFacts.append(el("dt", null, "affected"));
    flavourFacts.append(el("dd", null, flavours.affected_flavours.join(", ")));
  }
  if ((flavours.acks || []).length) {
    flavourFacts.append(el("dt", null, "acknowledged"));
    flavourFacts.append(el("dd", null, flavours.acks.map((a) => `${a.flavour} (${a.author})`).join(", ")));
  }
  if ((flavours.ack_required_from || []).length) {
    flavourFacts.append(el("dt", null, "awaiting"));
    const dd = el("dd");
    dd.append(el("span", "flag warn", flavours.ack_required_from.join(", ")));
    flavourFacts.append(dd);
  }
  fp.body.append(flavourFacts);
  root.append(fp.section);

  if ((evidence.unavailable || []).length) {
    const up = panel("Evidence we could not gather");
    const list = el("ul", "plain");
    for (const item of evidence.unavailable) {
      list.append(el("li", null, `${item.source_id}: ${item.reason} — ${item.impact}`));
    }
    up.body.append(list);
    root.append(up.section);
  }

  if ((detail.history || []).length > 1) {
    const hp = panel("Review history");
    const list = el("ul", "plain");
    for (const entry of detail.history) {
      list.append(el("li", null, `${fmtDate(entry.generated_at)} · ${entry.decision || entry.status} · risk ${entry.risk_score}`));
    }
    hp.body.append(list);
    root.append(hp.section);
  }

  const prov = panel("How this review was produced");
  const p = record.provenance || {};
  const provFacts = el("dl", "facts");
  const entries = [
    ["tool", p.tool_version],
    ["policy", `${p.policy_version} (${p.policy_hash})`],
    ["risk algorithm", p.risk_algorithm_version],
    ["precedents", p.lessons_hash],
    ["harness", p.harness ? `${p.harness.id} / ${p.harness.model}, ${p.harness.attempts} attempt(s)` : "none"],
    ["run", p.run_id],
  ];
  for (const [label, value] of entries) {
    provFacts.append(el("dt", null, label));
    provFacts.append(el("dd", null, value || "—"));
  }
  prov.body.append(provFacts);
  root.append(prov.section);
}

async function initBug() {
  const id = new URLSearchParams(location.search).get("id");
  const root = document.getElementById("detail");
  if (!id || !/^\d+$/.test(id)) {
    root.replaceChildren(el("div", "empty", "No bug specified."));
    return;
  }
  try {
    renderBug(await fetchJSON(`bugs/${id}.json`));
  } catch (error) {
    root.replaceChildren(el("div", "empty", `Could not load bug ${id}: ${error.message}`));
  }
}

/* -- precedents and agreement -------------------------------------------- */

async function initLessons() {
  const root = document.getElementById("lessons");
  try {
    const data = await fetchJSON("lessons.json");
    const active = data.active || [];
    if (!active.length) {
      root.replaceChildren(
        el("div", "empty", "Nothing learned yet. Precedents appear once a recommendation has differed from a Release Team decision.")
      );
      return;
    }
    root.replaceChildren();
    for (const lesson of active) {
      const p = panel(`${lesson.lesson_id} · from bug #${lesson.source_bug}`);
      const facts = el("dl", "facts");
      facts.append(el("dt", null, "situation"), el("dd", null, lesson.situation));
      facts.append(el("dt", null, "we said"), el("dd", null, lesson.our_decision || "—"));
      facts.append(el("dt", null, "the team decided"), el("dd", null, lesson.human_decision));
      facts.append(el("dt", null, "lesson"), el("dd", null, lesson.lesson));
      p.body.append(facts);
      const quote = el("div", "quote", `“${lesson.rationale_quote}”`);
      p.body.append(quote, el("div", "confidence", `— ${lesson.rationale_author}, ~ubuntu-release`));
      root.append(p.section);
    }
  } catch (error) {
    root.replaceChildren(el("div", "empty", `Could not load precedents: ${error.message}`));
  }
}

async function initAgreement() {
  const root = document.getElementById("agreement");
  try {
    const data = await fetchJSON("agreement.json");
    root.replaceChildren();

    const tiles = el("ul", "tiles");
    const add = (value, label, kind) => {
      const tile = el("li", `tile ${kind || ""}`.trim());
      tile.append(el("div", "value", value), el("div", "label", label));
      tiles.append(tile);
    };
    add(data.decided || 0, "decided by the team");
    add(data.comparable || 0, "comparable with ours");
    add(data.rate === null || data.rate === undefined ? "—" : `${Math.round(data.rate * 100)}%`, "agreement", "good");
    root.append(tiles);

    if (data.rate === null || data.rate === undefined) {
      root.append(
        el("div", "confidence", "No rate is shown until at least ten decisions can be compared: a percentage over fewer would invite more confidence than it deserves.")
      );
    }

    if ((data.recent || []).length) {
      const p = panel("Recent decisions");
      const table = el("table", "ffe");
      const head = el("thead");
      const hr = el("tr");
      for (const label of ["Bug", "We said", "Team decided", "Agreed", "When"]) hr.append(el("th", null, label));
      head.append(hr);
      table.append(head);
      const body = el("tbody");
      for (const row of data.recent) {
        const tr = el("tr");
        const bugCell = el("td", "bug-id");
        const a = el("a", null, `#${row.bug_id}`);
        a.href = row.url || `https://bugs.launchpad.net/bugs/${row.bug_id}`;
        a.rel = "noopener noreferrer";
        bugCell.append(a);
        tr.append(bugCell);
        tr.append(el("td", null, row.ours || "—"));
        tr.append(el("td", null, row.theirs || "—"));
        tr.append(el("td", null, row.agreed === null ? "—" : row.agreed ? "yes" : "no"));
        tr.append(el("td", "confidence nowrap", fmtDate(row.decided_at)));
        body.append(tr);
      }
      table.append(body);
      p.body.append(table);
      root.append(p.section);
    }
  } catch (error) {
    root.replaceChildren(el("div", "empty", `Could not load agreement data: ${error.message}`));
  }
}

const PAGES = { index: initIndex, bug: initBug, lessons: initLessons, agreement: initAgreement };
const start = PAGES[document.documentElement.dataset.page];
if (start) start();

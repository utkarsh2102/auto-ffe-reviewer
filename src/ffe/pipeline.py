"""The run: discover, gather, assess, and only then spend anything.

Ordered so the expensive things happen last and least often. Discovery is one
search. The cheap gate rejects most bugs without fetching anything at all.
Evidence gathering is bounded and cached. The model is consulted only when the
review key has moved, and even then only within a budget.

The shape that matters is that each stage can decline to proceed and the run
still produces a useful record. A bug whose evidence is half-gathered still
gets a deterministic assessment; a bug whose model call fails still gets one.
Nothing here can turn a bad hour upstream into an empty dashboard.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from ffe.config import Settings
from ffe.errors import ContractError
from ffe.evidence import human as human_module
from ffe.evidence.builder import build as build_evidence
from ffe.evidence.builder import with_fingerprint
from ffe.evidence.fingerprint import fingerprint as compute_fingerprint
from ffe.evidence.fingerprint import review_key as compute_review_key
from ffe.learning.detect import find_disagreements, learnable
from ffe.learning.extract import extract as extract_lesson
from ffe.learning.select import select as select_lessons
from ffe.llm.base import HarnessError, LLMHarness, LLMRequest
from ffe.llm.contract import repair_prompt, validate
from ffe.llm.prompt import PolicyBundle, render_precedents, render_request
from ffe.models import (
    SCHEMA_VERSION,
    Assessment,
    BugFacts,
    EvidenceBundle,
    HarnessProvenance,
    ReviewProvenance,
    ReviewRecord,
    ReviewStatus,
    RiskAssessment,
)
from ffe.risk.gates import evaluate as apply_gates
from ffe.risk.score import assess as score_risk
from ffe.risk.signals import derive as derive_signals
from ffe.sources.base import SourceContext
from ffe.sources.launchpad import BugRef, LaunchpadClient
from ffe.sources.release_calendar import development_series, load_series
from ffe.sources.seeds import FlavourConfig, load_flavours
from ffe.store.repo import BugState, State, Store, touch_seen
from ffe.util.clock import now, utc_iso
from ffe.util.hashing import digest

TOOL_VERSION = "0.1.0"


@dataclass
class RunSummary:
    """What one run did, written to the state tree for later inspection."""

    run_id: str
    started_at: str
    finished_at: str = ""
    queue_size: int = 0
    process_gaps: tuple[int, ...] = ()
    reviewed: list[int] = field(default_factory=list)
    skipped_unchanged: list[int] = field(default_factory=list)
    archived: list[int] = field(default_factory=list)
    deferred_budget: list[int] = field(default_factory=list)
    lessons_learned: list[str] = field(default_factory=list)
    llm_calls: int = 0
    errors: list[str] = field(default_factory=list)
    unavailable_sources: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)


@dataclass
class Pipeline:
    """Everything one run needs, assembled once."""

    settings: Settings
    ctx: SourceContext
    store: Store
    launchpad: LaunchpadClient
    harness: LLMHarness | None
    policy: PolicyBundle
    schema: dict[str, Any]
    flavours: FlavourConfig = field(default_factory=load_flavours)
    run_id: str = ""

    def __post_init__(self) -> None:
        if not self.run_id:
            self.run_id = f"run-{utc_iso().replace(':', '').replace('-', '')}"

    # -- the whole run ------------------------------------------------------ #

    def run(self, *, force: bool = False, only: tuple[int, ...] = ()) -> RunSummary:
        summary = RunSummary(run_id=self.run_id, started_at=utc_iso())
        state = self.store.load_state()

        queue_fact = self.launchpad.review_queue()
        if not queue_fact.ok or queue_fact.value is None:
            # Without the queue we do not know what to review, and guessing
            # from the sweep would review things nobody asked us to.
            summary.errors.append(f"could not read the review queue: {queue_fact.note}")
            summary.finished_at = utc_iso()
            self.store.write_run(self.run_id, summary.as_dict())
            return summary

        queue = {ref.id: ref for ref in queue_fact.value}
        if only:
            queue = {k: v for k, v in queue.items() if k in only}
        summary.queue_size = len(queue)

        if self.settings.discovery.secondary_sweep and not only:
            summary.process_gaps = self._process_gaps(queue)

        for bug_id, ref in queue.items():
            try:
                self._handle_queued(bug_id, ref, state, summary, force=force)
            except Exception as exc:  # one bug must not end the run
                summary.errors.append(f"bug {bug_id}: {exc}")

        # Bugs we were tracking that are no longer subscribed: the team has
        # been through them, which is where our ground truth comes from.
        departed = [
            bug_id
            for bug_id, tracked in state.bugs.items()
            if bug_id not in queue and tracked.in_queue and not self.store.is_archived(bug_id)
        ]
        for bug_id in departed if not only else []:
            try:
                self._handle_departed(bug_id, state, summary)
            except Exception as exc:
                summary.errors.append(f"bug {bug_id} (departed): {exc}")

        if not only:
            try:
                summary.lessons_learned = [str(lesson["lesson_id"]) for lesson in self.learn()]
            except Exception as exc:  # learning must never break a review run
                summary.errors.append(f"learning: {exc}")

        summary.unavailable_sources = sorted({u.source_id for u in self.ctx.unavailable})
        summary.finished_at = utc_iso()
        self.store.save_state(state)
        self.store.write_run(self.run_id, summary.as_dict())
        return summary

    # -- discovery ---------------------------------------------------------- #

    def _process_gaps(self, queue: dict[int, BugRef]) -> tuple[int, ...]:
        """Bugs that look like FFes but nobody is subscribed to.

        Reported, never acted on. A request in no one's queue will not be
        reviewed, and the team would rather hear about it than find out at
        release time.
        """
        devel = development_series()
        sweep = self.launchpad.ffe_sweep(created_since=devel.created.isoformat() if devel else None)
        if not sweep.ok or sweep.value is None:
            return ()
        return tuple(sorted(ref.id for ref in sweep.value if ref.id not in queue))

    # -- one queued bug ----------------------------------------------------- #

    def _handle_queued(
        self,
        bug_id: int,
        ref: BugRef,
        state: State,
        summary: RunSummary,
        *,
        force: bool,
    ) -> None:
        tracked = state.get(bug_id)

        bug_fact = self.launchpad.fetch_bug(bug_id, discovered_via=ref.discovered_via)
        if not bug_fact.ok or bug_fact.value is None:
            summary.errors.append(f"bug {bug_id}: {bug_fact.note}")
            return
        bug = bug_fact.value

        tracked = touch_seen(
            tracked,
            date_last_updated=bug.date_last_updated,
            message_count=bug.message_count,
            date_last_message=bug.date_last_message,
        )
        tracked = replace(
            tracked, in_queue=True, status=ref.status, discovered_via=ref.discovered_via.value
        )

        record, used_llm = self._review(bug, tracked, summary, force=force)
        if record is not None:
            self.store.write_record(record)
            tracked = replace(
                tracked,
                fingerprint=record.evidence.fingerprint,
                review_key=record.provenance.review_key,
                last_evidence_at=utc_iso(),
                decision=record.assessment.decision.value
                if record.assessment
                else tracked.decision,
                confidence=record.assessment.confidence.value
                if record.assessment
                else tracked.confidence,
            )
            if used_llm:
                today = now().date().isoformat()
                tracked = replace(
                    tracked,
                    last_review_at=utc_iso(),
                    llm_runs_today=tracked.llm_budget_used(today) + 1,
                    llm_runs_date=today,
                )
            summary.reviewed.append(bug_id)
        else:
            summary.skipped_unchanged.append(bug_id)

        state.put(tracked)

    def _review(
        self, bug: BugFacts, tracked: BugState, summary: RunSummary, *, force: bool
    ) -> tuple[ReviewRecord | None, bool]:
        """Gather evidence, assess, and consult the model only if warranted."""
        bundle, risk = self.assess(bug)
        fingerprint = compute_fingerprint(bundle)
        bundle = with_fingerprint(bundle, fingerprint)

        harness_info = self.harness.describe() if self.harness else None
        key = compute_review_key(
            evidence_fingerprint=fingerprint,
            policy_hash=self.policy.policy_hash,
            lessons_hash=self.store.lessons_hash(),
            risk_algorithm_version=risk.algorithm_version,
            harness_id=harness_info.id if harness_info else "none",
            model=harness_info.model if harness_info else "",
        )

        if key == tracked.review_key and not force:
            # Nothing that could change the answer has changed. This is the
            # common case, and declining to act on it is most of why the
            # system is affordable to run every half hour.
            return None, False

        if self.harness is None:
            return self._record(bundle, risk, key, ReviewStatus.EVIDENCE_ONLY, None, None), False

        blocked = self._budget_reason(tracked)
        if blocked and not force:
            summary.deferred_budget.append(bug.id)
            return self._record(
                bundle, risk, key, ReviewStatus.DEFERRED_BUDGET, None, None, note=blocked
            ), False

        if summary.llm_calls >= self.settings.budget.max_llm_runs_per_run and not force:
            summary.deferred_budget.append(bug.id)
            return (
                self._record(
                    bundle,
                    risk,
                    key,
                    ReviewStatus.DEFERRED_BUDGET,
                    None,
                    None,
                    note="this run reached its review budget",
                ),
                False,
            )

        assessment, status, harness_provenance = self._consult(bundle, risk)
        summary.llm_calls += 1
        return self._record(bundle, risk, key, status, assessment, harness_provenance), True

    def assess(self, bug: BugFacts) -> tuple[EvidenceBundle, RiskAssessment]:
        """Everything deterministic: evidence, signals, score, gates."""
        series = [s.series for s in load_series()]
        devel = development_series()
        result = build_evidence(
            self.ctx,
            bug,
            launchpad=self.launchpad,
            flavours=self.flavours,
            known_series=tuple(series),
            default_series=devel.series if devel else None,
        )
        signals = derive_signals(result.bundle)
        return result.bundle, apply_gates(signals, score_risk(signals))

    # -- the model ---------------------------------------------------------- #

    def _consult(
        self, bundle: EvidenceBundle, risk: RiskAssessment
    ) -> tuple[Assessment | None, ReviewStatus, HarnessProvenance | None]:
        """Ask for a judgement, validate it, and retry a bounded number of times."""
        assert self.harness is not None
        # Only precedents that fit this request, capped. Forty loosely-related
        # ones are worse than three apt ones, because the apt ones stop
        # standing out.
        lessons = select_lessons(
            self.store.lessons(),
            derive_signals(bundle),
            max_lessons=self.settings.learning.max_active_lessons,
        )
        known = frozenset(str(lesson.get("lesson_id", "")) for lesson in lessons)

        rendered = render_request(
            bundle,
            policy=self.policy,
            schema=self.schema,
            precedents=render_precedents(
                lessons, max_chars=self.settings.learning.max_prompt_chars
            ),
            max_text_chars=self.settings.sanitize.max_description_chars,
        )

        request = LLMRequest(
            system=rendered.system,
            user=rendered.user,
            response_schema=self.schema,
            max_output_tokens=self.settings.llm.max_output_tokens,
            temperature=self.settings.llm.temperature,
            timeout_s=self.settings.timeouts.llm,
            request_id=f"{self.run_id}-{bundle.bug.id}",
        )

        info = self.harness.describe()
        attempts = 0
        latency = 0
        usage: dict[str, int] = {}
        violations: tuple[str, ...] = ()

        for attempt in range(self.settings.llm.max_repair_attempts + 1):
            attempts = attempt + 1
            try:
                response = self.harness.complete(request)
            except HarnessError:
                return (
                    None,
                    ReviewStatus.LLM_UNAVAILABLE,
                    HarnessProvenance(id=info.id, model=info.model, attempts=attempts),
                )

            latency += response.latency_ms
            usage = response.usage or usage

            try:
                result = validate(
                    response.text,
                    schema=self.schema,
                    evidence=bundle,
                    risk=risk,
                    known_precedents=known,
                )
            except ContractError as exc:
                violations = (str(exc),)
                result = None  # type: ignore[assignment]

            if result is not None and result.ok:
                return (
                    result.assessment,
                    ReviewStatus.REVIEWED,
                    HarnessProvenance(
                        id=info.id,
                        model=response.model,
                        attempts=attempts,
                        latency_ms=latency,
                        prompt_hash=rendered.prompt_hash,
                        usage=usage,
                    ),
                )

            violations = result.violations if result is not None else violations
            if attempt == self.settings.llm.max_repair_attempts:
                break

            # Retry with the errors alone. The evidence is already in the
            # system prompt, and resending the bug text would expose it twice.
            request = replace(
                request,
                user=repair_prompt(violations, response.text),
                temperature=max(request.temperature, 0.2),
            )

        return (
            None,
            ReviewStatus.LLM_OUTPUT_INVALID,
            HarnessProvenance(
                id=info.id, model=info.model, attempts=attempts, latency_ms=latency, usage=usage
            ),
        )

    # -- learning ----------------------------------------------------------- #

    def learn(self, *, limit: int = 5) -> list[dict[str, Any]]:
        """Extract precedents from decisions that differed from our advice.

        Only disagreements, and only where a Release Team member explained
        themselves. Agreement teaches nothing beyond "keep doing that", and a
        precedent invented from a comment that explained nothing would be
        applied to future requests as though it meant something.
        """
        if not self.settings.learning.enabled or self.harness is None:
            return []

        already = frozenset(
            int(lesson.get("source_bug", 0)) for lesson in self.store.lessons(active_only=False)
        )
        candidates = learnable(find_disagreements(self.store, already_learned=already))

        learned: list[dict[str, Any]] = []
        for disagreement in candidates[:limit]:
            lesson = extract_lesson(
                self.harness, disagreement, timeout_s=self.settings.timeouts.llm
            )
            if lesson is None:
                continue
            self.store.write_lesson(lesson)
            learned.append(lesson)
        return learned

    # -- departure ---------------------------------------------------------- #

    def _handle_departed(self, bug_id: int, state: State, summary: RunSummary) -> None:
        """A bug has left the queue: record what the team decided, and stand down."""
        tracked = state.get(bug_id)
        bug_fact = self.launchpad.fetch_bug(bug_id)
        if not bug_fact.ok or bug_fact.value is None:
            summary.errors.append(f"bug {bug_id}: could not confirm departure ({bug_fact.note})")
            return

        outcome = human_module.assess(
            bug_fact.value, in_queue=False, left_queue_at=tracked.last_seen_at or None
        ).outcome

        previous = self.store.latest_record(bug_id) or {}
        record = ReviewRecord(
            schema_version=SCHEMA_VERSION,
            record_id=f"{bug_id}/decided",
            status=ReviewStatus.SUPERSEDED_BY_HUMAN,
            evidence=replace(
                build_evidence(
                    self.ctx,
                    bug_fact.value,
                    launchpad=self.launchpad,
                    flavours=self.flavours,
                ).bundle,
                fingerprint=tracked.fingerprint,
            ),
            risk=RiskAssessment(),
            assessment=None,
            human=outcome,
            provenance=ReviewProvenance(
                tool_version=TOOL_VERSION,
                policy_version=self.policy.version,
                policy_hash=self.policy.policy_hash,
                lessons_hash=self.store.lessons_hash(),
                risk_algorithm_version=RiskAssessment().algorithm_version,
                review_key=tracked.review_key,
                generated_at=utc_iso(),
                run_id=self.run_id,
            ),
        )

        # Keep what we last recommended alongside the decision: that pairing is
        # the ground truth the learning loop and the agreement metric need.
        self.store.archive_record(
            record,
            our_last_assessment=previous.get("assessment"),
            our_last_risk=previous.get("risk"),
        )

        state.put(replace(tracked, in_queue=False, status=outcome.final_status or tracked.status))
        summary.archived.append(bug_id)

    # -- helpers ------------------------------------------------------------ #

    def _budget_reason(self, tracked: BugState) -> str | None:
        """Why this bug should not be reviewed again right now, if so."""
        budget = self.settings.budget
        today = now().date().isoformat()

        if tracked.llm_budget_used(today) >= budget.max_llm_runs_per_bug_per_day:
            return f"already reviewed {budget.max_llm_runs_per_bug_per_day} times today"
        if tracked.seconds_since_review() < budget.min_llm_interval_per_bug_seconds:
            return f"reviewed less than {budget.min_llm_interval_per_bug_seconds // 60} minutes ago"
        return None

    def _record(
        self,
        bundle: EvidenceBundle,
        risk: RiskAssessment,
        review_key: str,
        status: ReviewStatus,
        assessment: Assessment | None,
        harness: HarnessProvenance | None,
        *,
        note: str | None = None,
    ) -> ReviewRecord:
        return ReviewRecord(
            schema_version=SCHEMA_VERSION,
            record_id=f"{bundle.bug.id}/{bundle.fingerprint.split(':')[-1][:16]}",
            status=status,
            evidence=bundle,
            risk=risk,
            assessment=assessment,
            provenance=ReviewProvenance(
                tool_version=TOOL_VERSION,
                policy_version=self.policy.version,
                policy_hash=self.policy.policy_hash,
                lessons_hash=self.store.lessons_hash(),
                risk_algorithm_version=risk.algorithm_version,
                review_key=review_key,
                generated_at=utc_iso(),
                run_id=self.run_id,
                harness=harness,
            ),
        )


def signature(settings: Settings) -> str:
    """Identity of the current configuration, for run logs."""
    return digest({"harness": settings.llm.harness, "model": settings.llm.model})


__all__ = ["TOOL_VERSION", "Pipeline", "RunSummary", "signature"]

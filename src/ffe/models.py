"""Core data shapes for FFe review records.

Two rules govern everything in this module:

1. **Every fact carries its provenance.** There are no bare values in an evidence
   bundle -- a value always arrives wrapped in a `Fact`, which knows where it came
   from, when it was checked, and whether it is fresh, cached, stale or missing.
   This is what lets the dashboard show "seeded: no" with the command and raw
   output that established it.

2. **Evidence and judgement are separate.** `EvidenceBundle` and `RiskAssessment`
   are produced deterministically and are fully testable without a model.
   `Assessment` is the only thing an LLM produces, and it is nullable -- a record
   with no assessment is still a useful record.
"""

from __future__ import annotations

import dataclasses
import enum
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

T = TypeVar("T")

SCHEMA_VERSION = "1.0.0"
RISK_ALGORITHM_VERSION = "risk/1.2.0"


# --------------------------------------------------------------------------- #
# Enumerations
#
# All are StrEnum so they serialise to readable JSON and compare equal to the
# plain strings that arrive from schema-validated LLM output.
# --------------------------------------------------------------------------- #


class FactStatus(enum.StrEnum):
    OK = "OK"
    UNAVAILABLE = "UNAVAILABLE"  # source could not be reached, no prior value
    NOT_APPLICABLE = "NOT_APPLICABLE"  # question does not apply (e.g. rdeps of a new package)
    ERROR = "ERROR"  # source responded, but we could not use it


class SourceMethod(enum.StrEnum):
    HTTP_GET = "http_get"
    SUBPROCESS = "subprocess"
    LP_API = "lp_api"
    COMPUTED = "computed"
    STATIC = "static"


class CacheState(enum.StrEnum):
    FRESH = "fresh"  # fetched this run
    CACHED = "cached"  # served from cache within TTL
    STALE = "stale"  # upstream failed, last good value served instead
    NONE = "none"  # not a cacheable source


class Decision(enum.StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    NEEDS_INFORMATION = "NEEDS_INFORMATION"


class Confidence(enum.StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class RiskBand(enum.StrEnum):
    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"
    SEVERE = "SEVERE"


class ReviewStatus(enum.StrEnum):
    REVIEWED = "REVIEWED"
    EVIDENCE_ONLY = "EVIDENCE_ONLY"  # ran with --no-llm
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"  # harness failed or timed out
    LLM_OUTPUT_INVALID = "LLM_OUTPUT_INVALID"  # output failed validation after repair
    SUPERSEDED_BY_HUMAN = "SUPERSEDED_BY_HUMAN"  # a human decided; we stood down
    DEFERRED_BUDGET = "DEFERRED_BUDGET"  # review was due but budget blocked it


class EvidenceState(enum.StrEnum):
    """How well a piece of claimed testing evidence actually stands up."""

    FOUND_VERIFIED = "FOUND_VERIFIED"  # link present and confirmed via Launchpad
    FOUND_UNVERIFIED = "FOUND_UNVERIFIED"  # link present, could not confirm
    CLAIMED_ONLY = "CLAIMED_ONLY"  # asserted in prose with nothing to check
    ABSENT = "ABSENT"  # not mentioned at all


class ReleaseType(enum.StrEnum):
    LTS = "LTS"
    INTERIM = "INTERIM"


class FlavourImpact(enum.StrEnum):
    CORE = "CORE"  # seeded in ubuntu or ubuntu-server
    MULTI_FLAVOUR = "MULTI_FLAVOUR"  # seeded in two or more flavours
    SINGLE_FLAVOUR = "SINGLE_FLAVOUR"  # exactly one flavour
    UNSEEDED = "UNSEEDED"  # on no image


class GateEffect(enum.StrEnum):
    FLOOR_NEEDS_INFORMATION = "floor_needs_information"
    FLOOR_REJECT = "floor_reject"
    SKIP_LLM = "skip_llm"
    CAP_CONFIDENCE = "cap_confidence"
    FLAG = "flag"


class HumanDecision(enum.StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    NEEDS_INFORMATION = "NEEDS_INFORMATION"


class FfeKind(enum.StrEnum):
    NEW_UPSTREAM = "new-upstream"  # version bump to a new upstream release
    SYNC = "sync"  # sync from Debian
    MERGE = "merge"  # merge from Debian
    NEW_PACKAGE = "new-package"  # not yet in the archive
    FEATURE_CHANGE = "feature-change"  # behavioural change to an existing package
    TRANSITION = "transition"  # archive-wide transition
    SEED_CHANGE = "seed-change"  # changes what lands on an image
    UNKNOWN = "unknown"


class DiscoverySignal(enum.StrEnum):
    """Which mechanism surfaced a bug. Recorded so recall drift is visible."""

    RELEASE_TEAM_QUEUE = "release-team-queue"  # subscribed to ~ubuntu-release
    TITLE_SWEEP = "title-sweep"  # looks like an FFe, not subscribed
    WATCHLIST = "watchlist"  # manual escape hatch


# Confidence is ordered so it can be clamped against a deterministic ceiling.
_CONFIDENCE_ORDER: tuple[Confidence, ...] = (Confidence.LOW, Confidence.MEDIUM, Confidence.HIGH)
_RISK_ORDER: tuple[RiskBand, ...] = (
    RiskBand.LOW,
    RiskBand.MODERATE,
    RiskBand.HIGH,
    RiskBand.SEVERE,
)


def clamp_confidence(value: Confidence, ceiling: Confidence) -> Confidence:
    """Return `value`, lowered to `ceiling` if it claims more certainty than we have."""
    return min(value, ceiling, key=_CONFIDENCE_ORDER.index)


def risk_at_least(band: RiskBand, floor: RiskBand) -> RiskBand:
    """Return whichever of `band` and `floor` is the more severe."""
    return max(band, floor, key=_RISK_ORDER.index)


# --------------------------------------------------------------------------- #
# Bucketing
#
# Buckets, not raw numbers, are what enter the change-detection fingerprint.
# A package going from 148 to 149 reverse dependencies is not a reason to pay
# for another LLM review; crossing from "a handful" to "hundreds" is.
# --------------------------------------------------------------------------- #

_RDEP_BUCKETS: tuple[tuple[int, str], ...] = (
    (0, "0"),
    (1, "1-5"),
    (6, "6-20"),
    (21, "21-100"),
    (101, "101-500"),
    (501, "500+"),
)

# Days remaining before release. The boundaries are the points at which the
# Release Team's tolerance actually changes, not evenly spaced intervals.
_DAYS_BUCKETS: tuple[tuple[int, str], ...] = (
    (57, ">56"),
    (29, "56-29"),
    (15, "28-15"),
    (8, "14-8"),
    (4, "7-4"),
    (0, "3-0"),
)


def rdep_bucket(count: int) -> str:
    """Bucket a reverse-dependency count into a stable band name."""
    label = _RDEP_BUCKETS[0][1]
    for threshold, name in _RDEP_BUCKETS:
        if count >= threshold:
            label = name
    return label


def days_to_release_bucket(days: int) -> str:
    """Bucket days-until-release. Negative means the release has already happened."""
    if days < 0:
        return "post-release"
    for threshold, name in _DAYS_BUCKETS:
        if days >= threshold:
            return name
    return "3-0"


# --------------------------------------------------------------------------- #
# Provenance and facts
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where a value came from, precisely enough for a reviewer to re-check it."""

    source_id: str  # e.g. "seeds.ubuntuwire"
    method: SourceMethod
    locator: str  # URL, or the exact argv we ran
    checked_at: str  # ISO-8601 UTC
    raw_ref: str | None = None  # "sha256:..." -> the retained raw response
    cache: CacheState = CacheState.NONE
    duration_ms: int = 0
    raw_truncated: bool = False


@dataclass(frozen=True, slots=True)
class Fact(Generic[T]):
    """A single value plus the story of how we came to believe it.

    `status` is deliberately not a bool: "we checked and it is false" and "we
    could not check" must never collapse into the same thing, because the second
    one lowers our confidence ceiling and the first one does not.
    """

    value: T | None
    status: FactStatus
    provenance: Provenance
    note: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is FactStatus.OK and self.value is not None

    def unwrap(self, default: T) -> T:
        """Return the value, or `default` when the fact is not usable."""
        return self.value if self.ok else default  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class Unavailable:
    """A source we could not consult, and what that costs us."""

    source_id: str
    reason: str
    impact: str  # plain-English note on what this blinds us to


# --------------------------------------------------------------------------- #
# Release calendar
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Milestone:
    name: str  # "Feature Freeze", "Beta", ...
    date: str  # ISO-8601 date


@dataclass(frozen=True, slots=True)
class ReleaseContext:
    """Where the target release sits in its cycle.

    Feature Freeze runs from `feature_freeze` until `release_date`; there is no
    separate "are we frozen" lookup. Position inside that one window, plus which
    named milestones have passed, is the whole timing signal.
    """

    series: str  # "stonking"
    version: str  # "26.10"
    release_type: ReleaseType
    feature_freeze: str | None
    release_date: str | None
    milestones: tuple[Milestone, ...] = ()
    phase: str = "UNKNOWN"  # most recent milestone passed, upper-snake-cased
    days_to_release: int | None = None
    days_since_feature_freeze: int | None = None
    days_bucket: str = "unknown"
    freeze_window_elapsed_pct: int | None = None
    derivation: str = ""  # "schedule-page" | "distro-info+8w" | "both-agree"


# --------------------------------------------------------------------------- #
# Package facts
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SeedInfo:
    """Which images a package's binaries land on.

    Derived from the same index `seeded-in-ubuntu` reads, so the answer matches
    what a Release Team member would get from the tool by hand.
    """

    flavours: tuple[str, ...]
    images: tuple[tuple[str, str], ...]  # (flavour, image type)
    is_core: bool  # on a core Ubuntu image
    binaries_seeded: tuple[str, ...]  # which binaries were actually found
    binaries_checked: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RdepInfo:
    """Reverse-dependency blast radius for one package."""

    total: int
    bucket: str
    kind: str  # "binary" | "build"
    arch: str
    sample: tuple[str, ...] = ()  # capped; the full list lives in the raw blob
    seeded_rdeps: tuple[str, ...] = ()  # rdeps that are themselves on an image


@dataclass(frozen=True, slots=True)
class ArchiveInfo:
    """A source package's current standing in the archive.

    Component matters in its own right: main and restricted are
    Canonical-supported, universe is not, which bears on what a regression
    costs. The binary list is what makes the seed lookup possible at all,
    since that index is keyed by binary name.
    """

    source: str
    in_archive: bool
    version: str = ""
    component: str = ""  # main / universe / restricted / multiverse
    pocket: str = ""  # Release / Proposed / Updates
    binaries: tuple[str, ...] = ()

    @property
    def in_main(self) -> bool:
        return self.component in {"main", "restricted"}


@dataclass(frozen=True, slots=True)
class PackageFacts:
    name: str
    archive: Fact[ArchiveInfo]
    seeds: Fact[SeedInfo]
    rdepends: Fact[RdepInfo]
    build_rdepends: Fact[RdepInfo]


# --------------------------------------------------------------------------- #
# Testing evidence
#
# An FFe asks to land something that is *not in the archive yet*, so archive
# autopkgtest state describes the wrong version. What counts is what the
# developer put in the bug -- and whether it survives corroboration.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TestingItem:
    kind: str  # "ppa" | "build_log" | "autopkgtest" | "test_output" | "claim"
    state: EvidenceState
    detail: str  # one line a human can read
    locator: str | None = None
    source: str = ""  # "description" or "comment #N"


# States meaning a human could go and look at the thing. Prose never
# qualifies, however confidently it is written.
CHECKABLE_STATES = (EvidenceState.FOUND_VERIFIED, EvidenceState.FOUND_UNVERIFIED)


def is_corroborated(*states: EvidenceState) -> bool:
    """Whether any of these is something a reviewer could actually go and check."""
    return any(state in CHECKABLE_STATES for state in states)


@dataclass(frozen=True, slots=True)
class TestingEvidence:
    items: tuple[TestingItem, ...] = ()
    ppa: EvidenceState = EvidenceState.ABSENT
    build: EvidenceState = EvidenceState.ABSENT
    autopkgtest: EvidenceState = EvidenceState.ABSENT
    test_output: EvidenceState = EvidenceState.ABSENT
    unverified_claims: tuple[str, ...] = ()

    # A stored field rather than a derived property. As a property this was
    # invisible to to_jsonable, which serialises fields only, so it vanished
    # from every record and the dashboard read the missing key as "no testing
    # evidence" for every bug regardless of the truth.
    corroborated: bool = False

    def __post_init__(self) -> None:
        # Derived on construction rather than on demand, so there is no way to
        # build one of these with the flag out of step with the states.
        object.__setattr__(
            self,
            "corroborated",
            is_corroborated(self.ppa, self.build, self.autopkgtest, self.test_output),
        )


# --------------------------------------------------------------------------- #
# Flavours
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FlavourAck:
    """A flavour signing off on a change that touches its image."""

    flavour: str
    author: str
    message_url: str | None = None
    author_in_flavour_team: bool | None = None


@dataclass(frozen=True, slots=True)
class FlavourFacts:
    impact: FlavourImpact = FlavourImpact.UNSEEDED
    affected_flavours: tuple[str, ...] = ()
    requesting_flavour: str | None = None
    requester_is_flavour_lead: bool | None = None
    acks: tuple[FlavourAck, ...] = ()
    ack_required_from: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# Bug facts (untrusted input)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class BugTask:
    target: str
    status: str
    importance: str
    assignee: str | None = None


@dataclass(frozen=True, slots=True)
class BugComment:
    index: int
    author: str
    date_created: str
    content: str
    content_sha: str
    url: str | None = None
    author_is_release_team: bool = False


@dataclass(frozen=True, slots=True)
class BugFacts:
    """Everything we read off the bug. All of it is untrusted input."""

    id: int
    url: str
    title: str
    description: str
    description_sha: str
    reporter: str
    tags: tuple[str, ...] = ()
    date_created: str = ""
    date_last_updated: str = ""
    date_last_message: str | None = None
    message_count: int = 0
    tasks: tuple[BugTask, ...] = ()
    comments: tuple[BugComment, ...] = ()
    subscribers: tuple[str, ...] = ()
    release_team_subscribed: bool = False
    discovered_via: DiscoverySignal = DiscoverySignal.RELEASE_TEAM_QUEUE


@dataclass(frozen=True, slots=True)
class SubjectFacts:
    """What the FFe is actually asking for, as parsed from the bug."""

    packages: tuple[str, ...] = ()
    target_series: str | None = None
    ffe_kind: FfeKind = FfeKind.UNKNOWN
    derivation: str = ""


@dataclass(frozen=True, slots=True)
class InjectionSignal:
    """A deterministic pre-scan hit, shown verbatim next to the model's verdict."""

    pattern_id: str
    excerpt: str
    offset: int
    location: str  # "description" or "comment #N"


# --------------------------------------------------------------------------- #
# Evidence bundle
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    bug: BugFacts
    subject: SubjectFacts
    release_context: Fact[ReleaseContext]
    packages: tuple[PackageFacts, ...] = ()
    testing: TestingEvidence = field(default_factory=TestingEvidence)
    flavours: FlavourFacts = field(default_factory=FlavourFacts)
    injection_signals: tuple[InjectionSignal, ...] = ()
    unavailable: tuple[Unavailable, ...] = ()
    fingerprint: str = ""


# --------------------------------------------------------------------------- #
# Deterministic risk
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RiskComponent:
    """One contribution to the risk score, with the evidence that justifies it."""

    name: str
    points: int
    max_points: int
    rationale: str
    evidence_refs: tuple[str, ...] = ()  # JSON pointers into the bundle


@dataclass(frozen=True, slots=True)
class Gate:
    """A hard rule that constrains the outcome before any model is consulted."""

    id: str
    triggered: bool
    effect: GateEffect
    rationale: str
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    algorithm_version: str = RISK_ALGORITHM_VERSION
    score: int = 0
    band: RiskBand = RiskBand.LOW
    components: tuple[RiskComponent, ...] = ()
    gates: tuple[Gate, ...] = ()
    decision_floor: Decision | None = None
    confidence_ceiling: Confidence = Confidence.HIGH


# --------------------------------------------------------------------------- #
# LLM assessment
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ReasoningItem:
    point: str
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Assessment:
    """The only part of a record an LLM produces."""

    decision: Decision
    confidence: Confidence
    summary: str
    reasoning: tuple[ReasoningItem, ...] = ()
    missing_information: tuple[str, ...] = ()
    conditions: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    precedents_applied: tuple[str, ...] = ()
    disagreement_with_deterministic: str | None = None
    confidence_clamped_from: Confidence | None = None


# --------------------------------------------------------------------------- #
# Human outcome and the record itself
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class HumanOutcome:
    """What the Release Team actually decided, once they have.

    Detected from the bug leaving the ~ubuntu-release queue combined with its
    final status -- not from guessing at the wording of comments.
    """

    decided: bool = False
    decision: HumanDecision | None = None
    final_status: str | None = None
    detected_at: str | None = None
    left_queue_at: str | None = None


@dataclass(frozen=True, slots=True)
class HarnessProvenance:
    id: str
    model: str
    attempts: int = 1
    latency_ms: int = 0
    prompt_hash: str = ""
    raw_ref: str | None = None
    usage: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReviewProvenance:
    tool_version: str
    policy_version: str
    policy_hash: str
    lessons_hash: str
    risk_algorithm_version: str
    review_key: str
    generated_at: str
    run_id: str = ""
    harness: HarnessProvenance | None = None


@dataclass(frozen=True, slots=True)
class ReviewRecord:
    """One immutable review of one bug at one point in time."""

    schema_version: str
    record_id: str
    status: ReviewStatus
    evidence: EvidenceBundle
    risk: RiskAssessment
    provenance: ReviewProvenance
    assessment: Assessment | None = None
    human: HumanOutcome = field(default_factory=HumanOutcome)


# --------------------------------------------------------------------------- #
# Learned precedents
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Lesson:
    """A precedent learned from a Release Team decision that differed from ours.

    Lessons describe observed team behaviour. They never outrank the rubric --
    see policy/rubric/95-precedents.md.
    """

    lesson_id: str
    created_at: str
    source_bug: int
    our_decision: Decision | None
    human_decision: HumanDecision
    situation: str
    lesson: str
    rationale_quote: str
    rationale_author: str
    applies_when: dict[str, Any] = field(default_factory=dict)
    status: str = "active"  # "active" | "retired"
    retired_at: str | None = None
    retired_reason: str | None = None


# --------------------------------------------------------------------------- #
# Serialisation
# --------------------------------------------------------------------------- #


def to_jsonable(obj: Any) -> Any:
    """Convert dataclasses, enums and tuples into JSON-safe structures.

    Used for both persistence and hashing, so it must be total and stable: the
    same object graph always produces the same output.
    """
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        out: dict[str, Any] = {}
        for f in dataclasses.fields(obj):
            out[f.name] = to_jsonable(getattr(obj, f.name))
        return out
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [to_jsonable(v) for v in obj]
    return obj

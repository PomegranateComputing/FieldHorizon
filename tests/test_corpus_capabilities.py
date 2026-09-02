"""
The source capability graph.

Phase I assumed one source discovers, describes, proves rights for, and
hosts every document. DOAB describes books OAPEN hosts; Europeana brokers
records institutions host; Wikisource's bulk transport is a dump, not the
API its discovery uses. In each case Phase I had to treat the source as
*broken* because it could not do all four things.

These tests assert the replacement: providers declare roles, plans record
who filled which, and a provider that does two things well is not an
error.
"""

from __future__ import annotations

from fieldhorizon.corpus.capabilities import (
    NON_ERROR_PLAN_STATES,
    AcquisitionPlan,
    Capability,
    PlanStatus,
    ProviderRegistry,
    ProviderStatus,
    SourceCapabilities,
    build_plan,
)
from fieldhorizon.corpus.models import (
    BLOCKED_ACQUISITION_STATES,
    NON_ERROR_ACQUISITION_STATES,
    State,
    allowed_transitions,
    can_transition,
)

# Providers modelled on the real ones, with their real asymmetries.
DOAB = SourceCapabilities(
    provider_id="doab",
    capabilities=frozenset({Capability.DISCOVERY, Capability.METADATA,
                            Capability.RIGHTS_EVIDENCE, Capability.INCREMENTAL_UPDATES}),
    rights_trust=0.85,
    notes="Describes books it does not host.",
)
OAPEN = SourceCapabilities(
    provider_id="oapen",
    capabilities=frozenset({Capability.METADATA, Capability.CONTENT_HOSTING}),
    rights_trust=0.85,
    content_hosts=("library.oapen.org",),
)
GALLICA = SourceCapabilities(
    provider_id="gallica",
    capabilities=frozenset({Capability.DISCOVERY, Capability.METADATA, Capability.RIGHTS_EVIDENCE,
                            Capability.CONTENT_HOSTING, Capability.RENDERED_TEXT,
                            Capability.SUPPORTS_RESUME}),
    rights_trust=0.75,
    content_hosts=("gallica.bnf.fr",),
)
EUROPEANA = SourceCapabilities(
    provider_id="europeana",
    capabilities=frozenset({Capability.METADATA}),
    credentialled_capabilities=frozenset({Capability.DISCOVERY, Capability.RIGHTS_EVIDENCE}),
    credential_env="EUROPEANA_API_KEY",
    rights_trust=0.6,
    notes="A broker. Never hosts anything.",
)
SE_GITHUB = SourceCapabilities(
    provider_id="standard_ebooks_github",
    capabilities=frozenset({Capability.DISCOVERY, Capability.METADATA, Capability.RIGHTS_EVIDENCE,
                            Capability.CONTENT_HOSTING, Capability.BULK_SNAPSHOT,
                            Capability.INCREMENTAL_UPDATES, Capability.SUPPORTS_RESUME}),
    credential_env="GITHUB_TOKEN",
    rights_trust=0.95,
    content_hosts=("github.com", "raw.githubusercontent.com", "api.github.com"),
)


def registry(*providers, credentials: dict[str, bool] | None = None) -> ProviderRegistry:
    credentials = credentials or {}
    reg = ProviderRegistry()
    for provider in providers:
        reg.register(provider, credential_available=credentials.get(provider.provider_id, False))
    return reg


# ------------------------------------------------------------ capabilities


def test_a_provider_declares_only_what_it_actually_does():
    assert DOAB.has(Capability.DISCOVERY)
    assert DOAB.has(Capability.RIGHTS_EVIDENCE)
    # The whole point: DOAB does NOT host, and saying so is not a defect.
    assert not DOAB.has(Capability.CONTENT_HOSTING)


def test_credentialled_capabilities_appear_only_with_credentials():
    assert not EUROPEANA.has(Capability.DISCOVERY)
    assert EUROPEANA.has(Capability.DISCOVERY, credentials_available=True)
    assert EUROPEANA.has(Capability.METADATA)  # ungated


def test_a_source_needing_credentials_is_ready_not_broken():
    """
    `READY_WITH_CREDENTIALS` is not a failure. An operator who has not
    configured an API key should see an idle source, not an error they
    cannot act on from a log line.
    """
    reg = registry(EUROPEANA)
    assert reg.status_of("europeana", enabled=True) == ProviderStatus.READY_WITH_CREDENTIALS

    with_key = registry(EUROPEANA, credentials={"europeana": True})
    assert with_key.status_of("europeana", enabled=True) == ProviderStatus.READY


def test_provider_status_distinguishes_the_ways_of_being_unavailable():
    reg = registry(OAPEN)
    assert reg.status_of("oapen", enabled=False) == ProviderStatus.DISABLED
    assert reg.status_of("oapen", enabled=True, blocked="provider") == ProviderStatus.BLOCKED_BY_PROVIDER
    assert reg.status_of("oapen", enabled=True, blocked="policy") == ProviderStatus.BLOCKED_BY_POLICY
    assert reg.status_of("nobody", enabled=True) == ProviderStatus.UNCONFIGURED


def test_capabilities_never_carry_a_secret_only_its_name():
    """
    `credential_env` is a variable NAME. No secret can reach a log, a
    report, or the database through this object.
    """
    payload = SE_GITHUB.to_dict()
    assert payload["credential_env"] == "GITHUB_TOKEN"
    assert "token" not in str(payload).lower().replace("github_token", "")


def test_providers_with_a_capability_are_ranked_by_rights_trust():
    reg = registry(SE_GITHUB, GALLICA, OAPEN)
    hosts = reg.providers_with(Capability.CONTENT_HOSTING)
    assert hosts[0] == "standard_ebooks_github"  # 0.95
    assert set(hosts) == {"standard_ebooks_github", "gallica", "oapen"}


def test_credentialled_capabilities_are_excluded_from_the_registry_view_without_a_key():
    assert "europeana" not in registry(EUROPEANA).providers_with(Capability.DISCOVERY)
    assert "europeana" in registry(EUROPEANA, credentials={"europeana": True}).providers_with(
        Capability.DISCOVERY
    )


# ------------------------------------------------------------------ plans


def test_a_metadata_only_source_produces_a_complete_plan_not_a_failure():
    """
    DOAB's 103 rights-accepted records looked like 103 download failures
    in Phase I. They were a complete metadata acquisition with no hosting
    role, which is exactly what this state says.
    """
    plan = build_plan("doab:123", registry(DOAB), discovered_by="doab", rights_evidence_from="doab")

    assert plan.status == PlanStatus.METADATA_ONLY
    assert plan.is_error() is False
    assert plan.is_actionable() is False
    assert "no provider hosts its text" in plan.reason


def test_a_brokered_acquisition_names_both_institutions():
    """
    The provenance Phase I could not express: discovered by one
    institution, hosted by another.
    """
    reg = registry(EUROPEANA, GALLICA, credentials={"europeana": True})
    plan = build_plan(
        "europeana:/9200396/item",
        reg,
        discovered_by="europeana",
        metadata_from="europeana",
        rights_evidence_from="gallica",
        content_candidates=(("gallica", "https://gallica.bnf.fr/ark:/12148/bpt6k1234567.texteBrut"),),
        expected_format="html",
        normalization_pipeline="html",
    )

    assert plan.status == PlanStatus.READY_FOR_DOWNLOAD
    assert plan.discovered_by == "europeana"
    assert plan.rights_evidence_from == "gallica"
    assert plan.content_hosted_by == "gallica"
    assert plan.is_actionable()


def test_doab_discovery_plus_oapen_hosting_composes():
    reg = registry(DOAB, OAPEN)
    plan = build_plan(
        "doab:456",
        reg,
        discovered_by="doab",
        rights_evidence_from="doab",
        content_candidates=(("oapen", "https://library.oapen.org/bitstream/20.500.12657/1/book.pdf"),),
    )

    assert plan.status == PlanStatus.READY_FOR_DOWNLOAD
    assert plan.discovered_by == "doab"
    assert plan.content_hosted_by == "oapen"


def test_a_non_allowlisted_host_makes_the_plan_unverified_not_actionable():
    """
    An aggregator that can point the harvester at any host on the
    internet is an allowlist bypass, however open the licence.
    """
    reg = registry(EUROPEANA, GALLICA, credentials={"europeana": True})
    plan = build_plan(
        "europeana:/1111/off-allowlist",
        reg,
        discovered_by="europeana",
        content_candidates=(("gallica", "https://random-untrusted-host.example.net/file.txt"),),
    )

    assert plan.status == PlanStatus.PROVIDER_UNVERIFIED
    assert not plan.is_actionable()
    assert "non-allowlisted host" in plan.reason


def test_an_unregistered_provider_cannot_host_anything():
    plan = build_plan(
        "x:1",
        registry(DOAB),
        discovered_by="doab",
        content_candidates=(("mystery_provider", "https://example.org/x.pdf"),),
    )
    assert plan.status == PlanStatus.PROVIDER_UNVERIFIED
    assert "not a registered provider" in plan.reason


def test_a_credential_blocked_host_reports_auth_required():
    gated = SourceCapabilities(
        provider_id="gated",
        capabilities=frozenset({Capability.METADATA}),
        credentialled_capabilities=frozenset({Capability.CONTENT_HOSTING}),
        credential_env="GATED_TOKEN",
        content_hosts=("gated.example.org",),
    )
    plan = build_plan(
        "gated:1",
        registry(gated),
        discovered_by="gated",
        content_candidates=(("gated", "https://gated.example.org/x.epub"),),
    )

    assert plan.status == PlanStatus.AUTH_REQUIRED
    assert "$GATED_TOKEN" in plan.reason
    # Waiting on a credential is not an error.
    assert plan.is_error() is False


def test_the_same_host_becomes_actionable_once_the_credential_appears():
    gated = SourceCapabilities(
        provider_id="gated",
        capabilities=frozenset({Capability.METADATA}),
        credentialled_capabilities=frozenset({Capability.CONTENT_HOSTING}),
        credential_env="GATED_TOKEN",
        content_hosts=("gated.example.org",),
    )
    reg = registry(gated, credentials={"gated": True})
    plan = build_plan(
        "gated:1", reg, discovered_by="gated",
        content_candidates=(("gated", "https://gated.example.org/x.epub"),),
    )
    assert plan.status == PlanStatus.READY_FOR_DOWNLOAD


def test_extra_hosts_become_ordered_fallbacks():
    reg = registry(GALLICA, OAPEN, SE_GITHUB)
    plan = build_plan(
        "multi:1",
        reg,
        discovered_by="gallica",
        content_candidates=(
            ("gallica", "https://gallica.bnf.fr/ark:/1/x.texteBrut"),
            ("oapen", "https://library.oapen.org/bitstream/1/x.pdf"),
            ("standard_ebooks_github", "https://raw.githubusercontent.com/o/r/x.xhtml"),
        ),
    )

    assert plan.content_hosted_by == "gallica"
    assert [p for p, _ in plan.fallback_hosts] == ["oapen", "standard_ebooks_github"]


def test_an_unusable_primary_is_skipped_and_the_next_host_wins():
    reg = registry(DOAB, OAPEN)
    plan = build_plan(
        "fallback:1",
        reg,
        discovered_by="doab",
        content_candidates=(
            ("doab", "https://directory.doabooks.org/x.pdf"),          # does not host
            ("oapen", "https://library.oapen.org/bitstream/1/x.pdf"),  # does
        ),
    )
    assert plan.content_hosted_by == "oapen"
    assert plan.fallback_hosts == ()


def test_a_bulk_pending_plan_is_not_an_error():
    plan = build_plan(
        "wikisource:1", registry(DOAB), discovered_by="wikisource_fr", bulk_pending=True
    )
    assert plan.status == PlanStatus.BULK_SNAPSHOT_PENDING
    assert plan.is_error() is False


def test_an_image_only_document_reports_ocr_pending_not_failure():
    reg = registry(GALLICA)
    plan = build_plan(
        "gallica:scan",
        reg,
        discovered_by="gallica",
        content_candidates=(("gallica", "https://gallica.bnf.fr/ark:/1/f1.image"),),
        needs_ocr=True,
    )
    assert plan.status == PlanStatus.OCR_PENDING
    assert plan.is_error() is False


def test_the_non_error_states_are_exactly_the_ones_that_can_still_resolve():
    assert frozenset(
        {
            PlanStatus.METADATA_ONLY,
            PlanStatus.AUTH_REQUIRED,
            PlanStatus.BULK_SNAPSHOT_PENDING,
            PlanStatus.OCR_PENDING,
        }
    ) == NON_ERROR_PLAN_STATES
    for status in (PlanStatus.PROVIDER_UNVERIFIED, PlanStatus.CONTENT_UNAVAILABLE,
                   PlanStatus.ACCESS_BLOCKED):
        assert AcquisitionPlan("x", status=status).is_error()


def test_a_plan_serializes_for_the_database():
    reg = registry(GALLICA, OAPEN)
    plan = build_plan(
        "ser:1", reg, discovered_by="gallica",
        content_candidates=(
            ("gallica", "https://gallica.bnf.fr/ark:/1/x.texteBrut"),
            ("oapen", "https://library.oapen.org/bitstream/1/x.pdf"),
        ),
        expected_format="html", normalization_pipeline="html",
    )
    payload = plan.to_dict()

    assert payload["status"] == "READY_FOR_DOWNLOAD"
    assert payload["content_hosted_by"] == "gallica"
    assert payload["fallback_hosts"] == [["oapen", "https://library.oapen.org/bitstream/1/x.pdf"]]


# ------------------------------------------------------- the new states


def test_the_acquisition_states_exist_on_the_pipeline_state_machine():
    for name in ("METADATA_ONLY", "AUTH_REQUIRED", "PROVIDER_UNVERIFIED", "CONTENT_UNAVAILABLE",
                 "ACCESS_BLOCKED", "OCR_PENDING", "BULK_SNAPSHOT_PENDING", "READY_FOR_DOWNLOAD"):
        assert hasattr(State, name), name


def test_no_acquisition_state_is_terminal():
    """
    Every one describes a condition a later run can resolve -- a
    credential appears, a dump is fetched, an OCR worker is installed, a
    provider stops refusing. Making them terminal would recreate the
    Phase I problem of a source being permanently broken for a reason
    that was never permanent.
    """
    for state in (State.METADATA_ONLY, State.AUTH_REQUIRED, State.PROVIDER_UNVERIFIED,
                  State.CONTENT_UNAVAILABLE, State.ACCESS_BLOCKED, State.OCR_PENDING,
                  State.BULK_SNAPSHOT_PENDING, State.READY_FOR_DOWNLOAD):
        assert allowed_transitions(state), f"{state.value} is a dead end"


def test_rights_accepted_can_reach_every_planning_outcome():
    for state in (State.METADATA_ONLY, State.AUTH_REQUIRED, State.PROVIDER_UNVERIFIED,
                  State.BULK_SNAPSHOT_PENDING, State.READY_FOR_DOWNLOAD):
        assert can_transition(State.RIGHTS_ACCEPTED, state)


def test_a_metadata_only_document_can_later_become_downloadable():
    """A second provider, or a fetched dump, resolves it."""
    assert can_transition(State.METADATA_ONLY, State.READY_FOR_DOWNLOAD)
    assert can_transition(State.METADATA_ONLY, State.BULK_SNAPSHOT_PENDING)
    assert can_transition(State.AUTH_REQUIRED, State.READY_FOR_DOWNLOAD)


def test_the_non_error_and_blocked_state_sets_are_disjoint():
    assert not (NON_ERROR_ACQUISITION_STATES & BLOCKED_ACQUISITION_STATES)


def test_no_acquisition_state_is_indexable():
    """
    None of these documents has content, so none may reach retrieval.
    The Phase I invariant is unchanged.
    """
    from fieldhorizon.corpus.models import INDEXABLE_STATES

    for state in NON_ERROR_ACQUISITION_STATES | BLOCKED_ACQUISITION_STATES:
        assert state not in INDEXABLE_STATES


def test_access_blocked_never_leads_anywhere_that_bypasses_the_refusal():
    """
    A provider that refused us may be retried later, but the only routes
    out are re-queueing, recording it as metadata-only, or giving up.
    There is deliberately no path that treats a 403 as an invitation.
    """
    onward = allowed_transitions(State.ACCESS_BLOCKED)
    assert onward == frozenset(
        {State.QUEUED, State.METADATA_ONLY, State.FAILED_FINAL, State.WITHDRAWN}
    )


# ---------------------------------------------------------------- registry


def test_the_registry_reports_credential_presence_without_the_value(monkeypatch):
    monkeypatch.setenv("EUROPEANA_API_KEY", "super-secret-value")
    reg = ProviderRegistry()
    reg.register(EUROPEANA)

    assert reg.has_credentials("europeana") is True
    payload = reg.to_dict()
    assert payload["europeana"]["credentials_present"] is True
    assert "super-secret-value" not in str(payload)


def test_an_absent_credential_is_simply_false(monkeypatch):
    monkeypatch.delenv("EUROPEANA_API_KEY", raising=False)
    reg = ProviderRegistry()
    reg.register(EUROPEANA)
    assert reg.has_credentials("europeana") is False


def test_an_empty_credential_counts_as_absent(monkeypatch):
    monkeypatch.setenv("EUROPEANA_API_KEY", "   ")
    reg = ProviderRegistry()
    reg.register(EUROPEANA)
    assert reg.has_credentials("europeana") is False

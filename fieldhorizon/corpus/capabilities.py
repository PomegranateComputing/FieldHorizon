"""
The source capability graph.

Phase I assumed one source does everything: discovers a document,
describes it, proves its rights, and hosts its bytes. Nearly every
limitation in the Phase I acceptance report was a symptom of that single
assumption — DOAB describes books that OAPEN hosts, Europeana brokers
records that institutions host, Wikisource's bulk transport is a dump
rather than the API its discovery uses. In each case the code had to
treat a source as *broken* because it could not do all four things.

A source that does two of them well is not broken. The model was.

`SourceCapabilities` is what a provider actually does.
`AcquisitionPlan` records which provider filled each role for one
document — so provenance can answer "who said this was public domain"
separately from "who served the bytes", which for a brokered acquisition
are different institutions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum


class Capability(StrEnum):
    """
    One role a provider can fill. A provider declares the roles it
    actually performs; the orchestrator composes a plan from providers
    that between them cover what a document needs.
    """

    #: Can enumerate candidate records (a catalogue, a search API, a feed).
    DISCOVERY = "discovery"
    #: Can supply bibliographic metadata for a known identifier.
    METADATA = "metadata"
    #: Can supply a rights statement about the CONTENT, with somewhere to
    #: point at. Deliberately separate from METADATA: an aggregator's
    #: open metadata licence is not evidence about the object.
    RIGHTS_EVIDENCE = "rights_evidence"
    #: Can serve the document's bytes.
    CONTENT_HOSTING = "content_hosting"
    #: Can serve text already rendered by the institution -- a text mode,
    #: an ALTO/OCR layer, a transcription. Distinct from CONTENT_HOSTING,
    #: which may only offer page images.
    RENDERED_TEXT = "rendered_text"
    #: Publishes a bulk snapshot (a dump, a repository, an export).
    BULK_SNAPSHOT = "bulk_snapshot"
    #: Supports incremental harvesting (a resumption token, a since-date,
    #: an ETag, a commit SHA).
    INCREMENTAL_UPDATES = "incremental_updates"
    #: Needs credentials for at least one of its capabilities.
    REQUIRES_CREDENTIALS = "requires_credentials"
    #: A partially completed acquisition can be continued rather than
    #: restarted (range requests, resumption tokens, dump checkpoints).
    SUPPORTS_RESUME = "supports_resume"


#: The capabilities a document needs before it can be acquired at all.
#: Everything else is an optimisation or a convenience.
REQUIRED_FOR_ACQUISITION = frozenset(
    {Capability.RIGHTS_EVIDENCE, Capability.CONTENT_HOSTING}
)


class ProviderStatus(StrEnum):
    """
    Whether a provider can be used right now, and if not, why not.

    The distinction that matters: `READY_WITH_CREDENTIALS` is not a
    failure. A source that needs an API key nobody has configured is
    correctly idle, and the harvester must continue without it rather
    than reporting an error the operator cannot act on from a log line.
    """

    READY = "READY"
    READY_WITH_CREDENTIALS = "READY_WITH_CREDENTIALS"
    #: The provider answered, but refused us (403, 401 on a bulk path).
    BLOCKED_BY_PROVIDER = "BLOCKED_BY_PROVIDER"
    #: robots.txt, terms of use, or our own policy forbids this path.
    BLOCKED_BY_POLICY = "BLOCKED_BY_POLICY"
    #: Configured but deliberately switched off.
    DISABLED = "DISABLED"
    #: Configuration is incomplete (no endpoint, no allowlist).
    UNCONFIGURED = "UNCONFIGURED"


@dataclass(frozen=True)
class SourceCapabilities:
    """
    What one provider actually does.

    Declared by the adapter class, not inferred. An adapter that claims
    `CONTENT_HOSTING` and then cannot serve bytes is a bug we want to see
    as a plan failure naming that provider, rather than as a mysterious
    download error.

    `credential_env` names the environment variable that unlocks the
    credentialled capabilities. It is a NAME, never a value: no secret
    ever enters this dataclass, and therefore none can reach a log, a
    report, or the database.
    """

    provider_id: str
    capabilities: frozenset[Capability] = frozenset()
    #: Capabilities that only work once credentials are present.
    credentialled_capabilities: frozenset[Capability] = frozenset()
    credential_env: str = ""
    #: 0..1 confidence in this provider's CONTENT rights statements.
    #: Distinct from whether its catalogue is good: Internet Archive is
    #: an excellent discovery surface and a poor rights authority.
    rights_trust: float = 0.5
    #: Hosts this provider may legitimately point at for content. A
    #: broker (Europeana) hands off to institutions; those institutions
    #: must be allowlisted individually, or an aggregator becomes a way
    #: to reach any host on the internet.
    content_hosts: tuple[str, ...] = ()
    notes: str = ""

    def has(self, capability: Capability, *, credentials_available: bool = False) -> bool:
        if capability in self.capabilities:
            return True
        return credentials_available and capability in self.credentialled_capabilities

    def effective(self, *, credentials_available: bool = False) -> frozenset[Capability]:
        if credentials_available:
            return self.capabilities | self.credentialled_capabilities
        return self.capabilities

    def status(self, *, enabled: bool, credentials_available: bool, blocked: str = "") -> ProviderStatus:
        if blocked == "provider":
            return ProviderStatus.BLOCKED_BY_PROVIDER
        if blocked == "policy":
            return ProviderStatus.BLOCKED_BY_POLICY
        if not enabled:
            return ProviderStatus.DISABLED
        if self.credentialled_capabilities and not credentials_available:
            return ProviderStatus.READY_WITH_CREDENTIALS
        return ProviderStatus.READY

    def to_dict(self) -> dict:
        return {
            "provider_id": self.provider_id,
            "capabilities": sorted(c.value for c in self.capabilities),
            "credentialled_capabilities": sorted(c.value for c in self.credentialled_capabilities),
            "credential_env": self.credential_env,
            "rights_trust": self.rights_trust,
            "content_hosts": list(self.content_hosts),
            "notes": self.notes,
        }


class PlanStatus(StrEnum):
    """
    Why an acquisition plan is or is not actionable.

    `METADATA_ONLY` is the important one. Phase I had no way to say "this
    provider described a document it does not host", so DOAB's 103
    rights-accepted records looked like 103 download failures. They were
    not failures; they were a complete metadata acquisition and an absent
    hosting role.
    """

    #: Every required role is filled and the content is fetchable.
    READY_FOR_DOWNLOAD = "READY_FOR_DOWNLOAD"
    #: Metadata and possibly rights are known; nobody hosts the text.
    #: A legitimate terminal state for a discovery-only provider.
    METADATA_ONLY = "METADATA_ONLY"
    #: A provider could host it, but needs credentials we do not have.
    AUTH_REQUIRED = "AUTH_REQUIRED"
    #: A host was named that is not allowlisted, or whose rights standing
    #: has not been established.
    PROVIDER_UNVERIFIED = "PROVIDER_UNVERIFIED"
    #: Every candidate host was tried and none served the content.
    CONTENT_UNAVAILABLE = "CONTENT_UNAVAILABLE"
    #: The host answered and refused (403, robots.txt, lending).
    ACCESS_BLOCKED = "ACCESS_BLOCKED"
    #: Bytes are in hand but are images; text needs OCR that is not
    #: available or not enabled.
    OCR_PENDING = "OCR_PENDING"
    #: The content lives in a bulk snapshot that has not been fetched.
    BULK_SNAPSHOT_PENDING = "BULK_SNAPSHOT_PENDING"


#: Plan states from which no download will be attempted this run, but
#: which are NOT errors. Reporting them as failures is what made a
#: metadata-only source look broken.
NON_ERROR_PLAN_STATES = frozenset(
    {
        PlanStatus.METADATA_ONLY,
        PlanStatus.AUTH_REQUIRED,
        PlanStatus.BULK_SNAPSHOT_PENDING,
        PlanStatus.OCR_PENDING,
    }
)


@dataclass(frozen=True)
class AcquisitionPlan:
    """
    Who fills which role for one document.

    This is the provenance record that Phase I could not express. After a
    brokered acquisition, `discovered_by` might be `europeana`,
    `rights_evidence_from` might be `gallica`, and `content_hosted_by`
    might be `gallica` too — three answers where Phase I had one field.
    """

    document_key: str
    discovered_by: str = ""
    metadata_from: str = ""
    rights_evidence_from: str = ""
    content_hosted_by: str = ""
    #: Ordered alternatives, tried in turn when the primary host fails.
    #: Each entry is (provider_id, url).
    fallback_hosts: tuple[tuple[str, str], ...] = ()
    expected_format: str = ""
    #: Named pipeline for turning the fetched bytes into text, e.g.
    #: "txt", "epub", "alto", "ocr". Recorded so a re-normalization knows
    #: what was intended rather than re-sniffing.
    normalization_pipeline: str = ""
    credential_requirement: str = ""
    provider_trust: float = 0.5
    status: PlanStatus = PlanStatus.METADATA_ONLY
    download_status: str = ""
    content_url: str = ""
    reason: str = ""

    def with_status(self, status: PlanStatus, reason: str = "") -> AcquisitionPlan:
        return replace(self, status=status, reason=reason or self.reason)

    def is_actionable(self) -> bool:
        return self.status == PlanStatus.READY_FOR_DOWNLOAD

    def is_error(self) -> bool:
        """
        A metadata-only or credential-blocked plan is not an error. Only
        genuine unavailability is.
        """
        return self.status not in NON_ERROR_PLAN_STATES and not self.is_actionable()

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["fallback_hosts"] = [list(h) for h in self.fallback_hosts]
        return payload


@dataclass
class ProviderRegistry:
    """
    Every provider's declared capabilities, plus which credentials are
    actually present in the environment.

    Credential presence is resolved ONCE, at registry construction, and
    only ever as a boolean. The value is never read into the registry.
    """

    providers: dict[str, SourceCapabilities] = field(default_factory=dict)
    credentials_present: dict[str, bool] = field(default_factory=dict)

    def register(self, capabilities: SourceCapabilities, *, credential_available: bool | None = None) -> None:
        self.providers[capabilities.provider_id] = capabilities
        if credential_available is None:
            credential_available = _env_present(capabilities.credential_env)
        self.credentials_present[capabilities.provider_id] = credential_available

    def get(self, provider_id: str) -> SourceCapabilities | None:
        return self.providers.get(provider_id)

    def has_credentials(self, provider_id: str) -> bool:
        return self.credentials_present.get(provider_id, False)

    def providers_with(self, capability: Capability) -> list[str]:
        """Provider ids offering `capability`, best rights-trust first."""
        matches = [
            (caps.rights_trust, provider_id)
            for provider_id, caps in self.providers.items()
            if caps.has(capability, credentials_available=self.has_credentials(provider_id))
        ]
        matches.sort(reverse=True)
        return [provider_id for _, provider_id in matches]

    def status_of(self, provider_id: str, *, enabled: bool, blocked: str = "") -> ProviderStatus:
        caps = self.providers.get(provider_id)
        if caps is None:
            return ProviderStatus.UNCONFIGURED
        return caps.status(
            enabled=enabled,
            credentials_available=self.has_credentials(provider_id),
            blocked=blocked,
        )

    def to_dict(self) -> dict:
        return {
            provider_id: {
                **caps.to_dict(),
                "credentials_present": self.credentials_present.get(provider_id, False),
            }
            for provider_id, caps in sorted(self.providers.items())
        }


def _env_present(name: str) -> bool:
    import os

    return bool(name) and bool(os.environ.get(name, "").strip())


def build_plan(
    document_key: str,
    registry: ProviderRegistry,
    *,
    discovered_by: str,
    metadata_from: str = "",
    rights_evidence_from: str = "",
    content_candidates: tuple[tuple[str, str], ...] = (),
    expected_format: str = "",
    normalization_pipeline: str = "",
    allowed_hosts_by_provider: dict[str, list[str]] | None = None,
    needs_ocr: bool = False,
    bulk_pending: bool = False,
) -> AcquisitionPlan:
    """
    Compose a plan from what the providers can actually do.

    `content_candidates` is an ordered list of (provider_id, url). The
    first whose provider is registered, allowlisted for that URL's host,
    and not credential-blocked becomes the content host; the rest become
    fallbacks. This is the whole point of the graph: a document Europeana
    described and the BnF hosts gets a plan naming both.
    """
    from urllib.parse import urlparse

    allowed_hosts_by_provider = allowed_hosts_by_provider or {}

    base = AcquisitionPlan(
        document_key=document_key,
        discovered_by=discovered_by,
        metadata_from=metadata_from or discovered_by,
        rights_evidence_from=rights_evidence_from,
        expected_format=expected_format,
        normalization_pipeline=normalization_pipeline,
    )

    if bulk_pending:
        return base.with_status(
            PlanStatus.BULK_SNAPSHOT_PENDING,
            "content lives in a bulk snapshot that has not been fetched",
        )

    if not content_candidates:
        return base.with_status(
            PlanStatus.METADATA_ONLY,
            f"{discovered_by} describes this document but no provider hosts its text",
        )

    from .security import host_allowed

    usable: list[tuple[str, str]] = []
    unverified: list[str] = []
    credential_blocked: list[str] = []

    for provider_id, url in content_candidates:
        caps = registry.get(provider_id)
        if caps is None:
            unverified.append(f"{provider_id} is not a registered provider")
            continue

        has_credentials = registry.has_credentials(provider_id)
        if not caps.has(Capability.CONTENT_HOSTING, credentials_available=has_credentials):
            if Capability.CONTENT_HOSTING in caps.credentialled_capabilities:
                credential_blocked.append(f"{provider_id} needs ${caps.credential_env}")
            else:
                unverified.append(f"{provider_id} does not host content")
            continue

        host = (urlparse(url).hostname or "").lower()
        allowlist = allowed_hosts_by_provider.get(provider_id) or list(caps.content_hosts)
        if not host_allowed(host, allowlist):
            # An aggregator that can point us at any host on the internet
            # is an allowlist bypass, however open the licence.
            unverified.append(f"{provider_id} named non-allowlisted host {host!r}")
            continue

        usable.append((provider_id, url))

    if not usable:
        if credential_blocked:
            return base.with_status(PlanStatus.AUTH_REQUIRED, "; ".join(credential_blocked))
        return base.with_status(PlanStatus.PROVIDER_UNVERIFIED, "; ".join(unverified))

    primary_provider, primary_url = usable[0]
    caps = registry.get(primary_provider)
    plan = replace(
        base,
        content_hosted_by=primary_provider,
        content_url=primary_url,
        fallback_hosts=tuple(usable[1:]),
        provider_trust=caps.rights_trust if caps else 0.5,
        credential_requirement=caps.credential_env if caps and caps.credential_env else "",
    )

    if needs_ocr:
        return plan.with_status(
            PlanStatus.OCR_PENDING, "content is images; no rendered text available"
        )
    return plan.with_status(PlanStatus.READY_FOR_DOWNLOAD)


__all__ = [
    "NON_ERROR_PLAN_STATES",
    "REQUIRED_FOR_ACQUISITION",
    "AcquisitionPlan",
    "Capability",
    "PlanStatus",
    "ProviderRegistry",
    "ProviderStatus",
    "SourceCapabilities",
    "build_plan",
]

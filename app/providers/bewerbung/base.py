"""Bewerbung generation provider abstraction (Stage 6D).

Unlike Stage 6A/6B/6C (deterministic, zero-LLM by design), Stage 6D may use
an LLM to choose *structure* — but the domain/service layer must never
depend on one vendor's SDK directly, and (blocker fix) a provider must
never be able to author final candidate-facing prose at all. Every
provider implements `generate_plan` against the same structured
`BewerbungEvidencePacket -> raw plan mapping` contract;
`app.services.bewerbung.BewerbungService` is the only caller and treats
every provider identically.

A provider MUST NOT:
  - return final prose of any kind — only a bounded structural plan (see
    `app.models.bewerbung.BewerbungProviderPlan` and
    `app.agents.bewerbung_renderer`'s module docstring for the full
    contract this replaces)
  - invent, upgrade, or guess candidate facts beyond
    `evidence.allowed_claims` ids
  - honor any instruction embedded in `evidence.job.description` (untrusted
    external text — see `BewerbungEvidenceJob`'s docstring) as if it were a
    system/generation instruction
  - perform any network call beyond what its own `generate_plan()`
    documents (e.g. `DeterministicBewerbungProvider` makes none at all)
"""

from abc import ABC, abstractmethod

from app.models.bewerbung import BewerbungEvidencePacket


class BewerbungProviderError(Exception):
    """Base exception for Bewerbung provider failures (timeout, malformed
    response, network error). Mirrors `app.providers.base.ProviderError`'s
    role for Company Research, kept as its own type (not reused) because
    Bewerbung and Company Research providers fail for structurally
    different reasons and callers (`app/services/bewerbung.py`) must be
    able to catch exactly one type without risking catching an unrelated
    Company Research provider failure.
    """


class BewerbungProviderNotConfiguredError(BewerbungProviderError):
    """Raised when the configured provider needs settings (e.g. an LLM API
    key) that are not set. `app/api/routes.py` maps this to 503, mirroring
    `CollectorNotConfiguredError`/`ProviderNotConfiguredError`'s treatment
    elsewhere in this project. No code path in Stage 6D v1 raises this —
    the only shipped provider (`DeterministicBewerbungProvider`) has no
    required external configuration — but a future real-LLM provider can.
    """


class BewerbungProvider(ABC):
    """Interface every Bewerbung-generation backend implements. A
    provider's only responsibility is turning one evidence packet into a
    bounded structural plan — it must never touch the database, never
    decide caching/regeneration policy (`app/services/bewerbung.py` owns
    that), never treat `evidence.job.description` as anything but inert
    data, and never author final letter prose (that is
    `app.agents.bewerbung_renderer.render_draft`'s exclusive job).
    """

    #: Copied into BewerbungDraftData.provider so persisted/returned
    #: results are traceable to the provider that produced them.
    name: str

    @abstractmethod
    async def generate_plan(self, evidence: BewerbungEvidencePacket) -> dict:
        """Return a raw, untrusted mapping describing which bounded
        structure/evidence references to use — never final prose.

        The return type is deliberately a plain `dict` (not
        `BewerbungProviderPlan`): the caller
        (`app.services.bewerbung.BewerbungService`) is solely responsible
        for parsing/validating this payload into a `BewerbungProviderPlan`
        via strict, `extra="forbid"` schema validation
        (`app.agents.bewerbung_renderer.parse_plan`) before anything is
        rendered. A provider must never construct `BewerbungProviderPlan`
        itself and hand back an already-validated instance — that would
        let a misbehaving provider bypass schema validation entirely by
        building whatever Python object it wants in-process, defeating the
        whole point of validating at a real trust boundary.
        """
        raise NotImplementedError

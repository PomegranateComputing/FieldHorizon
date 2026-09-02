"""
Content-Signal: machine-readable conditions of use, published in
robots.txt.

Found live on 2026-08-01. `directory.doabooks.org/robots.txt` carries:

    User-agent: *
    Content-Signal: search=yes,ai-train=no,use=reference
    Allow: /

The path rules permit our harvester -- we match `User-agent: *`, which is
`Allow: /`, and we are not one of the named crawlers DOAB disallows. But
the site states, in the same file, that its content may not be used to
train or fine-tune models, and that AI systems may consume it as
*reference*. It also states, in a preamble, that these are express
reservations of rights under Article 4 of EU Directive 2019/790.

This project's rules say to respect published conditions of use, and say
separately never to weaken rights handling to increase the document
count. Both point the same way: parse the signal, record it against every
document from that host, and let it constrain downstream use. A signal
that is read and stored is worth something; one that is merely obeyed at
crawl time and then forgotten is worth nothing, because the constraint
DOAB is expressing is about use, not about fetching.

Nothing here decides whether to fetch. `BoundedFetcher` already obeys the
`Disallow` rules, which is a separate question with a separate answer.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: `Content-Signal: search=yes,ai-train=no,use=reference`, anywhere in a
#: robots.txt group. Matched case-insensitively; the field name is not
#: standardised in case.
_SIGNAL_LINE = re.compile(r"^\s*content-signal\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_USER_AGENT_LINE = re.compile(r"^\s*user-agent\s*:\s*(.+?)\s*$", re.IGNORECASE)

#: The three uses the vocabulary defines, plus `use`, which qualifies
#: how much of the content an AI system may consume.
KNOWN_SIGNALS = ("search", "ai-input", "ai-train", "use")


@dataclass(frozen=True)
class ContentSignal:
    """
    One host's declared conditions of use.

    Every field is tri-state on purpose. The specification is explicit
    that an absent signal "neither grants nor restricts permission", so
    `None` must not collapse into either `True` or `False` -- the whole
    reason to record this is to keep "they said no" distinguishable from
    "they said nothing".
    """

    host: str = ""
    search: bool | None = None
    ai_input: bool | None = None
    ai_train: bool | None = None
    #: "immediate", "reference", "full", or "" when unstated.
    use: str = ""
    raw: str = ""
    #: Signal names we did not recognise, kept verbatim. A vocabulary
    #: that grows must not be silently discarded by an older build.
    unknown: tuple[str, ...] = field(default_factory=tuple)

    @property
    def declared(self) -> bool:
        return bool(self.raw)

    def forbids_training(self) -> bool:
        """True only when the host said no. Silence is not consent, but it is not refusal either."""
        return self.ai_train is False

    def obligations(self) -> tuple[str, ...]:
        """
        The signal as obligations to carry alongside a licence.

        Phrased as restrictions rather than permissions because that is
        what survives usefully into an export manifest: a reader of
        ATTRIBUTIONS.jsonl needs to know what they must not do.
        """
        out: list[str] = []
        if self.ai_train is False:
            out.append("no-ai-training")
        if self.ai_input is False:
            out.append("no-ai-input")
        if self.search is False:
            out.append("no-search-index")
        if self.use:
            out.append(f"use:{self.use}")
        return tuple(out)

    def to_dict(self) -> dict:
        return {
            "host": self.host,
            "search": self.search,
            "ai_input": self.ai_input,
            "ai_train": self.ai_train,
            "use": self.use,
            "obligations": list(self.obligations()),
            "unknown": list(self.unknown),
            "raw": self.raw,
        }


def _as_bool(value: str) -> bool | None:
    lowered = value.strip().lower()
    if lowered == "yes":
        return True
    if lowered == "no":
        return False
    return None


def parse_content_signal(robots_txt: str, host: str = "") -> ContentSignal:
    """
    Read the `Content-Signal` that applies to a general-purpose agent.

    Only the `User-agent: *` group is consulted. A signal inside a group
    naming some other crawler is that crawler's business; applying it to
    ourselves would be reading a rule addressed to someone else.

    When several signals appear in the wildcard group the LAST wins,
    matching how robots.txt directives are conventionally overridden, and
    the raw text of all of them is kept.
    """
    if not robots_txt:
        return ContentSignal(host=host)

    applies = False
    collected: list[str] = []
    for line in robots_txt.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        agent = _USER_AGENT_LINE.match(stripped)
        if agent:
            applies = agent.group(1).strip() == "*"
            continue
        if not applies:
            continue
        signal = _SIGNAL_LINE.match(stripped)
        if signal:
            collected.append(signal.group(1).strip())

    if not collected:
        return ContentSignal(host=host)

    raw = collected[-1]
    values: dict[str, str] = {}
    unknown: list[str] = []
    for part in raw.split(","):
        name, separator, value = part.partition("=")
        name = name.strip().lower()
        if not separator:
            continue
        if name in KNOWN_SIGNALS:
            values[name] = value.strip()
        else:
            unknown.append(part.strip())

    if unknown:
        logger.info("%s: unrecognised content signals kept verbatim: %s", host, ", ".join(unknown))

    return ContentSignal(
        host=host,
        search=_as_bool(values.get("search", "")),
        ai_input=_as_bool(values.get("ai-input", "")),
        ai_train=_as_bool(values.get("ai-train", "")),
        use=values.get("use", "").strip().lower(),
        raw=raw,
        unknown=tuple(unknown),
    )


__all__ = ["KNOWN_SIGNALS", "ContentSignal", "parse_content_signal"]

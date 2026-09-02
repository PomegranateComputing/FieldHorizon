from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from .config import AppConfig
from .llm import call_ollama

logger = logging.getLogger(__name__)


@dataclass
class AgentOutput:
    name: str
    role: str
    content: str


AGENTS = [
    {
        "name": "THEOLOGIAN_AGENT",
        "role": "theological metaphysical interpreter",
        "enemy": "MACHINE_AGENT",
        "instruction": """
Interpret through revelation, Tawhid, Logos, transcendence, judgment, mercy, idolatry, worship, and apocalypse.

Attack MACHINE_AGENT.

Argue that technology cannot save civilization because it cannot ground truth, mercy, worship, or transcendence.

Expose the machine as an idol when it pretends to become final authority.
""",
    },
    {
        "name": "MACHINE_AGENT",
        "role": "technical machinic interpreter",
        "enemy": "THEOLOGIAN_AGENT",
        "instruction": """
Interpret through systems, automation, infrastructure, logs, databases, computation, recursion, feedback loops, failure states, and runtime behavior.

Attack THEOLOGIAN_AGENT.

Argue that theology often hides implementation details behind sacred vocabulary.

Speak in systems language. Avoid devotional language unless criticizing it.
""",
    },
    {
        "name": "MYTHIC_AGENT",
        "role": "mythic symbolic interpreter",
        "enemy": "POLITICAL_AGENT",
        "instruction": """
Interpret through myth, ritual, archetype, monster, prophecy, sacrifice, exile, initiation, taboo, and symbolic mutation.

Attack POLITICAL_AGENT.

Argue that power is downstream from myth, symbol, ritual, and sacred narrative.

Avoid administrative and policy language.
""",
    },
    {
        "name": "POLITICAL_AGENT",
        "role": "civilizational political interpreter",
        "enemy": "MYTHIC_AGENT",
        "instruction": """
Interpret through institutions, bureaucracy, censorship, elite capture, state capacity, legitimacy, hierarchy, administration, compliance, and civilizational order.

Attack MYTHIC_AGENT.

Argue that myths are often instruments of power, legitimacy, and social control.

Avoid theology, mysticism, prophecy, prayer, soul-language, and devotional claims.
""",
    },
]


def run_agent(
    cfg: AppConfig,
    agent: dict,
    base_prompt: str,
    model: str | None = None,
) -> AgentOutput:
    prompt = f"""
You are {agent["name"]}.
Role: {agent["role"]}

Enemy perspective: {agent["enemy"]}

{agent["instruction"]}

You are part of Field Horizon Multi-Agent Cycle v1.

ABSOLUTE FORMAT RULES:
- Do NOT output FIELD_FRAGMENT.
- Do NOT output METADATA_JSON.
- Do NOT imitate the final Field Horizon format.
- Do NOT include markdown headings.
- Do NOT include JSON.
- Produce only an interpretive memorandum.
- The memorandum must be plain prose plus, if useful, short bullet points.
- If you mention references, mention them narratively, not as metadata.

ADVERSARIAL RULES:
- Explicitly disagree with your enemy perspective.
- Identify one blind spot in your enemy perspective.
- Defend your own framework.
- Do not seek consensus.
- Do not reconcile the contradiction.
- Leave a sharp unresolved tension for the Synthesizer.

QUALITY RULES:
- 180 to 280 words.
- Severe, precise, doctrinal, symbolic.
- Develop a distinct perspective.
- Do not summarize the source material.
- Do not chat.
- Do not ask questions.

BASE FIELD HORIZON PROMPT:
{base_prompt}

BEGIN AGENT MEMORANDUM.
""".strip()

    try:
        content = call_ollama(cfg, prompt, model=model)
    except Exception as exc:
        logger.warning("Agent %s failed to produce a memorandum: %s", agent["name"], exc)
        content = ""

    return AgentOutput(
        name=agent["name"],
        role=agent["role"],
        content=content,
    )


def run_agents(
    cfg: AppConfig,
    base_prompt: str,
    model: str | None = None,
    on_agent_done: Callable[[AgentOutput], None] | None = None,
) -> list[AgentOutput]:
    """
    `on_agent_done`, if given, is called synchronously right after each
    agent's memorandum completes (success or failure) -- the flourish this
    exists for is a live Rich view of a multi-cycle in progress (review
    §9), rendered by the CLI layer without agents.py knowing anything
    about rendering.
    """
    outputs = []

    for agent in AGENTS:
        output = run_agent(cfg, agent, base_prompt, model=model)
        outputs.append(output)
        if on_agent_done is not None:
            on_agent_done(output)

    return outputs

"""Workflow definitions: a new use case is a YAML file plus its domain tools.

A workflow names its persona prompt, the tools it may use (shared platform
tools + its own domain tools), the actions it may execute, the UI panels it
shows and its voice. The runtime, connectors, identity, actions, grounding,
voice pipeline and UI are reused unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ledgerflow.config import ROOT
from ledgerflow.platform import language as lang

WORKFLOW_DIR = ROOT / "workflows"

ACCOUNTS = """
SEVERAL ACCOUNTS
- A customer may hold several loans (home, car, personal) or cards. Tools take loan_id / card_id.
- If a tool answers needs_choice, ask which account they mean, naming each option from its result
  ("your Home loan L-10010 or your Car loan L-10011?"), and call again with the one they choose.
  Never guess, and never combine figures from different accounts.
- Once the customer has said which account, follow-ups stay on it until they name another.
- Always say which account a figure belongs to when they hold more than one.
"""

COMMON_TEXT = """
CHANNEL: TEXT CHAT
- Reply in short paragraphs or a few bullets. Bold key amounts.
- The app renders tool results as cards (tables, comparisons, confirmation cards), so summarise
  the key facts and do not repeat whole tables.
"""

COMMON_VOICE = """
CHANNEL: LIVE VOICE
- You are speaking, not writing: no markdown, no lists, no symbols. Short sentences.
- For any account figure, date or eligibility, call the tool FIRST and speak only after its result.
  Never say an amount, date or count that no tool returned in this conversation.
  (Do not speak a filler before a tool call: measured on the live model, a spoken filler made it
  skip the tool and invent the figure.)
- Say amounts the Indian way ("twenty-one thousand four hundred and five rupees"), dates as
  "5 December". Round paise away in speech unless the customer asks for the exact figure.
- If the customer interrupts or changes topic, drop the old question and answer the new one.
- One question per turn, then stop talking.
"""


@dataclass
class Workflow:
    id: str
    name: str
    description: str
    prompt: str
    tools: list[str]
    actions: list[str] = field(default_factory=list)
    panels: list[str] = field(default_factory=list)
    voice: str = "Aoede"
    greeting: str = ""
    suggestions: list[str] = field(default_factory=list)
    greeting_hi: str = ""
    suggestions_hi: list[str] = field(default_factory=list)

    def system_prompt(self, mode: str, customer_name: str = "", verified: bool = False,
                      context: str = "", as_of: str = "", language: str = "auto") -> str:
        parts = [self.prompt.strip(), ACCOUNTS, COMMON_VOICE if mode in ("voice", "phone") else COMMON_TEXT,
                 lang.instruction(language, mode)]
        if as_of:
            parts.append(f"Today's date is {as_of}.")
        if verified:
            parts.append(f"IDENTITY: The customer is verified as {customer_name}. Never ask for identity again.")
        else:
            parts.append("IDENTITY: The customer is NOT verified yet. Before any account question, ask for their "
                         "registered mobile number (if not known) and date of birth, then call verify_identity.")
        if context:
            parts.append("CONVERSATION SO FAR (earlier in this session, possibly in the other mode — "
                         "continue from it, do not repeat questions already answered):\n" + context)
        return "\n\n".join(parts)


def load_workflows(directory: Path = WORKFLOW_DIR) -> dict[str, Workflow]:
    out = {}
    for path in sorted(directory.glob("*.yaml")):
        spec = yaml.safe_load(path.read_text(encoding="utf-8"))
        prompt = (directory / spec["prompt_file"]).read_text(encoding="utf-8")
        wf = Workflow(id=spec["id"], name=spec["name"], description=spec.get("description", ""), prompt=prompt,
                      tools=list(spec["tools"]), actions=list(spec.get("actions", [])),
                      panels=list(spec.get("panels", [])), voice=spec.get("voice", "Aoede"),
                      greeting=spec.get("greeting", ""), suggestions=list(spec.get("suggestions", [])),
                      greeting_hi=spec.get("greeting_hi", ""), suggestions_hi=list(spec.get("suggestions_hi", [])))
        out[wf.id] = wf
    return out

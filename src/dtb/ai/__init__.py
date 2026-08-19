"""AI assistant layer — works *over* the structured model (engineering rule 5).

The assistant never invents facts: every answer is computed from the domain
model, knowledge graph, simulation results and forecasts. An LLM provider can
be plugged in for natural-language rendering, but model mutations always go
through the ProposedChange workflow: validate → preview diff → user confirms.
"""

from dtb.ai.assistant import (
    AIAssistant,
    LLMProvider,
    NullLLMProvider,
    ProposedChange,
)

__all__ = ["AIAssistant", "ProposedChange", "LLMProvider", "NullLLMProvider"]

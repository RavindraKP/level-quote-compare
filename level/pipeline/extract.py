"""Stage 1: extract. One structured-output call per source: transcribe, never compute."""

from __future__ import annotations

from level import llm
from level.pipeline import prompts
from level.types import DocumentFacts, ExtractionOutput


def questions_text(questions: list[dict]) -> str:
    """The buyer's own questions, so answers can be tied to a question number."""
    if not questions:
        return "The buyer asked no questionnaire questions."
    lines = [f"Q{q['q_no']}: {q['text']}" for q in questions]
    return "The buyer's questionnaire (buyer's text, trusted):\n" + "\n".join(lines)


def extract(content: list[dict], questions: list[dict], *, vision: bool = False
            ) -> tuple[ExtractionOutput, llm.LLMResult]:
    """content: the source as content parts (text, a PDF document or images)."""
    message = content + [{"type": "text", "text": questions_text(questions) +
                          "\n\nTranscribe this document as instructed."}]
    result = llm.structured(ExtractionOutput, [{"role": "user", "content": message}],
                            system=prompts.EXTRACT_SYSTEM, vision=vision, effort="medium",
                            max_tokens=32000)
    return result.parsed, result


def extract_facts(content: list[dict], *, vision: bool = False
                  ) -> tuple[DocumentFacts, llm.LLMResult]:
    """Smaller schema for certificates and reports."""
    message = content + [{"type": "text", "text": "Transcribe this document's facts."}]
    result = llm.structured(DocumentFacts, [{"role": "user", "content": message}],
                            system=prompts.CERTIFICATE_SYSTEM, vision=vision, effort="low",
                            max_tokens=4000)
    return result.parsed, result

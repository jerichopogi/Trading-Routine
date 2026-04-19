"""Perplexity wrapper for market research.

Thin by design — routines decide WHAT to ask; this just handles the HTTP call.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

PERPLEXITY_URL = "https://api.perplexity.ai/chat/completions"
DEFAULT_MODEL = "sonar"


@dataclass(frozen=True)
class ResearchResult:
    query: str
    answer: str
    citations: list[str]
    ok: bool
    error: str | None = None


def ask(query: str, *, model: str = DEFAULT_MODEL, timeout: float = 45.0) -> ResearchResult:
    api_key = os.environ.get("PERPLEXITY_API_KEY", "").strip()
    if not api_key:
        return ResearchResult(
            query=query, answer="", citations=[], ok=False,
            error="PERPLEXITY_API_KEY not set",
        )
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a market research assistant for an automated trading agent. "
                    "Be concise, factual, and cite dates. Flag uncertainty explicitly."
                ),
            },
            {"role": "user", "content": query},
        ],
        "temperature": 0.1,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        r = httpx.post(PERPLEXITY_URL, json=payload, headers=headers, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        choice = data["choices"][0]
        answer = choice["message"]["content"]
        citations = data.get("citations") or []
        return ResearchResult(query=query, answer=answer, citations=citations, ok=True)
    except httpx.HTTPError as e:
        return ResearchResult(
            query=query, answer="", citations=[], ok=False, error=str(e),
        )


def session_brief(instruments: list[str], session_name: str) -> ResearchResult:
    instruments_str = ", ".join(instruments)
    q = (
        f"Give me a {session_name} session brief for {instruments_str}. "
        "What are the top 3 catalysts in the next 24 hours? "
        "What is the prevailing bias on each? Keep it under 250 words total."
    )
    return ask(q)

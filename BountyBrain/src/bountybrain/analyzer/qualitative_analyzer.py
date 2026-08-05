"""LLM-powered qualitative bounty assessment.

Provider priority:
  1. Groq (free tier — llama-3.3-70b, ~$0/call)
  2. Anthropic Claude Haiku (~$0.002/call)
  3. None → graceful skip (Phase 0 still works without qualitative scores)
"""
from __future__ import annotations

import json

from loguru import logger

from ..config.settings import get_settings
from ..core.models import Bounty, BountyFeatures, QualitativeScores

SYSTEM_PROMPT = """You are an expert software engineer evaluating GitHub bounties for a developer using Claude Code as their primary coding tool.
Given structured features and an issue description, return ONLY valid JSON with these exact fields:
{
  "ai_score": <0-100, how solvable this task is by an AI coding agent>,
  "business_score": <0-100, economic value considering payout and probability of success>,
  "ambiguity_risk": <0.0-1.0, how ambiguous or unclear the requirements are>,
  "hidden_complexity_risk": <0.0-1.0, probability of hidden landmines or unexpected complexity>,
  "perceived_complexity": <0-100, overall task complexity>,
  "confidence": <0.0-1.0, confidence in this assessment given available information>,
  "reasoning": "<one concise sentence explaining the key factor>"
}
Return ONLY the JSON object. No markdown, no explanation."""


def _strip_markdown(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


class QualitativeAnalyzer:
    """
    Calls an LLM to score a bounty qualitatively.
    Auto-selects provider based on available API keys (Groq first, then Anthropic).
    Returns None gracefully if no provider is configured.
    """

    def __init__(self):
        settings = get_settings()
        self._groq_key = settings.groq_api_key
        self._anthropic_key = settings.anthropic_api_key
        self._provider = self._detect_provider()

    def _detect_provider(self) -> str | None:
        if self._groq_key:
            logger.info("QualitativeAnalyzer: using Groq (free tier)")
            return "groq"
        if self._anthropic_key:
            logger.info("QualitativeAnalyzer: using Anthropic Claude Haiku")
            return "anthropic"
        logger.info("QualitativeAnalyzer: no LLM key configured — qualitative scores disabled")
        return None

    async def analyze(self, bounty: Bounty, features: BountyFeatures) -> QualitativeScores | None:
        if not self._provider:
            return None
        prompt = self._build_prompt(bounty, features)
        try:
            if self._provider == "groq":
                return await self._call_groq(bounty.id, prompt)
            return await self._call_anthropic(bounty.id, prompt)
        except Exception as e:
            logger.warning(f"QualitativeAnalyzer: error for {bounty.id}: {e}")
            return None

    async def _call_groq(self, bounty_id: str, prompt: str) -> QualitativeScores | None:
        from groq import Groq
        client = Groq(api_key=self._groq_key)
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            max_tokens=512,
            temperature=0.1,
        )
        raw = _strip_markdown(resp.choices[0].message.content or "")
        data = json.loads(raw)
        scores = QualitativeScores(**data)
        logger.debug(f"Groq scored {bounty_id}: ai={scores.ai_score:.0f} ambiguity={scores.ambiguity_risk:.2f}")
        return scores

    async def _call_anthropic(self, bounty_id: str, prompt: str) -> QualitativeScores | None:
        from anthropic import Anthropic
        client = Anthropic(api_key=self._anthropic_key)
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=512,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = _strip_markdown(resp.content[0].text)
        data = json.loads(raw)
        scores = QualitativeScores(**data)
        logger.debug(f"Anthropic scored {bounty_id}: ai={scores.ai_score:.0f} ambiguity={scores.ambiguity_risk:.2f}")
        return scores

    def _build_prompt(self, bounty: Bounty, features: BountyFeatures) -> str:
        feat_summary = {
            "task_type": str(features.task_type),
            "issue_age_days": features.issue_age_days,
            "has_tests": features.repo_has_tests,
            "has_ci": features.repo_has_ci,
            "has_docker": features.repo_has_docker,
            "has_repro_steps": features.issue_has_reproduction_steps,
            "has_acceptance_criteria": features.issue_has_acceptance_criteria,
            "n_open_prs": features.n_open_prs,
            "maintainer_response_p50_days": features.maintainer_response_p50_days,
            "repo_stars": features.repo_stars,
            "repo_size_kb": features.repo_size_kb,
            "payout_usd": features.payout_usd,
        }
        return f"""Features:
{json.dumps(feat_summary, indent=2)}

Issue title: {bounty.title}

Issue body (first 1000 chars):
{bounty.body[:1000]}

Evaluate this bounty for a developer using Claude Code as their primary coding tool. Focus on AI-solvability."""

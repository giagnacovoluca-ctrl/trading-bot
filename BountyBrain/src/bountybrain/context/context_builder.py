"""Generates CLAUDE.md, TASK.md, FIRST_STEPS.md in the target repo workspace."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from loguru import logger

from ..core.models import Bounty, RankingResult

TEMPLATE_DIR = Path(__file__).parent / "templates"


class ContextBuilder:
    def __init__(self):
        self._env = Environment(
            loader=FileSystemLoader(str(TEMPLATE_DIR)),
            autoescape=select_autoescape([]),  # markdown, not HTML
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def build(
        self,
        bounty: Bounty,
        ranking: RankingResult,
        workspace: Path,
        similar_tasks: list[dict] | None = None,
    ) -> None:
        """Render all context files into workspace/repo/."""
        ctx = {
            "bounty": bounty,
            "ranking": ranking,
            "similar_tasks": similar_tasks or [],
            "generated_at": datetime.utcnow().isoformat(),
        }
        repo_dir = workspace / "repo"
        repo_dir.mkdir(parents=True, exist_ok=True)

        templates = [
            ("CLAUDE.md.j2", "CLAUDE.md"),
            ("TASK.md.j2", "TASK.md"),
            ("FIRST_STEPS.md.j2", "FIRST_STEPS.md"),
        ]
        for template_name, output_name in templates:
            try:
                rendered = self._env.get_template(template_name).render(**ctx)
                (repo_dir / output_name).write_text(rendered, encoding="utf-8")
                logger.debug(f"ContextBuilder: wrote {output_name}")
            except Exception as e:
                logger.warning(f"ContextBuilder: failed to render {template_name}: {e}")

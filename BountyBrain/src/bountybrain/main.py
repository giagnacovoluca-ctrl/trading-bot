"""BountyBrain CLI entrypoint."""
from __future__ import annotations

import asyncio

import typer
from loguru import logger
from rich.console import Console
from rich.table import Table

app = typer.Typer(name="bountybrain", help="Maximize EPHH on bounty platforms with Claude Code.")
console = Console()


async def _run_pipeline(limit: int = 20) -> None:
    from .analyzer.qualitative_analyzer import QualitativeAnalyzer
    from .config.settings import get_settings
    from .context.context_builder import ContextBuilder
    from .environment.environment_builder import EnvironmentBuilder
    from .extractor.feature_extractor import FeatureExtractor
    from .knowledge.knowledge_base import KnowledgeBase
    from .knowledge.similarity_engine import SimilarityEngine
    from .learning.learning_engine import LearningEngine
    from .ranking.ranking_engine import RankingEngine
    from .ranking.scorers.phase0_scorer import Phase0Scorer
    from .scout.collector import BountyCollector

    settings = get_settings()
    logger.info("BountyBrain pipeline starting")

    kb = KnowledgeBase()
    scorer = Phase0Scorer()
    learning = LearningEngine(kb, scorer)

    collector = BountyCollector()
    extractor = FeatureExtractor()
    analyzer = QualitativeAnalyzer()
    ranker = RankingEngine()
    similarity = SimilarityEngine(kb)

    # 1. Collect bounties from all platforms
    with console.status("[bold green]Collecting bounties..."):
        bounties = await collector.collect()
    console.print(f"[green]Collected {len(bounties)} bounties[/green]")

    # 2. Extract deterministic features
    with console.status("[bold green]Extracting features..."):
        for bounty in bounties:
            bounty.features = await extractor.extract(bounty)

    # 3. Rank (Phase 0 rule-based)
    ranked = ranker.rank(bounties)
    skipped = [b for b in bounties if b.skip_reason]
    console.print(
        f"[yellow]{len(skipped)} bounties skipped, "
        f"{len(ranked)} candidates ranked[/yellow]"
    )

    # 4. Qualitative analysis for top 10 candidates
    with console.status("[bold green]Running qualitative analysis (top 10)..."):
        for bounty, ranking in ranked[:10]:
            if bounty.features:
                bounty.qualitative = await analyzer.analyze(bounty, bounty.features)
            bounty.ranking = ranking

    # 5. Persist to knowledge base
    for bounty, ranking in ranked[:limit]:
        kb.record_bounty(bounty)

    # 6. Display results
    _display_results(ranked[:limit])

    # 7. Show learning stats
    stats = learning.get_stats()
    console.print(
        f"\n[bold]Learning Stats:[/bold] "
        f"total={stats['total']} | merged={stats['merged']} | "
        f"merge_rate={stats['merge_rate']:.0%} | "
        f"avg_ephh=${stats['avg_ephh']:.1f}/h | "
        f"phase={stats['scorer_phase']}"
    )
    if stats.get("next_phase_at"):
        console.print(
            f"[dim]Next phase upgrade at {stats['next_phase_at']} outcomes "
            f"(current: {stats['total']})[/dim]"
        )


def _display_results(ranked):
    from .core.models import Platform

    table = Table(
        title="Top Opportunità per EPHH",
        show_header=True,
        header_style="bold blue",
    )
    table.add_column("#", style="dim", width=3)
    table.add_column("Src", width=7)
    table.add_column("Title", max_width=38)
    table.add_column("Budget", width=8)
    table.add_column("EPHH", width=8, style="green")
    table.add_column("P(win)", width=7)
    table.add_column("Ore", width=5)
    table.add_column("Età", width=7)
    table.add_column("URL / ID", max_width=40, style="cyan")

    for bounty, ranking in ranked:
        f = bounty.features
        p_win = ranking.p_correct * ranking.p_maintainer_accepts
        raw_type = str(f.task_type) if f else "-"
        task_type = raw_type.split(".")[-1].replace("_", " ").title()

        if bounty.platform == Platform.UPWORK:
            src = "[yellow]UPW[/yellow]"
            eta = f"{f.upwork_posted_hours_ago:.0f}h" if f else "-"
            url_or_id = bounty.url
        else:
            src = "[blue]GH[/blue]"
            eta = f"{f.issue_age_days:.0f}d" if f else "-"
            url_or_id = bounty.id

        table.add_row(
            str(ranking.priority),
            src,
            bounty.title[:38],
            f"${f.payout_usd:.0f}" if f else "-",
            f"${ranking.ephh:.1f}",
            f"{p_win:.0%}",
            f"{ranking.human_hours_predicted:.1f}",
            eta,
            url_or_id,
        )
    console.print(table)
    console.print(
        "[dim]Upwork: apri l'URL direttamente. "
        "GitHub: usa 'setup <ID>' per preparare il workspace.[/dim]"
    )


@app.command()
def run(
    limit: int = typer.Option(20, help="Max bounties to display"),
):
    """Run the full BountyBrain pipeline: collect, extract, rank, display."""
    asyncio.run(_run_pipeline(limit))


@app.command()
def dashboard():
    """Start the web dashboard on http://localhost:8080."""
    import uvicorn
    console.print("[bold green]Starting BountyBrain dashboard on http://localhost:8080[/bold green]")
    uvicorn.run(
        "bountybrain.dashboard.app:app",
        host="0.0.0.0",
        port=8080,
        reload=False,
    )


@app.command()
def setup(
    bounty_id: str = typer.Argument(..., help="Bounty ID to set up workspace for"),
):
    """Clone repo, set up dev environment, and generate Claude context files."""
    from datetime import datetime
    from .knowledge.knowledge_base import KnowledgeBase
    from .knowledge.similarity_engine import SimilarityEngine
    from .environment.environment_builder import EnvironmentBuilder
    from .context.context_builder import ContextBuilder
    from .core.models import Bounty, BountyFeatures, RankingResult, Platform, TaskType, OutcomeStatus

    kb = KnowledgeBase()
    records = kb.load_bounties()
    record = next((b for b in records if b.get("bounty_id") == bounty_id), None)
    if not record:
        console.print(f"[red]Bounty {bounty_id} non trovata nel knowledge base.[/red]")
        console.print("[yellow]Assicurati di aver eseguito 'run' prima di 'setup'.[/yellow]")
        raise typer.Exit(1)

    # Ricostruisce Bounty dal record salvato
    feat_data = record.get("features") or {}
    rank_data = record.get("ranking") or {}

    now = datetime.utcnow()
    bounty = Bounty(
        id=record["bounty_id"],
        platform=Platform(record.get("platform", "github")),
        title=record.get("title", ""),
        body=record.get("body", ""),
        url=record.get("url", ""),
        repo_url=record.get("repo_url", ""),
        repo_name=record.get("repo_name", ""),
        payout_usd=record.get("payout_usd", 0),
        created_at=now,
        updated_at=now,
    )
    if feat_data:
        try:
            bounty.features = BountyFeatures(**feat_data)
        except Exception:
            bounty.features = BountyFeatures(payout_usd=record.get("payout_usd", 0))

    ranking = None
    if rank_data:
        try:
            ranking = RankingResult(**rank_data)
        except Exception:
            pass

    # 1. Clona repo e prepara ambiente
    console.print(f"\n[bold green]Setup workspace per {bounty_id}[/bold green]")
    console.print(f"Repo: {bounty.repo_url or bounty.repo_name}")
    builder = EnvironmentBuilder()
    with console.status("[bold]Clonando repo e installando dipendenze..."):
        workspace = builder.build(bounty)

    # 2. Trova task simili dal Knowledge Base
    similarity = SimilarityEngine(kb)
    similar = similarity.find_similar(bounty, top_k=3)

    # 3. Genera file di contesto per Claude Code
    if ranking:
        ctx_builder = ContextBuilder()
        ctx_builder.build(bounty, ranking, workspace, similar_tasks=similar)
        console.print("[green]✓ CLAUDE.md, TASK.md, FIRST_STEPS.md generati[/green]")

    repo_dir = workspace / "repo"
    console.print(f"\n[bold]Workspace pronto:[/bold] {workspace}")
    console.print(f"\n[bold cyan]Prossimi passi:[/bold cyan]")
    console.print(f"  1. [white]cd {repo_dir}[/white]")
    console.print(f"  2. [white]claude[/white]   ← apre Claude Code con CLAUDE.md già nel contesto")
    console.print(f"  3. Risolvi il task, apri la PR")
    console.print(f"  4. [white]python -m bountybrain.main log-outcome {bounty_id} --status merged --payout {int(bounty.payout_usd)} --hours 1.5[/white]")


@app.command()
def score(
    text: str = typer.Argument(..., help="Descrizione job (testo incollato) o URL"),
    budget: float = typer.Option(0.0, "--budget", "-b", help="Budget dichiarato in USD"),
):
    """Analizza una singola offerta di lavoro incollata da qualsiasi piattaforma."""
    import asyncio as _asyncio
    from datetime import datetime, timezone
    from .core.models import Bounty, BountyFeatures, Platform
    from .extractor.feature_extractor import FeatureExtractor
    from .ranking.scorers.phase0_scorer import Phase0Scorer

    async def _score():
        # Costruisce una Bounty sintetica dal testo
        now = datetime.now(timezone.utc)
        payout = budget or _extract_budget_from_text(text)
        b = Bounty(
            id="manual_score",
            platform=Platform.UPWORK,
            title=text.strip().split("\n")[0][:80],
            body=text,
            url="",
            repo_url="",
            repo_name="",
            payout_usd=payout,
            created_at=now,
            updated_at=now,
        )
        ex = FeatureExtractor()
        b.features = await ex.extract(b)
        # Override: job appena trovato = 0h di età
        b.features.upwork_posted_hours_ago = 0.0
        b.features.issue_age_days = 0.0

        scorer = Phase0Scorer()
        result = scorer.score(b.features)

        f = b.features
        is_upwork = True
        net = payout * 0.80 if payout else 0
        p_win = result.p_maintainer_accepts

        console.print(f"\n[bold]Analisi job[/bold]")
        console.print(f"  Task type    : [cyan]{f.task_type.value}[/cyan]")
        console.print(f"  Budget netto : [green]${net:.0f}[/green] (dopo fee 20%)")
        console.print(f"  P(completare): [yellow]{result.p_correct:.0%}[/yellow]")
        console.print(f"  P(vincere)   : [yellow]{p_win:.0%}[/yellow] (job appena trovato)")
        console.print(f"  Ore stimate  : [white]{result.human_hours_predicted:.1f}h[/white]")
        console.print(f"  [bold green]EPHH = ${result.ephh:.1f}/h[/bold green]")

        if result.ephh >= 30:
            console.print("\n[bold green]✓ APPLICA — ottima opportunità[/bold green]")
        elif result.ephh >= 15:
            console.print("\n[yellow]~ CONSIDERA — EPHH accettabile[/yellow]")
        else:
            console.print("\n[red]✗ SALTA — troppo basso per il tempo richiesto[/red]")

        if not budget and payout == 0:
            console.print("\n[dim]Suggerimento: aggiungi --budget N per una stima precisa[/dim]")

    _asyncio.run(_score())


def _extract_budget_from_text(text: str) -> float:
    import re
    patterns = [
        r"\$\s*([\d,]+(?:\.\d+)?)\s*[-–]\s*\$?\s*([\d,]+)",  # range $50-$200
        r"budget[:\s]+\$\s*([\d,]+)",
        r"\$\s*([\d,]+(?:\.\d+)?)\b",
    ]
    for pat in patterns:
        m = re.search(pat, text[:1000], re.IGNORECASE)
        if m:
            try:
                if m.lastindex == 2:
                    return (float(m.group(1).replace(",", "")) + float(m.group(2).replace(",", ""))) / 2
                return float(m.group(1).replace(",", ""))
            except Exception:
                pass
    return 0.0


@app.command()
def log_outcome(
    bounty_id: str = typer.Argument(..., help="Bounty ID"),
    status: str = typer.Option("merged", help="merged|rejected|abandoned"),
    payout: float = typer.Option(0.0, help="Payout received (USD)"),
    hours: float = typer.Option(0.0, help="Actual human hours spent"),
    prompts: int = typer.Option(0, help="Number of Claude prompts used"),
):
    """Log a task outcome from the CLI."""
    from .core.models import OutcomeStatus, TaskOutcome
    from .knowledge.knowledge_base import KnowledgeBase

    kb = KnowledgeBase()
    ephh = 0.0
    if hours > 0 and payout > 0:
        ephh = (payout - 0.5 - 0.625) / hours

    outcome = TaskOutcome(
        bounty_id=bounty_id,
        status=OutcomeStatus(status),
        payout_received=payout,
        human_hours_actual=hours,
        n_prompts=prompts,
        ephh_actual=ephh,
    )
    kb.record_outcome(outcome)
    console.print(
        f"[green]Outcome logged:[/green] {bounty_id} → {status} | "
        f"${payout:.0f} | {hours:.1f}h | EPHH=${ephh:.1f}/h"
    )


if __name__ == "__main__":
    app()

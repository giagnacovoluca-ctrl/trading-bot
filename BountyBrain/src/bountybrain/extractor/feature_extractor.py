"""Deterministic feature extractor — uses GitHub API only, no LLM calls."""
from __future__ import annotations

import re
from datetime import datetime, timezone

import httpx
from loguru import logger

from ..config.settings import get_settings
from ..core.interfaces import FeatureExtractorBase
from ..core.models import Bounty, BountyFeatures, Platform, TaskType

TASK_TYPE_PATTERNS: dict[TaskType, list[str]] = {
    # Higher-specificity patterns first to avoid being shadowed by generic "fix"
    TaskType.FIX_FAILING_TEST: [r"failing test", r"test.*fail", r"fix.*test"],
    TaskType.SECURITY: [r"security", r"\bvuln\b", r"\bcve\b", r"\bxss\b", r"injection"],
    TaskType.PERFORMANCE: [r"\bperf\b", r"\bslow\b", r"optim", r"\bspeed\b"],
    TaskType.FIX_BUG: [r"\bbug\b", r"\bfix\b", r"broken", r"\berror\b", r"\bcrash\b"],
    TaskType.ADD_FEATURE: [r"\badd\b", r"implement", r"\bfeature\b", r"\bsupport\b"],
    TaskType.REFACTOR: [r"refactor", r"cleanup", r"clean.?up", r"improve"],
    TaskType.DOCUMENTATION: [r"\bdoc\b", r"\bdocs\b", r"readme", r"\bcomment\b"],
}


class FeatureExtractor(FeatureExtractorBase):
    def __init__(self):
        settings = get_settings()
        self._headers = {
            "Authorization": f"Bearer {settings.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._has_token = bool(settings.github_token)

    async def extract(self, bounty: Bounty) -> BountyFeatures:
        features = BountyFeatures(payout_usd=bounty.payout_usd)

        now = datetime.now(timezone.utc)
        created = bounty.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)

        age = now - created
        features.issue_age_days = age.total_seconds() / 86400
        features.issue_has_reproduction_steps = self._has_repro(bounty.body)
        features.issue_has_acceptance_criteria = self._has_acceptance_criteria(bounty.body)
        features.issue_body_length = len(bounty.body)
        features.task_type = self._classify_task_type(bounty.title + " " + bounty.body)

        if bounty.platform == Platform.UPWORK:
            self._enrich_upwork_features(features, bounty)
        elif self._has_token and bounty.repo_name and "/" in bounty.repo_name:
            await self._enrich_repo_features(features, bounty.repo_name)
            issue_number = self._extract_issue_number(bounty.url)
            if issue_number:
                await self._enrich_competition_features(
                    features, bounty.repo_name, issue_number, bounty.body
                )

        return features

    def _enrich_upwork_features(self, features: BountyFeatures, bounty: Bounty) -> None:
        body = bounty.body
        # Job type
        if re.search(r"hourly", body[:300], re.IGNORECASE):
            features.upwork_job_type = "hourly"
        else:
            features.upwork_job_type = "fixed"
        # Hours since posted
        features.upwork_posted_hours_ago = features.issue_age_days * 24
        # Experience level
        if re.search(r"\bentry.level\b|\bbeginner\b", body, re.IGNORECASE):
            features.upwork_experience_level = "entry"
        elif re.search(r"\bexpert\b|\bsenior\b", body, re.IGNORECASE):
            features.upwork_experience_level = "expert"
        else:
            features.upwork_experience_level = "intermediate"
        # Skills from description (after "Skills:" label)
        m = re.search(r"Skills?[:\s]+([^\n<]{5,200})", body, re.IGNORECASE)
        if m:
            raw = m.group(1)
            features.upwork_skills = [s.strip() for s in re.split(r"[,;]", raw) if s.strip()]
        # Treat experience level as proxy for task complexity
        if features.upwork_experience_level == "entry":
            features.issue_has_acceptance_criteria = True  # entry jobs tend to be well-specified

    async def _enrich_repo_features(self, features: BountyFeatures, repo_name: str) -> None:
        async with httpx.AsyncClient(headers=self._headers, timeout=20) as client:
            try:
                repo = await self._get_json(client, f"https://api.github.com/repos/{repo_name}")
                features.repo_stars = repo.get("stargazers_count", 0)
                features.repo_forks = repo.get("forks_count", 0)
                features.repo_size_kb = repo.get("size", 0)
                features.repo_language = repo.get("language", "") or ""
                features.repo_open_issues_count = repo.get("open_issues_count", 0)
                # Check for CONTRIBUTING.md
                features.repo_has_contributing = await self._file_exists(
                    client, repo_name, "CONTRIBUTING.md"
                )
                # Check root files for docker/ci/devcontainer
                features.repo_has_ci, features.repo_has_docker, features.repo_has_devcontainer = (
                    await self._check_repo_files(client, repo_name)
                )
                features.repo_contributor_count = await self._count_contributors(client, repo_name)
                features.n_open_prs = await self._count_open_prs(client, repo_name)
                features.maintainer_response_p50_days = await self._calc_response_p50(
                    client, repo_name
                )
            except Exception as e:
                logger.debug(f"Partial enrichment for {repo_name}: {e}")

    async def _get_json(self, client: httpx.AsyncClient, url: str) -> dict:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()

    async def _file_exists(self, client: httpx.AsyncClient, repo_name: str, filename: str) -> bool:
        try:
            resp = await client.get(
                f"https://api.github.com/repos/{repo_name}/contents/{filename}"
            )
            return resp.status_code == 200
        except Exception:
            return False

    async def _check_repo_files(
        self, client: httpx.AsyncClient, repo_name: str
    ) -> tuple[bool, bool, bool]:
        has_ci = has_docker = has_devcontainer = False
        try:
            tree = await self._get_json(
                client, f"https://api.github.com/repos/{repo_name}/contents"
            )
            if isinstance(tree, list):
                names = {f["name"] for f in tree if isinstance(f, dict)}
                has_docker = "Dockerfile" in names or "docker-compose.yml" in names
                has_devcontainer = ".devcontainer" in names
            # CI check: .github/workflows directory
            try:
                resp = await client.get(
                    f"https://api.github.com/repos/{repo_name}/contents/.github/workflows"
                )
                has_ci = resp.status_code == 200
            except Exception:
                pass
        except Exception:
            pass
        return has_ci, has_docker, has_devcontainer

    async def _count_contributors(self, client: httpx.AsyncClient, repo_name: str) -> int:
        try:
            resp = await client.get(
                f"https://api.github.com/repos/{repo_name}/contributors",
                params={"per_page": 1, "anon": "false"},
            )
            link = resp.headers.get("Link", "")
            if 'rel="last"' in link:
                m = re.search(r"page=(\d+)>; rel=\"last\"", link)
                return int(m.group(1)) if m else 1
            data = resp.json()
            return len(data) if isinstance(data, list) else 0
        except Exception:
            return 0

    async def _count_open_prs(self, client: httpx.AsyncClient, repo_name: str) -> int:
        try:
            resp = await client.get(
                f"https://api.github.com/repos/{repo_name}/pulls",
                params={"state": "open", "per_page": 1},
            )
            link = resp.headers.get("Link", "")
            if 'rel="last"' in link:
                m = re.search(r"page=(\d+)>; rel=\"last\"", link)
                return int(m.group(1)) if m else 1
            data = resp.json()
            return len(data) if isinstance(data, list) else 0
        except Exception:
            return 0

    async def _calc_response_p50(self, client: httpx.AsyncClient, repo_name: str) -> float:
        """Estimate maintainer response time p50 from recent closed issues."""
        try:
            issues = await self._get_json(
                client,
                f"https://api.github.com/repos/{repo_name}/issues",
            )
            if not isinstance(issues, list):
                return 7.0
            deltas: list[float] = []
            for i in issues[:20]:
                if i.get("state") == "closed" and i.get("comments", 0) > 0:
                    c = datetime.fromisoformat(i["created_at"].replace("Z", "+00:00"))
                    u = datetime.fromisoformat(i["updated_at"].replace("Z", "+00:00"))
                    deltas.append((u - c).total_seconds() / 86400)
            if deltas:
                deltas.sort()
                return deltas[len(deltas) // 2]
        except Exception:
            pass
        return 7.0  # default

    async def _enrich_competition_features(
        self, features: BountyFeatures, repo_name: str, issue_number: int, issue_body: str
    ) -> None:
        """Conta PR per questa issue con UNA sola chiamata Search API (rate limit: 30/min)."""
        async with httpx.AsyncClient(headers=self._headers, timeout=20) as client:
            try:
                # Una chiamata sola — poi conto open/merged dai campi dei risultati
                query = f"is:pr repo:{repo_name} #{issue_number}"
                resp = await client.get(
                    "https://api.github.com/search/issues",
                    params={"q": query, "per_page": 10},
                )
                remaining = int(resp.headers.get("x-ratelimit-remaining", 99))
                if remaining < 3:
                    # Rate limit quasi esaurito: log warning ma non bloccare
                    logger.warning(f"Search rate limit bassa: {remaining} rimaste")

                if resp.status_code == 200:
                    data = resp.json()
                    items = data.get("items", [])
                    total = data.get("total_count", 0)
                    # Conta open e merged dai primi 10 risultati (campione)
                    n_open = sum(1 for i in items if i.get("state") == "open")
                    n_merged = sum(1 for i in items if i.get("pull_request", {}).get("merged_at"))
                    n_closed = sum(1 for i in items if i.get("state") == "closed" and not i.get("pull_request", {}).get("merged_at"))
                    # Se total > 10, scala proporzionalmente
                    if total > len(items) and items:
                        ratio = total / len(items)
                        n_open = int(n_open * ratio)
                        n_closed = int(n_closed * ratio)
                    features.n_issue_prs_open = n_open
                    features.n_issue_prs_closed = n_closed
                    features.has_merged_pr = n_merged > 0
                elif resp.status_code in (403, 429):
                    logger.warning(f"Search API rate limit hit for {repo_name}#{issue_number}: {resp.status_code}")

                # Conta commenti "/attempt" — usa Issues API (no search quota)
                features.n_attempt_comments = await self._count_attempt_comments(
                    client, repo_name, issue_number
                )
            except Exception as e:
                logger.debug(f"Competition enrichment failed for {repo_name}#{issue_number}: {e}")

    async def _count_attempt_comments(
        self, client: httpx.AsyncClient, repo_name: str, issue_number: int
    ) -> int:
        """Conta commenti '/attempt' nella issue (segnale di bot farm)."""
        try:
            resp = await client.get(
                f"https://api.github.com/repos/{repo_name}/issues/{issue_number}/comments",
                params={"per_page": 100},
            )
            if resp.status_code == 200:
                comments = resp.json()
                return sum(
                    1 for c in comments
                    if "/attempt" in (c.get("body") or "").lower()
                )
        except Exception:
            pass
        return 0

    def _extract_issue_number(self, url: str) -> int | None:
        """Estrae il numero di issue dall'URL GitHub."""
        m = re.search(r"/issues/(\d+)", url or "")
        return int(m.group(1)) if m else None

    def _has_repro(self, text: str) -> bool:
        patterns = ["steps to reproduce", "to reproduce", "repro:", "reproduction", "```"]
        return any(p in text.lower() for p in patterns)

    def _has_acceptance_criteria(self, text: str) -> bool:
        patterns = [
            "acceptance criteria",
            "expected behavior",
            "expected output",
            "should:",
            "must:",
            "## expected",
        ]
        return any(p in text.lower() for p in patterns)

    def _extract_payout(self, text: str) -> float:
        """Extract USD payout from issue body using regex."""
        patterns = [
            r"\$\s*(\d+(?:,\d{3})*(?:\.\d{2})?)",
            r"(\d+(?:,\d{3})*(?:\.\d{2})?)\s*USD",
            r"bounty[:\s]+\$?\s*(\d+)",
        ]
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                return float(m.group(1).replace(",", ""))
        return 0.0

    def _classify_task_type(self, text: str) -> TaskType:
        text_lower = text.lower()
        for task_type, patterns in TASK_TYPE_PATTERNS.items():
            if any(re.search(p, text_lower) for p in patterns):
                return task_type
        return TaskType.UNKNOWN

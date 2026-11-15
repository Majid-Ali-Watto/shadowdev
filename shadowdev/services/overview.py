"""The developer's Overview: gathers what the Overview tab shows from the runs, the PRs watched,
what is held for their working hours, and their Claude accounts' usage."""
import time

from ..automation import limits as claude_limits
from ..automation.agent import claude_config_dir
from ..config import Config
from ..models.overview import WEEK, ClaudeUsage, Held, Overview
from ..models.user import User
from ..repositories.held import HeldRepository
from ..repositories.projects import ProjectRepository
from ..repositories.reviews import ReviewRepository
from ..repositories.runs import RunRepository


class OverviewService:
    def __init__(self, cfg: Config, runs: RunRepository, reviews: ReviewRepository, held: HeldRepository,
                 projects: ProjectRepository):
        self.cfg, self.runs, self.reviews, self.held, self.projects = cfg, runs, reviews, held, projects

    def of(self, user: User, now: float | None = None) -> Overview:
        now = now or time.time()
        since = now - WEEK
        return Overview(
            needs=self.runs.needing(user.id, since), active=self.runs.active(user.id),
            at_once=user.limits.max_parallel_runs, totals=self.runs.totals(user.id, since),
            prs=self.reviews.prs_of(user.id), claude=self._claude(user, now), held=self._held(user, now))

    def _claude(self, user: User, now: float) -> list[ClaudeUsage]:
        usage = claude_limits.USAGE
        return [ClaudeUsage(c.label, usage.windows(key, now), usage.held(key, now) is not None)
                for c in user.claude_accounts if (key := str(claude_config_dir(self.cfg, c)))]

    def _held(self, user: User, now: float) -> list[Held]:
        """Per project, what waits and when the developer's hours there begin, the soonest first."""
        by_project: dict[int, list] = {}
        for row in self.held.of_user(user.id):
            by_project.setdefault(row["project_id"], []).append(row)
        found = []
        for project_id, rows in by_project.items():
            project, subscription = self.projects.get(project_id), user.subscriptions.get(project_id)
            if not project or not subscription:
                continue
            found.append(Held(project.name, subscription.settings.work_starts(now),
                              tuple(r["branch"] for r in rows if r["kind"] == "prs"),
                              sum(r["kind"] == "slack" for r in rows),
                              sum(r["kind"] in ("jira", "github") for r in rows)))
        return sorted(found, key=lambda h: h.starts)

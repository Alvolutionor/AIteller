# src/processor/scorer.py
"""Source-aware scoring system.

Each source type has its own engagement normalization and quality signals.
Core focus: AI practical engineering content.
"""
import math
import re
import logging
from datetime import datetime, timezone
from src.collectors.base import RawItem
from src.prompts.filter import SCORE_WEIGHTS, SOURCE_CREDIBILITY_DEFAULTS

logger = logging.getLogger(__name__)

_known_experts: dict[str, float] | None = None


def _load_known_experts() -> dict[str, float]:
    global _known_experts
    if _known_experts is not None:
        return _known_experts
    _known_experts = {}
    try:
        import yaml, os
        path = os.path.join(os.path.dirname(__file__), "..", "..", "config", "known_experts.yaml")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            for expert in data.get("experts", []):
                score = expert.get("score", 0.8)
                for alias in expert.get("aliases", []):
                    _known_experts[alias.lower()] = score
    except Exception as e:
        logger.warning("Failed to load known_experts.yaml: %s", e)
    return _known_experts


class DeterministicScorer:
    """Source-aware deterministic scoring."""

    def score_all(self, item: RawItem) -> dict[str, float]:
        return {
            "engagement": self.engagement(item),
            "recency": self.recency(item),
            "code_evidence": self.code_evidence(item),
            "author_credibility": self.author_credibility(item),
            "cross_source": 0.0,
            "discussion_heat": self.discussion_heat(item),
        }

    # ------------------------------------------------------------------
    # Engagement — normalized per source
    # ------------------------------------------------------------------

    def engagement(self, item: RawItem) -> float:
        source = item.source
        m = item.metadata

        if source == "github_trending":
            return self._engagement_github(m)
        elif source == "reddit":
            return self._engagement_reddit(m)
        elif source in ("hackernews", "lobsters"):
            return self._engagement_hn(m)
        elif source == "twitter":
            return self._engagement_twitter(m)
        elif source == "youtube":
            return self._engagement_youtube(m)
        elif source == "bilibili":
            return self._engagement_bilibili(m)
        elif source == "hf_papers":
            return self._engagement_hf(m)
        elif source == "arxiv":
            return self._engagement_arxiv(m)
        else:
            return self._engagement_generic(m)

    def _engagement_github(self, m: dict) -> float:
        stars = m.get("stars") or 0
        today_stars = m.get("today_stars") or 0
        # GitHub: star velocity matters more than total stars
        # 10 today_stars = very active, 100 = viral
        velocity_score = min(1.0, math.log10(max(1, today_stars + 1)) / 2.0)
        total_score = min(1.0, math.log10(max(1, stars)) / 4.5)
        return min(1.0, velocity_score * 0.55 + total_score * 0.45)

    def _engagement_reddit(self, m: dict) -> float:
        score = m.get("score") or 0
        comments = m.get("num_comments") or 0
        # Reddit: 50 upvotes = decent, 500 = hot, 5000 = viral
        up_score = min(1.0, math.log10(max(1, score)) / 3.5)
        comment_score = min(0.5, math.log10(max(1, comments)) / 3.0)
        return min(1.0, up_score * 0.7 + comment_score * 0.3)

    def _engagement_hn(self, m: dict) -> float:
        points = m.get("points") or m.get("score") or 0
        comments = m.get("num_comments") or m.get("comments") or m.get("descendants") or 0
        # HN: 100 points = front page, 500 = very popular
        pts_score = min(1.0, math.log10(max(1, points)) / 2.7)
        comment_score = min(0.5, math.log10(max(1, comments)) / 2.5)
        return min(1.0, pts_score * 0.7 + comment_score * 0.3)

    def _engagement_twitter(self, m: dict) -> float:
        likes = m.get("likes") or 0
        retweets = m.get("retweets") or 0
        views = m.get("views") or 0
        # Twitter: 50 likes = decent, 500 = popular
        like_score = min(1.0, math.log10(max(1, likes)) / 2.7)
        rt_score = min(0.5, math.log10(max(1, retweets)) / 2.5)
        view_score = min(0.3, math.log10(max(1, views)) / 5.0)
        return min(1.0, like_score * 0.5 + rt_score * 0.3 + view_score * 0.2)

    def _engagement_youtube(self, m: dict) -> float:
        views = m.get("views") or m.get("view_count") or 0
        likes = m.get("likes") or m.get("like_count") or 0
        duration = m.get("duration") or 0
        # YouTube: 10K views = decent, 100K = popular
        view_score = min(1.0, math.log10(max(1, views)) / 5.0)
        like_score = min(0.5, math.log10(max(1, likes)) / 3.5)
        # Bonus for substantial content (>5min), penalty for <1min shorts
        duration_bonus = 0.0
        if duration and duration > 300:
            duration_bonus = 0.2
        elif duration and duration < 60:
            duration_bonus = -0.1
        return min(1.0, max(0.0, view_score * 0.5 + like_score * 0.3 + 0.2 + duration_bonus))

    def _engagement_bilibili(self, m: dict) -> float:
        views = m.get("views") or m.get("play") or 0
        likes = m.get("likes") or 0
        # Bilibili: 1K views = decent for niche, 10K = popular
        view_score = min(1.0, math.log10(max(1, views)) / 4.0)
        like_score = min(0.5, math.log10(max(1, likes)) / 3.0)
        return min(1.0, view_score * 0.6 + like_score * 0.4)

    def _engagement_hf(self, m: dict) -> float:
        upvotes = m.get("upvotes") or 0
        # HF Papers: 10 upvotes = notable, 50 = trending
        return min(1.0, math.log10(max(1, upvotes + 1)) / 1.7)

    def _engagement_arxiv(self, m: dict) -> float:
        # arXiv has no engagement metrics in the API
        # Default moderate score — rely on other dimensions
        return 0.3

    def _engagement_generic(self, m: dict) -> float:
        raw = (m.get("points") or 0) + (m.get("score") or 0) + \
              (m.get("stars") or 0) + (m.get("views") or 0) // 100
        if raw <= 0:
            return 0.0
        return min(1.0, math.log10(max(1, raw)) / 4.0)

    # ------------------------------------------------------------------
    # Recency
    # ------------------------------------------------------------------

    def recency(self, item: RawItem) -> float:
        now = datetime.now(timezone.utc)
        hours = (now - item.published_at).total_seconds() / 3600
        if hours <= 24:
            return 1.0
        elif hours <= 48:
            return 0.8
        elif hours <= 72:
            return 0.6
        elif hours <= 168:
            return 0.4
        return 0.2

    # ------------------------------------------------------------------
    # Code evidence — source-aware
    # ------------------------------------------------------------------

    def code_evidence(self, item: RawItem) -> float:
        source = item.source
        text = f"{item.title} {item.content}"
        meta = item.metadata
        score = 0.0

        # GitHub source: don't give free points for having a GitHub link
        # Instead, check for quality signals
        if source == "github_trending":
            return self._code_evidence_github(item)

        # arXiv / HF Papers: check for code availability
        if source in ("arxiv", "hf_papers"):
            return self._code_evidence_paper(item)

        # All other sources: check for links to code, code blocks, metrics
        if re.search(r"github\.com/[\w-]+/[\w-]+", text):
            score += 0.4
        link_url = str(meta.get("link_url", ""))
        if "github.com" in link_url or "gitlab.com" in link_url:
            score += 0.3
        if "```" in item.content or "`" in item.content:
            score += 0.3
        if re.search(r"\d+\.?\d*\s*(%|ms|s/it|tok/s|tokens|accuracy|F1|BLEU|latency)", text):
            score += 0.3

        return min(1.0, score)

    def _code_evidence_github(self, item: RawItem) -> float:
        """For GitHub repos: no free points for being a repo.

        GitHub repos inherently have code — scoring them on code_evidence
        is circular. Only give points for signals that go beyond "is a repo":
        benchmarks, metrics, or detailed technical writeup.
        """
        text = f"{item.title} {item.content}"
        score = 0.0

        # Only reward signals that indicate depth beyond a README
        if re.search(r"\d+\.?\d*\s*(%|ms|tok/s|x faster|accuracy)", text):
            score += 0.3
        # Detailed description (>200 chars) suggests real documentation
        if len(item.content) > 200:
            score += 0.1

        return min(1.0, score)

    def _code_evidence_paper(self, item: RawItem) -> float:
        """For papers: check if code is available."""
        text = f"{item.title} {item.content}"
        m = item.metadata
        score = 0.1  # papers always have some technical content

        if "github.com" in text or m.get("arxiv_url"):
            score += 0.4
        # Has experimental results
        if re.search(r"(experiment|benchmark|evaluat|result|table|figure)", text.lower()):
            score += 0.3
        # Practical keywords
        if re.search(r"(deploy|production|efficient|optimiz|framework|tool|pipeline)", text.lower()):
            score += 0.2

        return min(1.0, score)

    # ------------------------------------------------------------------
    # Author credibility
    # ------------------------------------------------------------------

    def author_credibility(self, item: RawItem) -> float:
        experts = _load_known_experts()
        author = (item.author or "").lower().strip()
        if author in experts:
            return experts[author]
        return SOURCE_CREDIBILITY_DEFAULTS.get(item.source, 0.4)

    # ------------------------------------------------------------------
    # Discussion heat — source-aware
    # ------------------------------------------------------------------

    def discussion_heat(self, item: RawItem) -> float:
        m = item.metadata
        source = item.source

        if source == "twitter":
            replies = m.get("replies") or 0
            retweets = m.get("retweets") or 0
            heat = replies + retweets
            # Twitter: 10 replies = decent, 100 = hot
            divisor = 2.0
        elif source == "github_trending":
            # Trending API doesn't provide forks/issues; use today_stars as buzz proxy
            heat = m.get("today_stars") or 0
            # GitHub trending: 50 today_stars = active, 500 = viral
            divisor = 2.7
        elif source == "bilibili":
            review = m.get("review") or 0
            danmaku = m.get("danmaku") or 0
            heat = review + danmaku
            # Bilibili: 5 comments = decent for niche, 50 = active
            divisor = 1.7
        elif source == "reddit":
            heat = m.get("num_comments") or 0
            # Reddit: 50 comments = decent, 500 = hot
            divisor = 2.7
        elif source in ("hackernews", "lobsters"):
            heat = m.get("num_comments") or m.get("comments") or m.get("descendants") or 0
            # HN: 10 comments = decent discussion, 100 = very active
            divisor = 2.0
        elif source == "youtube":
            heat = m.get("comment_count") or m.get("comments") or 0
            # YouTube: 20 comments = decent, 200 = active
            divisor = 2.3
        else:
            heat = m.get("num_comments") or m.get("comments") or m.get("comment_count") or 0
            divisor = 2.5

        if heat <= 0:
            return 0.0
        return min(1.0, math.log10(max(1, heat)) / divisor)


def compute_total_score(breakdown: dict[str, float]) -> float:
    """Compute weighted total score (0-10) from all dimensions."""
    total = sum(breakdown.get(k, 0.0) * w for k, w in SCORE_WEIGHTS.items())
    return round(min(10.0, max(0.0, total * 10)), 2)

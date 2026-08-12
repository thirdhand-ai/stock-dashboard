"""News sentiment - RESEARCH ONLY, deterministic and local.

Classifies headlines/summaries already stored by ingestion/finnhub_source.py
(the `news` table) using a small local keyword lexicon (research/config.py's
SentimentConfig) - no external AI/NLP API call, no network access from this
module at all. This is a coarse bag-of-words polarity heuristic, not true
financial-NLP sentiment - it is clearly a different kind of signal than the
factual fundamentals in research/fundamentals.py, and is labeled as such
everywhere it's displayed.

Never used to submit, size, or gate a trade - see trading/config.py's
RiskConfig for the actual (unrelated) risk gates used at order time.
"""
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import pandas as pd

from research.config import DEFAULT_SENTIMENT_CONFIG, SentimentConfig

LABEL_POSITIVE = "positive"
LABEL_NEUTRAL = "neutral"
LABEL_NEGATIVE = "negative"


def _contains(text: str, phrase: str) -> bool:
    """Whole-word/phrase, case-insensitive containment check."""
    pattern = r"(?<!\w)" + re.escape(phrase.lower()) + r"(?!\w)"
    return re.search(pattern, text) is not None


@dataclass
class TextClassification:
    label: str
    net_score: int
    positive_hits: List[str] = field(default_factory=list)
    negative_hits: List[str] = field(default_factory=list)
    major_positive: bool = False
    major_negative: bool = False


def classify_text(text: str, config: SentimentConfig = DEFAULT_SENTIMENT_CONFIG) -> TextClassification:
    lowered = (text or "").lower()

    positive_hits = [w for w in config.positive_words if _contains(lowered, w)]
    negative_hits = [w for w in config.negative_words if _contains(lowered, w)]
    net_score = len(positive_hits) - len(negative_hits)

    if net_score >= config.positive_threshold:
        label = LABEL_POSITIVE
    elif net_score <= config.negative_threshold:
        label = LABEL_NEGATIVE
    else:
        label = LABEL_NEUTRAL

    major_positive = any(_contains(lowered, kw) for kw in config.major_positive_keywords)
    major_negative = any(_contains(lowered, kw) for kw in config.major_negative_keywords)

    return TextClassification(
        label=label, net_score=net_score, positive_hits=positive_hits,
        negative_hits=negative_hits, major_positive=major_positive, major_negative=major_negative,
    )


@dataclass
class NewsArticleSentiment:
    headline: str
    summary: Optional[str]
    source: Optional[str]
    url: Optional[str]
    published_at: str
    label: str
    net_score: int
    major_positive: bool
    major_negative: bool


@dataclass
class NewsSentimentSummary:
    ticker: str
    ok: bool
    reason: Optional[str] = None
    articles: List[NewsArticleSentiment] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=lambda: {LABEL_POSITIVE: 0, LABEL_NEUTRAL: 0, LABEL_NEGATIVE: 0})
    news_sentiment_score: Optional[float] = None
    most_recent_at: Optional[str] = None
    major_positive_count: int = 0
    major_negative_count: int = 0


def load_recent_news(conn, ticker: str, config: SentimentConfig = DEFAULT_SENTIMENT_CONFIG, now: Optional[datetime] = None) -> pd.DataFrame:
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=config.lookback_days)).strftime("%Y-%m-%d %H:%M:%S")
    return pd.read_sql_query(
        """
        SELECT headline, summary, source, url, published_at
        FROM news
        WHERE ticker = ? AND published_at >= ?
        ORDER BY published_at DESC
        LIMIT ?
        """,
        conn,
        params=(ticker, cutoff, config.max_articles),
    )


def compute_news_sentiment_summary(
    conn, ticker: str, config: SentimentConfig = DEFAULT_SENTIMENT_CONFIG, now: Optional[datetime] = None,
) -> NewsSentimentSummary:
    df = load_recent_news(conn, ticker, config, now)
    if df.empty:
        return NewsSentimentSummary(
            ticker=ticker, ok=False,
            reason=f"no news within the last {config.lookback_days} days for this ticker",
        )

    articles: List[NewsArticleSentiment] = []
    counts = {LABEL_POSITIVE: 0, LABEL_NEUTRAL: 0, LABEL_NEGATIVE: 0}
    net_scores: List[int] = []
    major_positive_count = 0
    major_negative_count = 0

    for row in df.itertuples(index=False):
        combined_text = f"{row.headline} {row.summary or ''}"
        classification = classify_text(combined_text, config)
        counts[classification.label] += 1
        net_scores.append(classification.net_score)
        major_positive_count += int(classification.major_positive)
        major_negative_count += int(classification.major_negative)

        articles.append(NewsArticleSentiment(
            headline=row.headline, summary=row.summary, source=row.source, url=row.url,
            published_at=row.published_at, label=classification.label, net_score=classification.net_score,
            major_positive=classification.major_positive, major_negative=classification.major_negative,
        ))

    avg_net_score = sum(net_scores) / len(net_scores)
    raw_score = config.score_midpoint + avg_net_score * config.score_scale_per_net_point
    news_sentiment_score = max(0.0, min(100.0, raw_score))

    return NewsSentimentSummary(
        ticker=ticker, ok=True, articles=articles, counts=counts,
        news_sentiment_score=round(news_sentiment_score, 1),
        most_recent_at=str(df["published_at"].iloc[0]),
        major_positive_count=major_positive_count, major_negative_count=major_negative_count,
    )

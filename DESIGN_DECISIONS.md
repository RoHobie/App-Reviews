# Design Decisions & Architecture Reference

This document details the design decisions (D1–D13), algorithmic trade-offs, and data constraints governing the Review Intelligence MVP SaaS.

---

## Key Design Decisions (D1 – D13)

### D1: Stratified Random Sampling for Taxonomy Discovery
- **Decision:** The initial taxonomy discovery sample is drawn using stratified random sampling across `store × star_bucket × version`, rather than taking the "first N" reviews.
- **Bounds:** Default 10% of the cleaned dataset, clamped to `[min 150, max 300]` (if fewer than 150 cleaned reviews exist, all are used).
- **Rationale:** Taking the first N reviews introduces temporal and version bias, skewing the discovered taxonomy toward a single release window.

### D2: Review Type Gate Before Categories
- **Decision:** Every review passes through a gate type: `complaint`, `praise`, `feature_request`, `question`, or `spam_or_irrelevant`. Only complaints (and feature requests) enter category mapping.
- **Rationale:** Without a type gate, 5-star positive reviews ("Love this app!") land in the null bucket and pollute complaint metrics.

### D3: Pure Null Bucket for Vague Complaints
- **Decision:** Stored as `category_id="null"` strictly for complaints that lack actionable detail (e.g. "worst app", "doesn't work", "useless"). Never mixed with praise or spam.
- **Rationale:** Isolates vague complaints into a focused target list for product teams to solicit clarification from users.

### D4: Multi-Label Category Assignment
- **Decision:** Reviews can map to multiple categories. Outputs are stored in normalized form with one row per (review, category) assignment.
- **Rationale:** Customer reviews routinely articulate multiple distinct issues (e.g. login failure AND payment deduction).

### D5: Evolving Taxonomy via Reconciliation
- **Decision:** The classification stage can flag a `proposed_new_category` for complaints that fit no existing bucket. Stage 5 clusters these proposals via LLM, adds accepted ones, and re-classifies affected reviews. Unmatched proposals fall back to `other` (distinct from `null`).
- **Rationale:** A static initial taxonomy inevitably misses issues emerging later in the dataset.

### D6: Ordinal Scoring Over Probabilities
- **Decision:** LLMs return an integer 1–5 for relevance and 1–5 for severity. Normalized score `relevance / 5.0` is used for ranking.
- **Rationale:** Self-reported LLM confidence percentages are uncalibrated and misleading; ordinal integers provide reliable ordering.

### D7: Proof-Review Composite Ranking
- **Decision:** Reviews supporting each category are ranked via:
  $$\text{score} = 0.50 \cdot \text{relevance\_norm} + 0.20 \cdot \text{severity\_norm} + 0.15 \cdot \text{thumbs\_norm} + 0.15 \cdot \text{recency\_norm}$$
- **Rationale:** Pure LLM relevance ignores community validation (upvotes) and recency.

### D8: Hybrid Persistence Strategy
- **Decision:** SQLite with WAL mode tracks job states, stage progression checkpoints, and fast dashboard queries. Immutable CSV/JSON files serve as human-readable exports.
- **Rationale:** Raw CSVs alone cannot support asynchronous state management, while database-only models impede human auditability.

### D9: Reproducibility Contract
- **Decision:** Each run directory stores `config.json`, the unmutated `raw_reviews.csv` snapshot, intermediate stage artifacts, and a local disk cache keyed by $\text{SHA-256}(\text{model} + \text{prompt\_version} + \text{rendered\_prompt})$ with $\text{temperature}=0$. Re-running with `reuse_snapshot: true` yields byte-identical results.
- **Rationale:** App store reviews change daily and LLM outputs can drift; snapshotting + deterministic caching is the only reliable reproducibility mechanism.

### D10: Hinglish & Unicode Script Ratio Filtering
- **Decision:** Default language filter (`lenient`) measures Latin character ratio among alphabetic letters ($\ge 70\%$). English and Hinglish (Hindi in Latin script) are retained; non-Latin scripts (Devanagari, Tamil, etc.) are filtered.
- **Rationale:** Indian market reviews are predominantly Hinglish. Third-party language detectors misclassify Hinglish as non-English; script ratio prevents discarding valid complaints.

### D11: PII Redaction
- **Decision:** User IDs and usernames are omitted from LLM prompts and dashboard exports. Emails and Indian mobile numbers (`+91` / 10-digit numbers starting 6–9) are regex-replaced with `[EMAIL]` and `[PHONE]`.
- **Rationale:** Privacy preservation and token cost reduction.

### D12: India Storefront Focus (v1)
- **Decision:** Market is fixed to India (`country="in"`). Play Store scrapes with `country="in"`, `lang="en"`; App Store uses `/in/` RSS feed.
- **Rationale:** Eliminates cross-region pricing and language skew, fixing App Store limits to a predictable ~500 reviews.

### D13: Gemini via Google-GenAI SDK
- **Decision:** Google Gemini Flash models using the official `google-genai` SDK with native JSON structured output schemas (`response_schema`).
- **Rationale:** Cost-effective, large context window for batch reviews, and fast response times.

---

## Priority Score Formula (Stage 6)

Category priority ranking is computed **strictly in code** (not by LLM):
$$\text{priority} = 0.35 \cdot \text{volume\_share} + 0.25 \cdot \text{avg\_severity\_norm} + 0.20 \cdot \text{rating\_drag} + 0.10 \cdot \text{recency\_weight} + 0.10 \cdot \text{trend\_up}$$

- `volume_share`: Category review count / total complaint reviews.
- `avg_severity_norm`: Mean severity score / 5.0.
- `rating_drag`: (Overall average rating - category average rating) normalized to $[0, 1]$.
- `recency_weight`: Proportion of category reviews occurring in the most recent 30-day window.
- `trend_up`: Binary indicator (1.0 if category share increased in the recent half of the date range, else 0.0).

The LLM receives the pre-sorted list to author narrative without reordering categories.

---

## Known Store Constraints

1. **Apple App Store Public RSS Cap:** Capped at 10 pages × 50 entries = ~500 reviews per storefront. App Store cannot satisfy sample sizes above ~500 in v1.
2. **Apple RSS Developer Replies:** The public RSS feed omits developer replies. App Store reviews set `dev_reply=""` and `has_dev_reply=null`.
3. **Single Uvicorn Worker Design:** The app runs with `--workers 1` to ensure SQLite Write-Ahead Logging integrity and prevent background thread race conditions.

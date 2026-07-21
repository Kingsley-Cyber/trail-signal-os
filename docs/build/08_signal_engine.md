# 08 — Signal Engine & Opportunity Scoring

> **Status:** Design specification
> **Scope:** Deterministic scoring engine — the anti-slop core
> **Governs:** `signal_engine/`, `config/weights.yaml`
> **Consumes:** `evidence.v1` · **Produces:** `signal.v1`, `opportunity.v1`
> **LAW 1:** The LLM extracts a raw signal and later explains a score; every number between those two points is produced by deterministic, versioned code.

---

## 1. Signal Taxonomy (Precise Definitions)

Five orthogonal axes. Orthogonality matters — overlapping axes double-count and inflate scores.

| Axis | Question | Direction |
|------|----------|-----------|
| `demand` | Do people want this now? | ↑ good |
| `growth` | Is want accelerating over ~6mo? | ↑ good |
| `pain` | Are existing solutions failing people? | ↑ good (unmet need = opening) |
| `competition` | How crowded / how well-served? | ↓ good (invert to `k = 1−competition`) |
| `content` | Can this produce engaging short-form? | ↑ good |

"Opportunity" is not "high on average" — it's the right combination (high demand × low competition; high pain × low ad saturation). §7 rewards combinations explicitly; a naive weighted average would blend the signal away.

---

## 2. Source → Signal Matrix (Tiered per Doc 06)

Each cell = what raw metric the classifier extracts. Tier drives the confidence discount (§9). **Load-bearing signals must be reachable from open.**

| Signal | open sources → metric | defended → metric | hostile/ToS → metric |
|--------|----------------------|-------------------|---------------------|
| demand | Google Trends (interest index); YouTube (review-video view counts, search hits); Reddit (mention frequency) | Amazon/Walmart/eBay (review count, BSR as sales proxy) | — |
| growth | Google Trends (6mo slope); YouTube (upload-rate growth) | marketplace (review-accumulation rate) | TikTok Creative Center (trend velocity); Pinterest Trends (slope) |
| pain | Trustpilot / G2 / Capterra (complaint-theme density); Reddit (gripe frequency); YouTube ("cons" segments) | Amazon (1–3★ review mining) | — |
| competition | YouTube (content saturation); count of review sites | marketplace (listing count, seller count, incumbent rating) | Meta Ad Library (ad count); TikTok Top Ads (presence) → `ad_intensity` |
| content | YouTube (engagement vs saturation) | — | TikTok Creative Center (niche engagement); Pinterest (pin performance) |

`ad_intensity` is a named sub-component of competition and is exposed separately because §7's underserved-pain term needs it. **Note:** demand and pain are fully reachable from open sources — by design, since those are the two hard-required axes (§6).

---

## 3. Extraction Contract (the ONLY LLM Step in Scoring)

`classify.py`, role `enrich.primary`, constrained JSON:

```
evidence.v1  →  signal_raw {
    niche_id,
    signal_type,
    source,
    window,
    raw_metric: {name, value, unit, sample_n},
    evidence_ids[]
}
```

The LLM decides which axis this evidence speaks to and pulls the raw number + sample size. **It does not normalize, weight, or score.** `sample_n` is mandatory — it drives confidence (§5). Validation failure → `cp:signal:repair`.

---

## 4. Normalization — Raw → [0,1] (Deterministic, `normalize_version`)

The commensurability problem (TikTok velocity vs Amazon BSR vs Trends index are different units) is solved by rank, not absolute thresholds:

```
1. WINSORIZE   clip raw to [p5, p95] of the niche+window cohort   # kill outliers
2. RANK        s = percentile_rank(raw_clipped within cohort)     # unit-free, robust, ∈[0,1]
3. DIRECT      if metric direction is negative (e.g. listing_count for competition):
                 s = 1 − s
```

**Percentile rank within the niche cohort is deliberate:** it makes signals comparable across sources (every axis lands on the same 0–1 scale defined by that niche's own distribution) and immune to unit and outlier distortion. Absolute thresholds were rejected — they rot as markets shift and can't compare a view-count to a BSR.

`competition` aggregates its components before ranking:

```
competition_raw = wᶜ·listing_density + wᵃ·ad_intensity
```

(weights in `weights.yaml`); `ad_intensity` is also kept as its own normalized value for §7.

**Verifier `normalize_invariants`:** `0 ≤ s ≤ 1`, window set, direction applied. Deterministic code failing this is a bug → hard error, never a retry.

---

## 5. Confidence — Per Signal (Deterministic)

A signal's confidence is how much the score should trust it. Thin or stale or low-tier evidence must not swing a score:

```
confidence = w_n · sat(log1p(sample_n) / log1p(N_ref))     # more evidence → more trust
           + w_t · tier_weight                              # source reliability
           + w_r · exp(−age_days / half_life[signal_type])  # recency (§10)
        (w_n + w_t + w_r = 1; result clamped [0,1])
```

| Term | Meaning |
|------|---------|
| `tier_weight` | open 1.00 · defended 0.85 · hostile 0.50 (§9) |
| `N_ref` | reference sample size for "fully trusted" (config, per signal_type) |
| `sat(x)` | `min(x, 1)` |

**Defaults:** `w_n 0.45, w_t 0.30, w_r 0.25`. A lone Reddit comment (`sample_n=1`) and a 200-review Trustpilot theme produce different confidences on the same axis — which is the point.

---

## 6. Coverage Gate Spec

Grid `signal_type × source_tier`; a cell's value = best `signal.confidence` seen. SCORING is admitted by `coverage.py` when:

```
HARD-REQUIRED (cannot score without):
  demand @ {open|defended} ≥ min_cell_confidence
  pain   @ {open|defended} ≥ min_cell_confidence

SOFT (score-with-gaps allowed; each unfilled → coverage_gaps[] + confidence penalty):
  growth, competition, content @ any tier ≥ min_cell_confidence

min_cell_confidence = 0.40  (config)
gate passes when HARD-REQUIRED met OR dossier_deadline reached
```

**Rationale:** you cannot honestly call something an opportunity without knowing demand and pain; you can estimate with partial growth/competition/content. On deadline with unfilled hard cells, no score is emitted — the dossier reports the gap instead of guessing.

---

## 7. Scoring Model (Deterministic, `scoring_version`) — The Core

**Inputs:** normalized sub-scores `demand d, growth g, pain p, competition c, content t, ad_intensity a`, each with confidence. Four stages:

### (a) Confidence Shrink Toward Neutral

Low-evidence dimensions can't swing the score:

```
x' = 0.5 + (x − 0.5) · confidence(x)        for each of d,g,p,c,t,a
k  = 1 − c'                                   # invert competition → "uncrowded"
```

At confidence 0 a dimension contributes 0.5 (neutral); at 1 it contributes its full value. **This is the anti-slop mechanism:** a score built on thin evidence stays near neutral rather than confidently wrong.

### (b) Base = Weighted Geometric Mean

Not arithmetic — a real opportunity must be decent on every axis; geometric mean tanks if any axis is low:

```
base = d'^w_d · g'^w_g · p'^w_p · k^w_k · t'^w_t       (Σw = 1)
     = exp( Σ wᵢ·ln xᵢ )
```

### (c) Interaction Bonuses

Reward the combinations that define opportunity (capped so they tilt, never dominate):

```
demand_gap       = d' · k                # validated demand + uncrowded → the gold quadrant
underserved_pain = p' · (1 − a')        # high complaints + low ad spend → open lane

final = clamp( base · (1 + λ_gap·demand_gap + λ_pain·underserved_pain), 0, 1 )
```

### (d) Dead-Niche Gate Is Intrinsic

Not bolted on. `demand_gap = d'·k`: a dead niche (low demand, low competition) has low `d'`, so the term → 0 — no false bonus from emptiness. And base's geometric mean with a demand weight tanks on low `d'` regardless.

**Double protection:** emptiness never masquerades as opportunity because demand gates both the base and the bonus. This is the failure mode most trend tools ship with; here it's closed by construction.

### Overall Confidence

Weighted geometric mean of the required-axis confidences (thin evidence → low overall confidence), then capped by §9 if hostile-dependent:

```
opp_confidence = geomean(conf_d, conf_p, conf_g, conf_c, conf_t)   # required axes weighted
```

`opportunity.v1` stores `score=final`, `subscores={d',g',p',k,t'}`, `confidence=opp_confidence`, `coverage_gaps`, plus provenance `{scoring_version, weights_version, normalize_version, config_hash}`. **Reproducible byte-for-byte from the stored `signal.v1`s.**

---

## 8. `weights.yaml` (Versioned, Tunable)

```yaml
version: "w-2026.07.21"          # bump → new opportunity_ids, old retained, lineage.diff shows delta
axis_weights: {demand: 0.25, growth: 0.15, pain: 0.25, competition: 0.20, content: 0.15}
competition_components: {listing_density: 0.6, ad_intensity: 0.4}
interactions: {lambda_gap: 0.15, lambda_pain: 0.15}
confidence: {w_n: 0.45, w_t: 0.30, w_r: 0.25}
tier_weight: {open: 1.00, defended: 0.85, hostile: 0.50}
n_ref: {demand: 50, growth: 30, pain: 40, competition: 100, content: 60}
min_cell_confidence: 0.40
```

**Tuning discipline:** never hand-edit a score; edit weights, bump the version, re-score (cheap — §7 runs over existing signals), and read `lineage.diff` to see exactly what the change did. **History is never destroyed.**

---

## 9. Tier Discount + Hostile-Dependence Cap

Tier weight already discounts hostile-source confidence (§5). The hard guard:

```
if removing all hostile-tier signals drops any HARD-REQUIRED cell below min_cell_confidence:
    opportunity.hostile_dependent = true
    opp_confidence = min(opp_confidence, 0.50)
```

So a score can use TikTok/Meta signals but can never depend on them — because those are exactly the sources the degradation ladder loses by week two (doc 06). Demand and pain being open-reachable (§2) means well-formed dossiers are rarely hostile-dependent.

---

## 10. Half-Lives & Expiry

Decay rates differ by axis — a complaint outlives a viral moment:

```yaml
half_life_days: {demand: 60, growth: 30, pain: 180, competition: 45, content: 21}
```

Feeds `signal.expires_at` and the recency term (§5). `content` decays fastest (what's viral this month won't be next); `pain` slowest (a leaking product leaks for a year). Expired signals drop from scoring and trigger re-collection; dedup makes unchanged pages free.

---

## 11. EXPLAIN Contract (LLM — Explains, Never Scores) — LAW 1

`explain.py`, role `enrich.primary` (or `operator.interactive`), input = the finished `opportunity.v1` + its evidence:

```
→ explanation {text, cited_record_ids[]}
```

**Rules:**

- Cites the `evidence_ids` behind each claim
- States the confidence and why (e.g. "content leans on a discounted TikTok source")
- Surfaces the top pain themes verbatim (≤25-word quotes)
- **Receives the score as input and may not change it** — the write guard (test 1) rejects any explanation task that mutates `score`/`subscores`
- The prose lives in a separate field with separate provenance from the number

---

## 12. Worked Example (Proof the Combination Logic Holds)

**Niche:** "camping", **candidate:** "portable camping fan". Normalized sub-scores (raw → §4) and confidences (§5):

| axis | s (pctile) | conf | s' after shrink |
|------|-----------|------|-----------------|
| demand | 0.72 | 0.80 | 0.676 |
| growth | 0.65 | 0.55 | 0.583 |
| pain | 0.80 | 0.75 | 0.725 |
| competition | 0.40 | 0.70 | c'=0.430 → k=0.570 |
| content | 0.68 | 0.50 (TikTok, discounted) | 0.590 |
| ad_intensity | 0.35 | — | a'=0.35 → (1−a')=0.65 |

```
base = 0.676^.25 · 0.583^.15 · 0.725^.25 · 0.570^.20 · 0.590^.15 = 0.637
demand_gap       = 0.676 · 0.570 = 0.385
underserved_pain = 0.725 · 0.65  = 0.471
final = 0.637 · (1 + 0.15·0.385 + 0.15·0.471) = 0.637 · 1.128 = 0.719
opp_confidence = geomean(0.80,0.55,0.75,0.70,0.50) = 0.65
```

**Result:** opportunity 0.72, confidence 0.65. Not hostile-dependent (demand+pain from open sources).

**EXPLAIN** (number unchanged): "Strong complaint density (0.80 pctile, 200+ reviews citing battery life and noise) meets solid, mildly accelerating demand in a moderately uncrowded field with low ad spend — both interaction terms lifted the base. Confidence moderate: content potential leans on a discounted TikTok source. Top pains: [ev_1042, ev_1108, ev_1155]."

### Counter-Check (Dead Niche)

Same candidate but demand pctile 0.15 @ conf 0.80 → `d'=0.22`. Even with competition low (`k=0.70`): `demand_gap = 0.22·0.70 = 0.154` (bonus collapses) and base tanks on `0.22^.25`. `final ≈ 0.34`.

**Emptiness scored low — the gate holds without any special-case code.**

---

## 13. Out of Scope / Discipline

**Not here:** the raw scrapers (`workers/`, doc 06), the LLM extraction prompts' internals (`prompts/`), the graph wiring (doc 07). This doc is the deterministic brain only.

**Two standing rules:**

1. Any proposal to let an LLM output a score is rejected — it breaks LAW 1 and the auditability that makes this defensible.
2. Any new axis must be orthogonal to the five, or it double-counts. **Add sources to the matrix freely; add axes almost never.**

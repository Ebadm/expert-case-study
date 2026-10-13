# External Sourcing Agent: Implementation Plan

*Written before any code, to fix the scope and the design rules for a two-evening build.
How the system turned out is in [ARCHITECTURE.md](ARCHITECTURE.md). The last section lists
where the build departed from this plan and why.*

## 1. Goal

Build a Python agent that takes a project brief and returns, per expert group, up to 20
experts with name, relevant position (title and company), a profile link and an
evidence-backed reason. People from excluded companies must not appear.

**Target: the Senior level on the Tungsten Automation brief.**
- all three groups (former employees, customers, competitors), each with its own search
  approach;
- two search providers, with duplicates resolved across them;
- results ranked by fit, with evidence behind each reason;
- a hand-labelled sample with measured precision;
- time and API cost recorded per run.

**Generalisation test: the Polish Rail Freight brief** must run through the same CLI with
no pipeline changes. If it needs brief-specific code, the abstraction is wrong.

**Success means:**
- every listed person exists and fits the brief;
- exclusions hold, including former names;
- nobody appears twice;
- every reason can be traced to a source;
- precision is measured, not asserted;
- the cost of a run is known.

## 2. Scope

| In | Out (and why) |
|---|---|
| CLI, one command per brief | Frontend or dashboard: not graded |
| Structured brief spec, reviewable between stages | Vector or production database: files are enough at this scale |
| Two search providers through one interface | Fine-tuning, multi-agent frameworks: no evidence they're needed |
| Deterministic eligibility, deduplication, ranking | LinkedIn scraping, or bypassing logins and paywalls: terms of service |
| Precision on a labelled sample; cost per run | Recall claims: there is no ground-truth list |
| Tests for the rules that matter | Staff-level extras: only if time remains |

If time runs short, cut query volume and polish, never the eligibility checks.

## 3. Approach

```
brief.md ─► 1. parse brief ─► brief_spec.json          (LLM extracts; code validates)
            2. generate queries per group ─► queries.json
            3. search: Brave + Exa ─► search_results.json
            4. extract candidates + evidence ─► candidates.json
            5. deduplicate into identities
            6. verify eligibility (code only)
            7. rank and select ─► final_results.json / .csv / .md
            8. evaluate a labelled sample ─► evaluation.json
            every stage ─► run_metadata.json (time, calls, tokens, cost)
```

**Design rules**
1. **Brief-specific rules live in the spec, not in code.** The spec holds groups,
   organisations and aliases, exclusions, date windows, hard requirements and
   preferences. The pipeline interprets them generically.
2. **Code enforces rules; the LLM only interprets.** The LLM parses the brief, writes
   search terms, reads unstructured pages and helps with ambiguous cases. Date windows,
   exclusions, merges and output limits are deterministic and tested. Ranking never
   overrides a failed rule.
3. **Unknown stays unknown.** No invented dates, titles or quotes. Every extracted claim
   keeps its source URL and text. A paraphrase is never shown as a quote, and a search
   snippet supports discovery, not eligibility.
4. **Every stage has a file as input and output.** Failures can be inspected, and a run
   can restart from a reviewed artifact without re-paying for earlier stages.
5. **Measure everything per run:** stage timings, requests per provider, tokens, cost.
   Exact where the provider reports it, labelled as an estimate otherwise.

## 4. Search strategy per group

The groups live in different places on the web, so each gets its own approach.

| Group | Where they are found | Query approach |
|---|---|---|
| Tungsten former employees | LinkedIn profiles listing Tungsten/Kofax as a past employer | Keyword X-ray on both names, excluding current staff; semantic Exa people search with the departure window in the text |
| Tungsten customers | Vendor case studies, press releases, customer interviews | Find the customer organisations first, then the people who used the product there. Presence at a customer is not proof of use |
| Tungsten competitors | LinkedIn and company pages of ABBYY, Hyland, OpenText, IBM, Microsoft | Per-company role searches (product, GTM, partnerships). At IBM and Microsoft, require relevance to document processing |
| Polish infrastructure / regulation | PKP PLK, UTK staff; network statements, regulator documents | Polish and English queries on train paths, access charges, timetabling |
| Polish freight operators | Private operators active in Poland | Discover the operators, then former operations, commercial and fleet leads |
| Polish terminals / intermodal | Terminal operators, intermodal directories | Discover the terminals, then the people running them |

- **Brave** for keyword precision: exact names, titles, `site:` X-rays.
- **Exa** for semantic discovery, where the wording differs from the job title.
- Results are normalised into one shape, URLs canonicalised, and provenance kept: which
  provider and query found each source. The same page from two providers is one source,
  not two.
- Queries are bounded and logged per group; near-duplicates are dropped.

## 5. Data model (sketch)

```python
BriefSpec:    brief_id, target + aliases, excluded organisations (with scope: current/former),
              groups[], run_date, source text + hash
ExpertGroup:  id, description, eligible organisations, role and topic keywords,
              hard constraints, preferences, departure window, query languages
Candidate:    name, profile_url, positions[] (employer, title, start, end, is_current),
              evidence[], source providers and queries, verification status + reasons
Evidence:     source_url, provider, retrieved_at, claim, supporting_text,
              type (quote | paraphrase | snippet | structured), supports[]
RunMetadata:  stage timings, requests and errors per provider, LLM tokens, cost, counts
```

Hard constraints and preferences are separate fields. "Ideally 8+ years" must never
silently become a filter.

## 6. Eligibility, deduplication, ranking

**Eligibility (code only).** Each candidate is `eligible`, `ineligible` or
`needs_review`, with the rule that decided it.
- **Dates:** windows are computed from `run_date`, never hard-coded. Tungsten former
  employees left 6 months to 3 years ago; competitors' former staff within 2 years. A
  missing departure date never passes silently.
- **Exclusions:** organisation names are normalised and matched against an explicit
  alias list (Tungsten Automation = Kofax), with no loose substring matching. The LLM may
  suggest aliases, but they never enter the exclusion list without evidence.
- **Polish exclusions** (PKP Cargo, PKP PLK, DB Cargo Polska, UTK) apply to *current* staff
  only. Former staff remain eligible.
- **Customers** need evidence of direct product use; competitors exclude founders and
  C-level.

**Deduplication.** Exact profile URL first, then normalised name plus a shared employer.
Similar names alone never merge, and the LLM is used only for genuinely ambiguous pairs.
All sources are kept on merge. A person who fits two groups may appear in both, with the
overlap recorded.

**Ranking.** Only eligible candidates are ranked. Initial weights, to be revisited after
labelling:

| Criterion | Tungsten | Polish |
|---|---|---|
| Role and group fit | 25% | 20% |
| Target / product or rail-sector relevance | 20% | 25% |
| Hands-on operational experience | — | 25% |
| Evidence strength | 25% | 20% |
| Recency and eligibility fit | 15% | — |
| Source quality and corroboration | 10% | — |
| Seniority (and independence) | 5% | 10% |

Component scores and a sourced rationale are kept, not just a total. Missing evidence
never scores optimistically. Groups are never padded to 20.

## 7. Evaluation

- **Sample:** label ≈30-50 Tungsten candidates across the groups. Each gets `yes` / `no` /
  `unknown` on identity, role relevance, organisation relationship, dates, exclusions,
  evidence supporting the claim, and overall eligibility.
- **Metrics, each with its sample size:**
  - precision overall and per group;
  - exclusion-violation rate;
  - duplicate rate;
  - claim-support rate.
- **Recall:** none claimed.
- **Polish:** a separate small sample, checking group assignment, current-employer
  exclusions and evidence of operational involvement.

## 8. Risks and mitigations

| Risk | Mitigation |
|---|---|
| LLM invents people, employers or quotes | Every person comes from a real search hit; quotes are checked against the source text by code |
| Stale profiles (left, or joined an excluded company) | Dated work history over headlines; keep the data date; flag uncertainty |
| Renamed or acquired companies | Aliases from the brief; whole-name matching; tests for look-alikes |
| Name collisions | Never merge on a name alone |
| Search operators behave differently from Google's | Probe each provider live before building on an operator |
| Cost grows with result volume | Cap queries per group; cache pages; deterministic checks before LLM calls |
| Design overfits Tungsten | Run the Polish brief early as the acceptance test |

## 9. Testing

Offline unit tests with mocked providers and a fake LLM; no test spends credits.
- **Brief parsing:** preferences stay preferences; rules are traceable to the brief.
- **Dates:** inside the window, too recent, too old, missing, and the 6-month, 2-year and
  3-year boundaries.
- **Exclusions:** aliases recognised, look-alike names not excluded, Polish former staff
  allowed.
- **Deduplication:** URL merges, same name with different employers kept apart,
  provenance kept.
- **Ranking:** ineligible people never returned, weights applied, order correct.
- **End to end:** a small pipeline run on mocks.

## 10. Schedule

| Evening | Goal | Steps |
|---|---|---|
| 1 | A working, tested Tungsten pipeline | Repo, config, CLI; models; brief parser (inspect the spec by hand); Brave + Exa adapters; per-group queries; extraction; date and exclusion rules; dedup; basic ranking; tests for dates, exclusions, dedup; one small live run, reading the evidence |
| 2 | Senior quality and generalisation | Better customer and competitor strategies; evidence and provenance; ranking; cost tracking; label and evaluate a sample; Polish brief with bilingual queries; README, results, agent log, presentation |

**Definition of done**
- All three Tungsten groups end to end, with group-specific search.
- Exclusions and aliases enforced, and nobody listed twice.
- Up to 20 per group with sourced reasons, never padded.
- Precision measured on a labelled sample.
- Time and cost per run.
- Tests for the critical rules.
- The Polish brief run with no pipeline changes.
- README, committed results and an honest agent log.

---

## What changed while building

The plan held: rules in the spec, code over LLM for rules, provenance on every claim,
measured precision and cost. Where the build departed from it, a measurement drove the
change (details in [AGENT_LOG.md](AGENT_LOG.md)):

- **Google via Serper replaced Brave.** Live probes showed Brave corrupting or ignoring
  the X-ray operators; Google honours them (entry 4).
- **No page fetching for profiles.** LinkedIn can't be fetched, but Exa `/contents`
  returns dated work history that code parses, so `httpx` + `trafilatura` was not
  needed (entry 5).
- **The spec model changed.** A single `target_name` doesn't fit the Polish brief, which
  has no target, so the target is optional. `dict` fields can't be in an LLM
  structured-output schema. Ranking weights moved from the LLM-parsed spec to config, so
  the model can't invent scoring (entries 1, 2).
- **One set of ranking weights for all briefs.** "Relevance" is scored against each
  brief's own topics, so per-brief weight tables weren't needed. Experts with no profile
  are penalised (entries 10, 11).
- **New stages the plan didn't foresee:**
  - people search at discovered organisations, for customers and the Polish groups (entry 7);
  - profile lookup for people named only on pages (entry 11);
  - an early stop once a group has enough qualified candidates, which cut cost by ≈35-40% (entries 9, 10).

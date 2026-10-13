# External Sourcing Agent: How It Works

This document walks through the whole program: what each step does, where its code
lives, the strategy behind it, how specific sourcing problems are solved, why the same
code works for very different briefs, and what a run costs. [README.md](../README.md) is
the overview and usage guide, [PLAN.md](PLAN.md) the initial plan, and
[AGENT_LOG.md](AGENT_LOG.md) the decision log with the experiments behind each choice.

**Status (2026-10-11):** all nine stages run end to end on both briefs with one
command, from the raw brief to `final_results.md`: up to 20 experts per group, each with
name, relevant position (title and company), profile link, a reason, and a verbatim
quote with its source link. Precision on the hand-labelled Tungsten shortlist: 60 of 60
(95% interval 94-100%), no exclusion violations, no duplicates (section 4.11). Open: labelling the Polish brief, and the Staff-level
extras (section 11).

---

## 1. The pipeline at a glance

```
 brief.md
    │
    ▼
 1. brief_parsing ──────────► brief_spec.json         Haiku extracts, code builds windows and checks quotes
    │
    ▼
 2. query_generation ───────► queries.json            templates + Haiku terms / company queries
    │                                                 (+ 1-2 Google lookups for a customer-story section)
    ▼
 3. search (first round) ───► search_results.json     Google via Serper (+ Exa company discovery)
    │
    ▼
 4. candidate_extraction ───► candidates.json/.csv    Exa /contents for Google hits, code parses profiles,
    │      │                  organisations.json/.csv Haiku reads discovery pages
    │      └─ top-up round ─► (added to search_results.json)
    │                         Exa people queries, only for groups still short of the target
    ▼
 5. organisation_people ────► org_people_queries.json one Google X-ray per discovered organisation,
    │                         org_people_search_results.json   in batches of 10 until the group has enough;
    │                         (candidates added to candidates.json/.csv)   Exa last, only if still short
    ▼
 6. profile_lookup ─────────► profile_lookup.json      the LinkedIn profile of each person named on a page:
    │                         (profiles added to       up to three Google queries, accepted by code on the
    │                          candidates.json/.csv)   same name + the organisation (result or work history)
    ▼
 7. entity_resolution ──────► resolved_candidates.json canonical identities: same profile URL, or same name
    │                                                  + shared employer, or a profile-lookup link; ambiguous
    │                                                  profile pairs -> Haiku; one merged record per person
    ▼
 8. verification ───────────► verification.json        code only: eligible / ineligible / needs_review,
    │                                                  with the rule that decided each
    ▼
 9. ranking ────────────────► final_results.json/.md/.csv   Haiku checks the group definition, scores fit and
                              verification.csv             writes a reason citing an evidence item; code adds
                                                           recency + corroboration, weights, top 20
```

| # | Stage | Code | Reads | Writes | LLM? |
|---|---|---|---|---|---|
| 1 | Brief parsing | `brief/parser.py` | raw brief (Markdown) | `brief_spec.json` | 1 Haiku call |
| 2 | Query generation | `search/query_generator.py`, `search/people_plan.py`, `search/people_queries.py`, `search/company_queries.py` | `brief_spec.json` | `queries.json` | 1 Haiku call per group |
| 3 | Search | `search/runner.py`, `search/serper.py`, `search/exa.py`, `search/base.py` | `queries.json` | `search_results.json` | none |
| 4 | Candidate extraction (+ Exa top-up) | `extraction/candidate_extractor.py`, `extraction/profiles.py`, `extraction/pages.py`, `extraction/enrichment.py`, `extraction/orgs.py`, `adaptive.py` | `search_results.json` | `candidates.json`, `candidates.csv`, `organisations.json`, `organisations.csv` | Haiku per discovery page (and the rare profile without structured data) |
| 5 | People at discovered organisations | `people_search/organisations.py`, `adaptive.py` | `organisations.json`, `candidates.json` | `org_people_*.json`, updated candidates | none in practice |
| 6 | Profile lookup for people named on pages | `people_search/profile_lookup.py` | `candidates.json` | `profile_lookup.json`, updated candidates | none |
| 7 | Entity resolution and deduplication | `resolution/identity.py` | `candidates.json` | `resolved_candidates.json` | Haiku only for ambiguous profile pairs (budget 40) |
| 8 | Verification (eligibility, exclusions) | `verification/eligibility.py`, `verification/employment.py` | `resolved_candidates.json` | `verification.json` | none |
| 9 | Ranking and final shortlist | `ranking/` (`ranker`, `assessor`, `dossier`, `scoring`), `reporting/shortlist_files.py` | resolved + verification | `final_results.json`, `final_results.md`, `final_results.csv`, `verification.csv` | 1 Haiku call per 8 eligible candidates |
| — | Review exports | `reporting/review_sheets.py`, `verification/employment.py` | `candidates.json` | `candidates.csv`, `organisations.csv` | none |
| — | Evaluation | `evaluation.py` | a labelled `final_results.csv` | `evaluation.json` | none |
| — | Bookkeeping | `metrics.py`, `reporting/run_summary.py`, `pipeline/` | — | `run_metadata.json`, `run_history.jsonl` | — |

`pipeline/orchestrator.py` runs the stages; `cli.py` (or `python -m sourcing_agent`) is the
entry point. Every stage writes a JSON artifact (a Pydantic model from `models/`), and a run can start from any saved
artifact (`--brief`, `--spec`, `--queries`, `--search-results`, `--organisations`,
`--candidates`), so a human can review or edit the spec, the queries or the organisation
list in between without re-running (or re-paying for) earlier stages. `--candidates`
re-runs profile lookup (people not yet looked up), resolution, verification and ranking
(about a minute, cents; free when nothing changed).

---

## 2. Design principles

1. **Deterministic where possible; the LLM where judgement is needed.** Haiku reads the
   brief, writes search terms, reads unstructured pages, settles ambiguous identity pairs
   and ranks. Every rule that can be checked mechanically runs in code: date windows,
   current/former status, organisation matching, exclusions, excluded roles, identity
   merges on URL or name + employer. Ranking never overrides a failed rule. Model output is
   validated against a schema (structured outputs), and every extracted claim carries a
   verbatim quote that code checks against the source text; items whose quote is not
   found are dropped, with a warning.
2. **Structured data over text, text over snippets.** Employment history comes from
   Exa's structured person entities (dated work history) whenever possible, parsed by
   code. A search snippet supports discovery, never eligibility.
3. **No scraping of LinkedIn.** LinkedIn pages are never fetched directly (login wall,
   terms). Profiles are found through public search engines and read from Exa's index.
4. **Templates for what is predictable, the LLM for what is not.** Former-employee and
   competitor queries are built by code from the spec (operators, exclusions, date
   slices); the LLM only supplies role/topic words and, for groups without named
   organisations, company-discovery queries.
5. **Search only as much as a group needs, measured the way the output is.** The final
   list has 20 experts per group. Once a group has `TARGET_CANDIDATES_PER_GROUP` (25)
   *qualified* candidates — passed verification and judged by the LLM to meet the group's
   hard requirements, the final list's own test — the agent stops enriching, skips the Exa
   top-up and stops stage 5 for that group (sections 4.4-4.6). Thin groups keep searching.
6. **Nothing brief-specific in code.** No company names, countries, languages or group
   names are hard-coded; everything comes from the parsed spec (section 6).
7. **One person, one record, every source.** Entity resolution builds a canonical
   identity per person across queries, engines, pages and groups; a name alone never
   merges two people (section 4.8).
8. **Every reason is sourced.** The reason shown for an expert is the LLM's sentence,
   but the quote and link under it are an evidence item the LLM cited by id and code
   copied, so they always exist in a real source.
9. **Every run is measured.** Token usage, provider requests and cost (exact for Exa,
   estimated for Serper and Anthropic list prices) are recorded per run, including failed
   runs.

---

## 3. Data model (`models/`)

| Model | Stage | What it holds |
|---|---|---|
| `BriefDraft` | 1 (LLM output) | What Haiku extracts: groups, organisations and aliases, exclusions, windows **in months**, hard constraints vs preferences, each with a `source_quote`. |
| `BriefSpec` | 1 | The validated spec: calendar date windows computed from the run date, group ids, exclusions with scope, validation warnings, ambiguities, the brief text and its SHA-256. Every later artifact records this hash and is refused if it doesn't match. |
| `QueryPlan` / `GroupQueryPlan` / `SearchQuery` | 2 | Per group: mode (`people` or `companies_then_people`), role/topic keywords, and queries (id, provider, type, language, intent, organisation). |
| `SearchResults` / `SearchHit` / `QueryRun` | 3 | One hit per result per query (title, URL, canonical URL, snippet, Exa text and entities, rank); one run per query (requests, results, filtered, why paging stopped, cost, errors). |
| `Candidate` / `Position` / `Evidence` | 4-5 | Name, profile URL, positions (employer, title, month-level dates, current flag, which brief/discovered organisations the employer matched), typed evidence (`provider_structured`, `quote`, `snippet`), `target_mentioned`, providers and query ids. |
| `DiscoveredOrganisation` / `OrganisationSet` | 4 | Organisations found by company discovery, with quotes and source URLs and an `include` flag a reviewer can switch off. |
| `CandidateSet` | 4-5 | Candidates, organisations, per-group summary (`candidates`, `matched`, `useful`, `organisations`), enrichment cost and warnings. |
| `ProfileLookup` / `ProfileLookupSet` | 6 | Per page-named person: the queries, the outcome (`found` / `not_found` / `ambiguous`), the matching profiles and how the accepted one was accepted (`accepted_on`). |
| `Identity` / `MergeLink` / `ResolvedSet` | 7 | One canonical person: `identity_id` (`profile:<linkedin slug>` or `page:<name>-<organisation>`), name, profile URLs, groups, the source records merged and *why* (`same_profile_url`, `name_and_organisation`, `profile_lookup`, `llm_same_person`); one merged `Candidate` per identity per group (`identity_id`, `also_in_groups`); duplicate statistics. |
| `Verification` / `RuleResult` / `VerificationSet` | 8 | Per candidate: `eligible` / `ineligible` / `needs_review`, the departure status, and every rule's outcome (`pass` / `fail` / `review`) with a readable detail. |
| `Expert` / `GroupResult` / `FinalResults` / `Assessment` | 9 | The shortlist: per group up to 20 experts (rank, name, title, company, profile URL, reason, evidence quote + URL, status, score breakdown, sources, `also_in_groups`), a separate `for_review` list of borderline candidates, group counts, the weights, and the LLM verdict and scores of every ranked candidate including those not returned. |
| `RunMetadata` | all | Stage timings, every LLM call's tokens and cost, search usage per provider and purpose, totals, pricing assumptions, per-stage counts (merges, decisions by rule, returned), errors. |

---

## 4. Step by step

### 4.1 Brief parsing — `brief/parser.py`

**What it does.** Turns a free-text brief into a `BriefSpec`.

1. `parse_brief_file` sends the brief to Haiku 5.5 with `SYSTEM_PROMPT` and the
   `BriefDraft` schema (structured outputs through `llm.py`, `messages.parse`).
   The prompt asks for extraction only: no organisations, aliases or titles from the
   model's own knowledge; an alias only when the brief itself says "formerly X";
   mandatory wording → hard constraint, "ideally/preferred" → preference; time windows in
   **months**, never calendar dates.
2. `build_spec` (code) computes absolute departure windows from the run date
   (`departure_window_dates`, `subtract_months`), normalises group ids, and checks every
   `source_quote` against the brief (`quote_in_text`, tolerant of quote styles and
   whitespace). Rules whose quote is not in the brief become `validation_warnings`.
3. Anything the model was unsure about lands in `ambiguities` for review before searching.

**Why this way.** A wrong hard constraint silently drops good experts, so dates are
computed by code (deterministic, testable) and every rule is traceable to a quote. The
spec can be reviewed and edited by hand; later stages start from it with `--spec`.

**Cost:** one Haiku call, under $0.01.

### 4.2 Query generation — `search/query_generator.py`, `search/people_plan.py`, `search/people_queries.py`, `search/company_queries.py`

`generate_queries` plans every group in parallel (`plan_group`). Each group gets one of
two modes, decided by the spec alone:

**A. Organisations named in the brief → `people` mode** (e.g. former Tungsten/Kofax
staff, ABBYY/UiPath/… competitor staff). One Haiku call supplies broad
`role_keywords`/`topic_keywords` (`TERMS_SYSTEM_PROMPT`, cleaned by `clean_terms`). Code
builds the queries (`build_people_queries`):

- **Google LinkedIn X-rays** (`google_people_query`): `site:linkedin.com/in "<org>" …
  -site:linkedin.com/in/dir`. People currently at an organisation are excluded three ways
  (`current_staff_exclusions`): a single distinctive title word (`-intitle:tungsten`;
  LinkedIn titles end with the current employer), `-"at <name>"` (headline "Title at
  Company") and `-"<name> · Present"` (a current experience entry). Google reads at most
  32 words (`GOOGLE_MAX_WORDS`), so keyword groups are trimmed first and exclusions added
  while they fit; the directory exclusion always stays.
- **Former employees: slices for recall.** A filtered X-ray returns only ≈10 profiles
  (page 2 is empty), so recall comes from many differently-sliced queries rather than
  deeper paging (`_former_slices`), alternating:
  1. *departure quarters* written the way LinkedIn writes dates
     (`("Jul 2025" OR "Aug 2025" OR "Sep 2025")`), newest first, covering the window
     (`departure_quarters`);
  2. *job-function groups* (`FUNCTION_GROUPS`: channel/partner, success/support,
     engineering, sales, services, senior, product/marketing, finance/ops);
  3. *the former name* with the window's early years or with employee titles
     (`"Kofax" ("2023" OR "2024") "Tungsten Automation"`).
  Product-name and plain-alias queries were dropped: they mostly found people listing
  "Kofax" as a skill.
- **Current or current-or-former staff:** one "by role" query per organisation and, when
  former staff qualify, a "mentions the company but not in the title" query; with several
  organisations, every organisation's first query comes before anyone's second
  (`_round_robin`).
- **Exa people queries** (`exa_people_query`, `exa_moved_on_query`): natural-language
  ("professional profiles of former employees of X (also known as Y) who left between
  October 2023 and April 2026"), because Exa's people search rejects date filters.

**B. No organisations named → `companies_then_people` mode** (the target's customers,
all three Polish groups). One Haiku call writes `QUERIES_PER_PROVIDER` (5) Google and 5
Exa *company-discovery* queries plus role/topic words (`COMPANY_SYSTEM_PROMPT`). Code then
validates them (`build_company_queries`): count enforced, near-duplicates dropped
(Jaccard ≥ 0.8), missing group languages and queries that are really people searches
flagged, and for target-relative groups, queries that don't name the target flagged.

- **Customers of the target** get templated Google queries instead of the LLM's
  (`customer_story_queries`), aimed at pages that *name* customers:
  1. the target's own case-study section, found by code (`discover_customer_story_sites`:
     one Google search per name, then the most common story-like first path segment on a
     host containing the target's name, e.g. `site:tungstenautomation.com/case-studies`;
     Haiku did not know this path);
  2. case-study aggregators (`site:featuredcustomers.com/vendor/<slug>`,
     `site:casestudies.com/company/<slug>`);
  3. `("<target>" OR "<alias>") ("case study" OR "customer story" OR "success story")`.
  Exa keeps the LLM's queries (press releases, deployments by industry), which found 22
  customers Google did not.

People queries for discovered organisations are built later, in stage 5, with the same
templates.

**Caps:** at most 5 Exa queries and 5 + 5 company queries per group
(`QUERIES_PER_PROVIDER`), at most 20 Google X-rays per group (`XRAY_QUERIES_PER_GROUP`).

### 4.3 Search — `search/runner.py`, `search/serper.py`, `search/exa.py`, `search/base.py`

`run_searches` runs a `QueryPlan`: each provider on a small thread pool, both providers in
parallel; timeouts, connection errors, 429 and 5xx are retried with bounded backoff
(`request_with_retries`); a failed query is recorded, not fatal. Results are returned in
plan order.

- **Google via Serper** (`SerperSearch`): `POST google.serper.dev/search` with
  `{"q", "page"}`, `hl` for non-English queries and **no country (`gl`)**, since the
  briefs have no country constraint. 10 results and 1 credit per page; paging stops on an
  empty page, a short last page, a page with no new URLs, or `SERPER_MAX_PAGES`.
  LinkedIn X-rays fetch **page 1 only** (`adaptive.first_page_only`). Extraction reads rank 1
  of every query first and stops at the group's target, so later pages are fetched only
  for a group that has read every hit and is still short (`adaptive.deepen`, before the
  Exa top-up): page 2 of its X-rays, then page 3 if still short, up to
  `SERPER_PEOPLE_MAX_PAGES` (3). Profiles already seen are not read again, and ranks
  continue (page 2 starts at rank 11). Measured on Tungsten (2026-10-11): the search stage
  went from 126 to 54 Serper requests and from 1m 55s to 45 s. Before, the run fetched 3
  pages of every X-ray but read only ranks 1-12 of the former-employee queries and 1-4 of
  the competitor ones. `site:` listings keep `SERPER_MAX_PAGES` (10) and page normally
  (96 case studies for Tungsten).
- **Exa** (`ExaSearch`): `POST /search`, `category: people` + `type: auto` for people,
  `type: deep` for company discovery, 50 results per request, exact cost from
  `costDollars`. People results carry structured person entities with dated work
  history. On the former-employee query `auto` returned 50 results in ≈2 s and `deep` at
  most 27 in ≈12 s, neither finding more genuine former employees (AGENT_LOG entry 6).
- **Filtering** (`keep_result`, `canonical_url`): people queries keep only LinkedIn
  profiles (`/in/<slug>`, not `/in/dir`, posts or company pages); company queries drop job
  boards and Exa's synthetic pages. Dropped results are counted as `filtered`.

**First round only.** With a target set, the search stage runs everything except Exa
*people* queries (`adaptive.first_round`); those are a conditional top-up (4.5).

**Failures are kept apart from empty results.** Each query's `QueryRun` records why it
stopped:
- `empty_page`, `no_more_results` or `repeated_page` when the provider simply had nothing
  more;
- `error`, with the message, when a request failed after retries, returned a non-200, or
  sent an unreadable body;
- results dropped by the filters are counted in `filtered`.

The other failure cases:
- **A missing API key:** that provider's queries are recorded as skipped with
  "`<PROVIDER>_API_KEY` not set", and the run continues on the other provider.
- **Out of credits, or a rejected API key (HTTP 401/403)** (Serper, Exa or Anthropic):
  fatal, raised as `QuotaExhausted` or `InvalidCredentials`. Carrying on would write
  degraded results.
- **Page contents** (`/contents`) that fail or come back unreadable: counted as failed
  pages in `candidates.json`, never as empty profiles.

### 4.4 Candidate extraction — `extraction/`

`extract_candidates` (`candidate_extractor.py`) turns hits into candidates, group by
group, **in chunks of 50, best-ranked first** (rank 1 of every query, then rank 2, …;
`_priority`). Only the chunk being read is enriched (`_Enricher`): Google hits have just
a title and snippet, so their content is fetched from Exa `/contents` (≈$0.001 per page);
Exa search hits already carry content.

| Source | Method (`extraction_method`) | Code |
|---|---|---|
| LinkedIn profile with an Exa person entity (almost all) | work history parsed by code: employer, title, month-level dates, current flag (`exa_entities`) | `profiles.candidate_from_profile`, `_position_from_work` |
| Profile text without an entity (rare: ≈2 in 1,100) | Haiku lists positions, each with a verbatim quote checked against the text (`llm_profile_text`) | `profiles.py`, `PROFILE_SYSTEM_PROMPT` |
| Enrichment failed | title and "Experience: X" parsed from the snippet; status unknown (`snippet_only`) | `profiles._experience_from_snippets` |
| Discovery page (case study, press release, register) | Haiku lists named people and qualifying organisations, each with a verbatim quote; names must appear on the page (`llm_page`) | `pages.extract_from_page`, `PAGE_SYSTEM_PROMPT` |

**Early stop per group.**
- *Profiles* stop once the group has `TARGET_CANDIDATES_PER_GROUP` (25) qualified
  candidates. After each chunk the **gate** (`ranking/assessor.Assessor.qualified`) runs the
  deterministic verification on the group's candidates so far and asks Haiku for a verdict
  on the eligible ones it hasn't judged yet; only `meets` counts. Candidates from earlier
  rounds count (`base_candidates`). With `QUALIFY_WITH_LLM=false` the gate is off and date
  fit counts instead (`verification/employment.useful_statuses`).
- *Why the gate:* the first version counted date fit (target 40). In groups whose hard
  requirement names a seniority ("former operations, commercial or fleet *leads*"), only
  ≈25% of date-fit candidates were leads (Polish freight 11 of 41, infrastructure 0 of 37),
  so search stopped long before it had 20 usable people. Counting with the final list's
  own test makes thin groups keep searching and ample groups stop as early as before.
- *Discovery pages* stop once the group has `ORG_PEOPLE_QUERIES_PER_GROUP` (40) distinct
  organisations — the most stage 5 will search — or at `EXTRACTION_MAX_PAGES_PER_GROUP`
  (150).
- What was not read is never enriched or sent to the LLM, and a warning records it
  ("competitors: 28 qualified candidates (target 25) after 50 of 315 profiles; the other 265
  were not read").

**Organisation matching** (`extraction/orgs.py`, all code). Employer strings vary
("Kofax India Pvt. Ltd.", "Tungsten Automation, a Kofax company", "PKP Polskich Linii
Kolejowych"). `name_matches` compares whole token runs after normalising accents, case,
"&"/"and" and legal forms (`S.A.`, `sp. z o.o.`, `Pvt Ltd`), never substrings ("IBM" ≠
"Mibmax"); joins adjacent tokens ("Open Text" = "OpenText"); and matches acronyms
against the initials of a run of words, which also absorbs Polish case endings ("UTK" =
"Urząd/Urzędu Transportu Kolejowego"). A company the target acquired only matches if the
spec lists it as an alias.

**Discovered organisations** (`pages.py` → `candidate_extractor.py`). Code rejects
descriptions instead of names (`is_placeholder_org`: "A major European insurer",
"Regional US bank"), strips rename notes (`clean_org_name`: "Cerner, now Oracle Health"
→ "Cerner"), requires the name to be on the page, and skips the target, the group's
named organisations and organisations excluded for current *and* former staff
(`_not_a_discovery`; organisations excluded only for current staff stay discoverable,
since their former staff can be the group). Variants of an organisation the brief names
fold into its name (`canonicalise_organisations`, `same_organisation`: "PKP Polskie
Linie Kolejowe S.A." → "PKP PLK"), while subsidiaries stay separate ("PKP Cargo
Terminale" ≠ "PKP Cargo"). Legal-form variants merge (`_merge_organisations`). People
quoted on pages are re-matched once the group's organisations are known
(`_tag_discovered_organisations`). The list is saved best-evidenced first as
`organisations.json`, with an `include` flag for review.

Duplicates within a group merge on exact keys only (same profile URL; same name +
organisation from pages); cross-source and cross-group resolution is stage 7 (4.8).

### 4.5 Exa top-up — `adaptive.py`

After extraction, `top_up` looks at each `people`-mode group: if it already has the
target of qualified candidates, its Exa people queries are not run (and a warning says so);
otherwise they run, are extracted with the existing candidates counted toward the target,
merged, and their results appended to `search_results.json`.

Why Exa is the top-up and Google the first round: on the saved runs, Google was the
better first engine for named-organisation groups (Tungsten former employees: 22 useful
from Google vs 5 from Exa), Exa people search ignores "former" (about half its "former
employee" results still worked there), and Exa costs ≈$0.047 per 50-result query against
≈$0.002 per Google X-ray. But Exa is not useless — it carried Polish infrastructure
planners (21 of 29 useful were Exa-only) and added 177 useful competitor staff — so it
is switched on by the data (a group falling short), not off by a fixed rule.

### 4.6 People at discovered organisations — `people_search/organisations.py`

For `companies_then_people` groups, `run_organisation_people`:

1. plans one Google X-ray per included organisation, best-evidenced first, up to
   `ORG_PEOPLE_QUERIES_PER_GROUP` (`build_organisation_people_queries`):
   - customer groups require the target's names
     (`site:linkedin.com/in "Kemira" ("Tungsten Automation" OR "Kofax") -"at Tungsten
     Automation" …`), with no `-intitle:` (that would drop customer staff titled
     "Kofax developer"), and get no Exa queries (249 Exa profiles at customers, 3 naming
     the target);
   - other groups use the role keywords; former-staff groups also exclude current staff
     of each organisation; Exa queries are planned for the first 5 organisations;
2. runs them **in batches of 10 organisations** (`ORG_BATCH`): search → enrich → extract →
   merge, then checks the group's useful count; it stops as soon as the target is reached;
3. fetches page 1 of each X-ray; once every organisation is searched and the group is still
   short, the later Google pages of those X-rays (4.3), one page at a time;
4. runs the Exa queries only after that, and only if still short.

One organisation per query because Google returns ≈10 profiles per X-ray: on six
customers, per-organisation queries found 77 profiles against 17 for shared queries.
Each customer candidate records `target_mentioned` (the profile names the target), the
evidence of hands-on use the brief asks for. The full plan is saved as
`org_people_queries.json`; what actually ran is in `org_people_search_results.json`.

`--organisations results/<brief>/organisations.json` reruns only this stage, after a
reviewer has switched off organisations (`include: false`).

### 4.7 Profile lookup — `people_search/profile_lookup.py`

**Why.** Case studies, press releases and conference pages name people with a title and
an organisation but no profile ("Rob Duber, Manager, Process Improvement, University
Hospitals"). Nothing else searches for them by name: the people searches look for anyone
at an organisation. Without this stage, most page-named customers reached the shortlist
with a case-study page where the profile should be, and "dates not stated" as their status.

**What it does.** For each page-named person with an organisation (one search per
person, page records of the same person grouped; up to `PROFILE_LOOKUPS_PER_GROUP`, 60,
per group, those whose page names the target first), up to three Google queries, first
page only, each run only if the earlier ones settled nothing. The organisation is searched
without its legal form ("PCC Intermodal", not "PCC Intermodal S.A.", which profiles rarely
repeat and which made quoted queries return nothing):

1. `site:linkedin.com/in "Robert Duber" University Hospitals`: the full name quoted;
2. `site:linkedin.com/in Wojciech Skrzypinski PCC Intermodal`: unquoted and without
   accents, because LinkedIn often drops diacritics or adds a middle initial (his profile
   says "Wojciech J. Skrzypinski"; the quoted "Wojciech Skrzypiński" returned nothing);
3. `site:linkedin.com/in "Duber" "University Hospitals"`: surname and organisation
   quoted, for short first names (LinkedIn has "Rob Duber").

A profile is accepted only when code checks pass, strongest first (`accepted_on` in
`profile_lookup.json`):

- **`search_result`**: the name in the result title is the same name (`name_relation ==
  "same"`, the entity-resolution rule: accents, honorifics, credentials, short first
  names), and the result's title or snippet names the organisation (`name_matches`).
- **`work_history`**: a past job is rarely in the snippet. Jakub Styczeń's result shows
  only his current "CEO / CCO" role, and three LinkedIn profiles share his name. So up to
  `MAX_PROFILES_TO_VERIFY` (3) same-name profiles are read (Exa `/contents`, ≈$0.001 each),
  and one is accepted if its work history includes the organisation *itself*, up to legal
  form and acronyms (`same_organisation`): "EUROTERMINAL SŁAWKÓW SP. Z O.O." is
  "EUROTERMINAL Sławków". A name merely containing it is not enough: when the looser
  rule was tried live, "Rail Polska" matched a different person's "DB Schenker Rail Polska".
- **`profile_text`**, the weakest: a profile whose record has no work history at all
  (Exa had only languages and posts for Wojciech Skrzypinski) is accepted only if its own
  text names the organisation and no other same-name profile does.
- In every tier, exactly one profile must pass; two or more are `ambiguous` and none is
  taken. Same-name people read for checking but not accepted never join the candidates.

Accepted profiles are added to `candidates.json` with their dated work history (no LLM);
the page record gets `profile_lookup` (found / not_found / ambiguous) and
`same_person_as`, which entity resolution merges (rule `profile_lookup`), so the person
keeps the page quote and gains dates. Every lookup (queries, outcome, matches, how it was
accepted) is added to `profile_lookup.json`; a re-run with nobody new to look up leaves
it as it is. People already found, or tried with the current `LOOKUP_VERSION`, are
skipped, so `--candidates` re-runs cost nothing; when the lookup improves, the version
goes up and only the people not found before are tried again.

**Measured.**

| Brief | Looked up | Found | Not found | Ambiguous | Cost, time |
|---|---|---|---|---|---|
| Tungsten | 33 | **15** (13 on the search result, 1 on work history, 1 on profile text) | 18 | 0 | $0.104, 34 s |
| Polish | 30 | **9** (all on the search result) | 21 | 0 | $0.091, 31 s |

Examples of acceptances below the search-result tier:
- Brian Harris (Loma Linda University, work history: his profile lists "Executive Director,
  Web Operations & ECM Solutions, Loma Linda University, 2008-2013", the role the case
  study names);
- Jarkko Hanhimäki (Kemira, profile text).

Every returned expert in both briefs has a LinkedIn profile (60 of 60 for Tungsten, 40 of
40 for Polish).

Profiles Exa cannot read (not in its index) count as not found; 4-8 such profiles per brief.

**What it fixed alongside.** Ranking used to fill an empty profile column with the first
web page the person appeared on (`profile_url=c.profile_url or web`), so case-study and
aggregator pages looked like profiles. The column now holds only a real profile; the
Markdown says "(no profile found)" and the page stays the evidence link. And the ranking
dossier now lists evidence of group fit (page quotes, profile lines naming the target)
before employment records: with only 6 evidence items per dossier, a merged person with
many dated positions had lost the case-study quote naming them, and three customers fell
from "meets" to "unclear" until this was fixed.

### 4.8 Entity resolution and deduplication — `resolution/identity.py`

**What it does.** `resolve` turns candidate records into canonical identities. The same
person arrives many ways: several queries and both engines (same profile URL), a quote on
a case-study page plus a LinkedIn profile, or two expert groups. Records are clustered
with union-find; rules, strongest first:

1. **Same canonical profile URL** (`canonical_url` already strips locale subdomains,
   tracking parameters and trailing paths) → same person, across groups too.
2. **Same name + a shared employer**, when at least one record is not a profile (a page
   quote and a profile, or two pages). Names are compared after `name_tokens`: accents
   folded (including letters Unicode doesn't decompose, "Paweł" → "pawel"), honorifics
   and credentials dropped ("Dr. Jane Doe, MBA" = "Jane Doe"), bracketed notes and
   headline tails removed, middle names ignored (first + last name). Employers match with
   the same organisation matching as everywhere else (`name_matches`, legal forms,
   acronyms). **Profile-lookup links** (`same_person_as`, 4.7) are merged at this step
   too: the lookup accepted the profile on the same name and the page's organisation in
   the search result, which the profile's own positions may word differently.
3. **Two different profiles** with the same or a near name ("Matthew T." / "Matthew
   Thompson", a one-letter spelling difference) and a shared employer with overlapping
   dates are **ambiguous**: people keep two LinkedIn accounts, and two people can share a
   name at a large employer. Haiku decides from both records (`SamePersonDraft`, "true
   only when the records corroborate each other"); without an LLM or above
   `MAX_IDENTITY_LLM_PAIRS` they stay apart.

A name alone never merges two records; candidate pairs are only compared within a block
(same first name and last-name initial), so the work stays linear in practice.

**Canonical identity.** Each cluster becomes an `Identity` with a stable id
(`profile:<slug>` from the profile URL, else `page:<name>-<organisation>`), the best name
(from the structured profile), all profile URLs, the groups it appears in, the source
records, and the `MergeLink`s that explain each merge. Within each group the cluster's
records merge into one candidate (`_merge`): the structured profile is the base; positions
and evidence from every record are kept (deduplicated), all hits, queries and providers
kept, `target_mentioned` true if any source names the target.

**Cross-group policy.** A person who qualifies for two groups stays in both (e.g. a
former Tungsten employee now at a customer), each record carrying `also_in_groups`, so a
valid expert is never silently dropped from one list because they fit another.
Within-group duplicates and cross-group identities are counted in `ResolutionStats` and
`run_metadata.json`.

Measured on the committed runs: Tungsten 310 records → 294 people (16 within-group
duplicates, mostly case-study quotes merged into profiles; nobody in two groups);
Polish 1,420 → 1,380 (18 within-group duplicates, 22 people in two groups; Haiku judged 2
ambiguous profile pairs and merged neither).

### 4.9 Verification — `verification/eligibility.py`

**What it does.** `verify_all` gives every resolved candidate an auditable decision:
`eligible`, `ineligible` or `needs_review`, with every rule's outcome. Any
failed rule → ineligible; else any review → needs_review; only eligible candidates are
ranked. All rules are code and read from the spec:

| Rule | Passes when | Fails / review when |
|---|---|---|
| `identity` | a personal name of two or more words | "LinkedIn Member" (fail); a single word (review) |
| `evidence` | more than a search snippet | only a snippet: employment and dates unreadable (review) |
| `relevant_position` | a position at one of the group's organisations (named, else discovered, else the target) | none (fail) |
| `employment_status` | the group's rule: `former` → left (within the window if any); `current` → current; `current_or_former` → either, within the window for leavers; with no window a relevant position is enough | still there / left too recently / too long ago (fail); departure date missing where the rule needs it (review) |
| `exclusion` | no current position at an excluded organisation or its aliases (any position, for "current and former" exclusions) | a current position there, or a headline "at / @ <excluded>" not preceded by "ex-/former" (fail: catches stale records); a page names them there with no dates (review) |
| `excluded_role` | the relevant title is not one of the group's excluded roles | a match (fail). Brief phrases map to title patterns: "C-level" → `chief … officer`, `CEO/CFO/CTO/…`; "founder" → `founder/co-founder`; any other phrase → whole-word match |
| `target_evidence` | for groups defined by the target (its customers): a source names the target (profile line or page quote) | no source names it: working at a customer is not evidence of using its products (fail) |

**Alias semantics (deliberately conservative for exclusions).** An employer whose name
contains an excluded organisation's name as whole words counts as that organisation:
"Kofax India Pvt Ltd", "Tungsten Network, a Kofax company" (confirmed as Kofax), "PKP
CARGO TERMINALE sp. z o.o." (a PKP Cargo company). Substrings never match ("IBM" ≠
"Mibmax"), and an organisation excluded only for current staff never blocks its former
staff (former PKP PLK planners stay eligible).

`verification.csv` lists every resolved candidate with its decision, the deciding rule
and detail, and its ranking outcome.

### 4.10 Ranking and the final shortlist — `ranking/`, `reporting/shortlist_files.py`

**Split of work.**
- *Code* builds a dossier per eligible candidate from verified data: headline,
  positions with dates (relevant ones marked), the code-computed status ("left Apr
  2026", "current"), whether a source names the target, numbered evidence items
  (`C3.E1`: claim, verbatim quote, URL) and, for discovered organisations, the
  organisation's own evidence (`C3.O1`: the case-study quote saying it runs the product).
  It scores the objective components: **recency** (1.0 for current staff, else how
  recently they left within the window) and **corroboration** (a structured record 0.6
  or a page quote 0.5, +0.2 per further independent site; two copies of the same source
  don't count).
- *Haiku* gets the group definition (hard requirements, required evidence, target roles
  and preferences labelled "scores only", excluded roles, topics) and up to 8 dossiers
  per call. For each it gives a **verdict** against the hard requirements only: `meets`
  (every one shown), `unclear` (nothing contradicts them, but one is not shown, e.g. a
  department head whose remit isn't stated) or `does_not_meet` (one is contradicted:
  an individual product manager where the brief says product *leader*, an unrelated
  function, an outside adviser instead of an employee, a vendor employee instead of a
  customer). Target roles and preferences only affect scores. It also scores **role fit, relevance, seniority, evidence strength** (0-5), writes a
  one-sentence reason from dossier facts only, and cites one evidence id.

**Score.** total = 100 × Σ weight × component (each 0-1). Default weights, the same for
every brief: role fit 25%, relevance 20%, evidence strength 25%, recency
15%, corroboration 10%, seniority 5% (`RANK_WEIGHTS` overrides). What "relevance" means
differs per brief because it is scored against that brief's topics and preferences
(Polish rail-sector and hands-on operational experience; Tungsten product and
competitive relevance), not against hard-coded criteria.

**No-profile penalty.** An expert with no profile URL (named only on a web page, and
profile lookup found nothing) loses `NO_PROFILE_PENALTY` points (default 25, recorded as
`score.profile_penalty`). A client can't contact or check someone without a profile, so
qualifying people with a profile rank first. Those without one are still returned when too
few people with a profile qualify; the list is not shortened because of it. 25 points is
more than the usual gap between qualifying experts. On the Tungsten run, 9 customers who
qualify have no profile; with the penalty they score 16-41, below all 20 returned
customers (lowest 55), so every returned customer has a profile. The penalty is applied in
code after the verdict, so cached verdicts stay valid and changing it costs nothing.

**Output.** Only `meets` candidates are returned, sorted by total; the top
`MAX_EXPERTS_PER_GROUP` (20), or fewer if fewer qualify — the list is never padded.
`unclear` candidates are listed separately as **borderline, for researcher review** (the
best 10 per group), never counted as experts; `does_not_meet` candidates are recorded in
`assessments` and `verification.csv` with the requirement they fail. Each `Expert` carries the case study's four fields — **name; position (title
and company of the relevant role: the current one, else the latest at a relevant
organisation); profile link (LinkedIn, or empty when none was found); reason with a
verbatim quote and its source link** — plus status, score breakdown, all sources and
`also_in_groups`. The cited quote is copied by code from the evidence item, so it always
exists in a real source; an invalid citation falls back to the strongest evidence item.
If a ranking call fails (other than out of credits or a rejected key), its candidates get no verdict and are
not returned; a warning names the group and the count, and `--candidates` judges them
again.

Files: `final_results.json` (the model), `final_results.md` (per-group tables a reviewer
reads without running anything), `final_results.csv` (one row per expert with empty
label columns for precision review) and `verification.csv` (every decision).

### 4.11 Evaluation — `evaluation.py`

**How.** A reviewer copies `final_results.csv`, fills its seven label columns
(`identity_correct`, `role_relevant`, `organisation_relationship_correct`,
`employment_dates_valid`, `exclusions_honoured`, `evidence_supports_claim`,
`overall_eligible`) with `yes` / `no` / `unknown` (blank = not reviewed), and runs
`python -m sourcing_agent.evaluation <labels.csv>`. Output (`evaluation.json`, and a line
per group on screen), per group and overall:

- precision (`overall_eligible = yes` among labelled `yes`/`no`);
- exclusion-violation rate;
- critical-claim support rate (`evidence_supports_claim`);
- duplicate rate, computed from `identity_id` with no labels needed;
- the rate for each check.

Every rate comes with its sample size, a 95% Wilson interval (meaningful even at 20 of
20) and a count of `unknown` labels, which are kept separate from yes and no. No recall
is claimed: there is no complete list of qualifying experts to compare against.

**Results (Tungsten).** The shortlist from the full run of 2026-10-11, labelled by one
reviewer who opened every profile and evidence link; saved as
`results/tungsten_automation/eval.csv` and computed into `evaluation.json`.

| Group | Labelled | Precision | 95% interval | Exclusion violations | Duplicates |
|---|---|---|---|---|---|
| Former employees | 20 | 100% | 84-100% | 0 | 0 |
| Customers | 20 | 100% | 84-100% | 0 | 0 |
| Competitors | 20 | 100% | 84-100% | 0 | 0 |
| **All** | **60** | **100%** | **94-100%** | **0** | **0** |

- **Every check passed:** identity, role, organisation relationship, dates, exclusions and
  evidence scored 100% on all 60.
- **How strong this is:** one brief, one reviewer, 20 per group. Per group the evidence
  supports "very likely above ≈84%", not "100%"; ruling out a 1-in-20 error rate per group
  would need about 60 labels per group. No recall is claimed.
- **Next:** a Polish sample including its review table; a second reviewer on ≈15 of the
  same experts to measure agreement; the borderline `for_review` table, to see whether
  the verdict prompt is too strict.

### 4.12 Review exports — `reporting/review_sheets.py`, `verification/employment.py`

`departure_status` classifies each candidate against the group's relevant organisations
(named ones, else discovered ones, else the target): `left_within_window`,
`left_too_recently`, `left_too_long_ago`, `former_no_window`, `still_current`,
`dates_unknown`, `no_relevant_position`. Dates with month precision are compared in whole
months, the way the brief and LinkedIn count ("left 6 months ago" includes April when the
run date is 10 October); boundary-month departures are noted in the sheet.

`candidates.csv` (UTF-8 with BOM for Excel and Polish characters) has one row per
candidate, most promising first (status order per group type; customers with
`target_mentioned` first; most recent departure first), with relevant role and dates,
current employer, best evidence, and empty reviewer columns for precision labelling.
`organisations.csv` lists discovered organisations with quote, URL, source
count and `include`. Regenerate without API calls:
`python -m sourcing_agent.reporting.review_sheets results/<brief>`.

### 4.13 Run bookkeeping — `metrics.py`, `reporting/run_summary.py`, `pipeline/artifacts.py`

`RunTracker` records stage timings, every LLM call (tokens, duration, cost from
`LLM_PRICE_IN_PER_MTOK` / `LLM_PRICE_OUT_PER_MTOK` in `.env`) and every search round per provider and
purpose (`search`, `discovery`, `contents`, `profile_lookup`), each tagged with the
pipeline stage it ran in. At the end, `run_summary.summarise` builds two blocks that open
`run_metadata.json`:

- `summary`, readable: status, stages run, start time, duration, total cost with its split
  by API, cost per API (Anthropic per model with calls and tokens; Serper requests by
  purpose; Exa search requests and pages read), time and cost per stage split by API,
  and notes on where each cost comes from (Anthropic and Serper are estimates, Exa reports
  dollars). A part whose cost is unknown makes its total "$?" rather than understated.
- `costs`, the same numbers machine-readable (`total_usd`, `by_api`, `by_stage`).

The raw records follow (each LLM call, each search round), so every summary figure can be
traced. The CLI prints the same summary after a run. `run_metadata.json` holds the latest
run; `run_history.jsonl` appends every run, including failed ones.

```
"summary": {
  "status": "completed (stopped after ranking)",
  "stages": "profile_lookup -> ranking",
  "duration": "1m 03s",
  "total_cost": "$0.095 (Anthropic $0.007 + Serper $0.073 + Exa $0.015)",
  "cost_by_api": {
    "Anthropic (claude-haiku-5-5)": "$0.007: 5 calls, 26,477 input + 8,487 output tokens",
    "Serper (Google search)": "$0.073: profile lookup $0.073 (73 requests)",
    "Exa (search and page contents)": "$0.015: page contents $0.015 (15 pages read)"
  },
  "time_and_cost_by_stage": {
    "[6/9] profile_lookup": "27.8s, $0.088 (Serper $0.073, Exa $0.015)",
    "[9/9] ranking": "31.8s, $0.007 (Anthropic $0.007)"
  }
}
```

---

## 5. How specific problems are solved

| Problem | Solution | Where |
|---|---|---|
| Google returns only ≈10 profiles per filtered LinkedIn X-ray | Many slices (departure quarters, job functions, alias + years/titles) instead of deeper paging; measured with greedy set cover over experiment rounds: 6 → 28 former employees in the window | `_former_slices`, `departure_quarters`, `FUNCTION_GROUPS` |
| Excluding people who still work at the company | `-intitle:<word>`, `-"at <name>"`, `-"<name> · Present"`; a multi-word negated title phrase makes Google return nothing, so one distinctive word | `current_staff_exclusions`, `title_exclusion_word` |
| Renamed companies (Kofax → Tungsten Automation) | Aliases only from the brief; both names in queries and matching | `brief/parser`, `orgs.name_matches` |
| Skill-listers ("Kofax" as a skill, not an employer) | Employment comes from structured work history, not the search hit; product queries dropped | `profiles.py`, `query_generator` |
| Exa ignores "former" | Status computed from Exa's own work history; Exa demoted to a top-up | `employment.departure_status`, `adaptive.top_up` |
| Customers are not named in the brief | Company discovery first: the vendor's case-study section (path found by code), aggregators, generic case studies, Exa press releases; Haiku reads pages; then people at each customer | `customer_story_queries`, `pages.py`, stage 5 |
| Vendor listing page is JavaScript-rendered | `site:<case-study section>` pages through the individual case studies, whose titles name the customer | `discover_customer_story_sites` |
| LLM returns descriptions instead of names | Placeholder rejection, name must be on the page, verbatim quote checked | `is_placeholder_org`, `_name_on_page`, `quote_in_text` |
| Name variants, Polish case endings, acronyms | Token normalisation, legal forms, acronym-initials matching, folding into the brief's names; subsidiaries kept apart | `orgs.py`, `canonicalise_organisations` |
| Month-precision dates at window edges | Whole-month comparison, boundary months noted | `employment.py`, `review_sheets.py` |
| Hallucinated employers, dates or rules | Code parses structured data; every LLM claim needs a verified quote; rules carry brief quotes | `profiles.py`, `pages.py`, `brief/parser.py` |
| LinkedIn terms of service | Never fetched; found via search engines, read from Exa's index | `enrichment.py` |
| People named on pages have no profile (case-study page shown as the "profile", no dates) | Profile lookup: up to three Google queries per person (quoted name; unquoted without accents; surname + organisation), accepted only on the same name + the organisation (in the result, or in the profile's work history) + a single match; merged into the page record; the profile column holds only real profiles, and experts without one lose `NO_PROFILE_PENALTY` points | `profile_lookup.py`, `ranking/scoring.py` |
| The same person found several times (queries, engines, pages, groups) | Canonical identities: same URL; same name + employer; ambiguous profile pairs judged by Haiku; one merged record per group, overlap across groups recorded | `resolution/identity.py` |
| Name collisions (two people called Brian Harris) | A name never merges alone; a shared employer is required, and two profiles additionally need overlapping dates plus an LLM judgement | `resolve`, `name_relation` |
| Stale roles (profile record behind reality) | Headline "at <excluded>" fails the exclusion; `data_as_of` kept; status shown in the output | `eligibility._headline_says_at` |
| Made-up people or quotes | Every person comes from a real search hit; page people need their name on the page and a verified quote; reasons cite evidence by id and the quote is copied by code | `pages.py`, `ranking/ranker._expert` |
| Thin groups padded with weak matches | Three-way verdict: only `meets` is returned; `unclear` goes to a separate review table; the list is never filled to 20 | `ranker.rank`, `shortlist_files.final_markdown` |
| Personal contact data in quotes | Displayed quotes are cleaned of markdown and have phone numbers and email addresses redacted | `dossier.clean_quote` |
| Search stopping before enough *qualified* people | The early stop counts what the final list needs: verified + LLM "meets" (`Assessor.qualified`), not date fit; groups whose brief names a seniority also get role + seniority queries first (LLM-supplied title words per language) | `ranking/assessor.Assessor`, `adaptive.py`, `build_organisation_people_queries` |
| A provider account running out of credits, or rejecting the API key, mid-run | Fatal and explicit (`QuotaExhausted`: Serper "Not enough credits", Exa HTTP 402, Anthropic "credit balance"; `InvalidCredentials`: HTTP 401/403). The run stops with a message naming the key, instead of writing degraded results (out of credits was seen live twice before this guard) | `search/base.raise_if_unusable`, `llm.py` |
| LLM rate limits dropping work | SDK retries with backoff raised to 8 (two parallel briefs hit 80k output tokens/min and lost 16 pages with the default 2); failed calls are logged, not fatal | `llm.py` |
| "Ideal" roles treated as hard rules | Target roles and preferences are labelled "scores only" for the LLM; only hard requirements can reject (measured: 10 former employees wrongly rejected before, 1 after) | `RANK_SYSTEM_PROMPT`, `group_brief` |
| Customer staff with no link to the product | `target_evidence` rule (a source must name the target) plus the LLM's "direct experience" check against profile lines that name the target | `eligibility.py`, `candidate_extractor` |
| Cost growing with result volume | Early stop at 2× the ranking need, chunked enrichment, Exa as top-up, stage 5 in batches, page reading stops at 40 organisations | `adaptive.py`, `candidate_extractor.py`, `people_search/organisations.py` |

---

## 6. Why it generalises

The code contains no company names, countries, languages or group types. Everything that
differs between briefs comes from the parsed spec:

| Brief property | Comes from | Effect |
|---|---|---|
| Groups, their organisations and aliases | `BriefSpec.groups[].eligible_organisations` | Named → `people` mode with templates; none → `companies_then_people` |
| Target organisation | `BriefSpec.target_organisation` | Customer groups get case-study templates and target-name people queries; excluded-staff phrases |
| Employment rule and window | `required_employment_status`, `departure_dates` | Which statuses are useful; departure-quarter slices; Exa phrasing |
| Exclusions and their scope | `excluded_organisations` | Query exclusions; discovery skips only current-and-former exclusions |
| Languages | `query_languages` | Haiku writes company queries per language; Serper `hl`; validation warns on gaps |
| Role and topic words | Haiku, per group | Role keywords in people queries |
| Hard requirements, excluded roles, required evidence, preferences, topics | `hard_constraints`, `excluded_roles`, `required_evidence`, `preferences`, `relevant_topics` | Verification rules and the ranking rubric; the same weights score "relevance" against each brief's own topics |

**Tested on two very different briefs** with the same code:

- *Tungsten Automation* (English, software, one named target with an alias, named
  competitors, customers not named): former employees by slices, competitors by role,
  customers by case-study discovery.
- *Polish rail freight* (Polish and English, no target, no named organisations in any
  group, current-only exclusions of state companies): all three groups discover
  organisations first (Polish and English queries), then people at them. It needed two
  fixes, both general: organisations excluded only for current staff must stay
  discoverable, and former-staff groups must exclude each organisation's current staff
  (AGENT_LOG entry 8).

A new brief needs no code change: parse it, optionally review `brief_spec.json` (aliases,
windows) and `organisations.json` (switch off wrong organisations), and run.

---

## 7. Cost and performance

Every run records its cost per API and per stage at the top of `run_metadata.json`
(4.13). Prices are not in the code: token and credit prices come from `.env`
(`.env.example` is prefilled with list prices and their date), and Exa reports its own.

| Item | Price |
|---|---|
| Claude Haiku 5.5 | $0.10 / $0.50 per million input / output tokens (`LLM_PRICE_*`, as of 2026-10-06) |
| Serper (Google) | ≈$0.001 per credit = one page of 10 results (`SERPER_PRICE_PER_1K`) |
| Exa search | ≈$0.001 per result returned (≈$0.047 for 50), exact from `costDollars` |
| Exa `/contents` | $0.001 per page read, exact |

### What a full run costs and how long it takes

Measured on the fresh full runs of 2026-10-11:

| Cost | Tungsten | Polish |
|---|---|---|
| Serper | 225 credits, **$0.23** | 421 credits, **$0.42** |
| Exa search | 5 queries, **$0.11** | 25 queries, **$0.75** |
| Exa `/contents` | 303 pages, **$0.30** | 1,170 pages, **$1.17** |
| Haiku | 124 calls, **$0.10** | 394 calls, **$0.31** |
| **Total** | **$0.74** | **$2.65** |

The Tungsten run fetched 3 Google pages of every X-ray; with page 1 first (4.3) its search
stage drops to 54 Serper requests and 45 s. The Polish run already used page 1 first: its
two short groups fetched later pages for 92 X-rays (22 for infrastructure, 70 for freight),
which added 3 qualified freight operators.

| Time per stage | Tungsten | Polish |
|---|---|---|
| 1. Brief parsing | 39 s | 26 s |
| 2. Query generation | 11 s | 13 s |
| 3. Search | 1m 55s (45 s with page 1 first) | 1m 37s |
| 4. Candidate extraction (+ later pages, Exa top-up) | 60 s | 2m 34s |
| 5. People at discovered organisations | 54 s | 10m 48s |
| 6. Profile lookup | 34 s | 31 s |
| 7-8. Resolution and verification | under 1 s | 3 s |
| 9. Ranking | 17 s (most verdicts made while searching) | 24 s |
| **Total** | **5m 30s (≈5 min with page 1 first)** | **16m 35s** |

- **Where the money goes:** about 70% is Exa: reading profiles and pages, and its search.
  Haiku is under 15%.
- **Where the time goes:** page reading (8 parallel Haiku workers shared by all groups),
  enrichment, and stage-5 batches, which run one after another within a group because
  each batch decides whether the next runs.
- **Why Polish costs more and takes longer:** every group discovers organisations first,
  and the thin infrastructure group never reaches the stop target, so it searches every
  organisation in stage 5, judging new candidates after each batch.
- **Re-running only the shortlist stages** (`--candidates`) costs $0.00-0.10 and takes
  under a minute; cached verdicts make an unchanged re-rank free.
- **Variation:** runs vary by about ±20%, because Google results drift.

### Strategies applied

The pipeline used to collect far more candidates than ranking needs (Polish terminal
operators 381, Tungsten competitors 250). Making each group search only until it has
enough cut Tungsten from $1.38 to $0.87 and Polish from $3.20 to $1.94, with every group
still at or near its target:

| Strategy | Effect |
|---|---|
| Stop a group at `TARGET_CANDIDATES_PER_GROUP` (25) *qualified* candidates: verified + LLM "meets" | Ample groups stop after a few chunks (Tungsten competitors read 50 of 315 profiles); thin groups keep searching |
| Read best-ranked first, in chunks of 50, enriching only the chunk read | Unread Google hits cost nothing |
| Exa people search only as a top-up for groups Google left short | Most named-organisation groups never run it |
| Stage 5 in batches of 10 organisations, stopping at the target | Terminal operators had 99 useful people from the first 10 |
| Stop reading discovery pages at 40 organisations (cap 150) | The first 100 pages held 84-91% of the useful matches |
| LinkedIn X-rays fetch page 1 first; pages 2-3 only for a group still short, one page at a time | Tungsten search stage 126 → 54 Serper requests and 1m 55s → 45 s; earlier, the 3-page cap took it from 294 credits to 140 |
| Reuse cached LLM verdicts across runs | Re-ranking unchanged candidates makes no LLM calls |

### Measured and rejected

| Lever | Result | Decision |
|---|---|---|
| Exa `auto` instead of `deep` for company discovery (probe 2026-10-11: 20 queries per type, top 15 pages each read by Haiku) | `auto` is ≈5x faster (≈3 s vs ≈14 s per query) and found more organisations in the three ample groups (31 vs 28, 48 vs 40, 51 vs 38). But it costs about twice as much (`deep` returns fewer results: $0.09-0.15 vs $0.23 per group), and it found far fewer in the thin infrastructure group (14 vs 25), the only group that needs more | **keep `deep`** |
| Message Batches API for page extraction (probe 2026-10-11: 120 of the same pages) | Half price ($0.040 vs $0.080) with the same organisations (123 vs 134 found, 111 in common, within the run-to-run variance of the same prompt). But the batch took 140 s against ≈60 s for the same pages with 8 parallel calls, and batches may take up to an hour. Extraction reads in chunks with a stop check after each, so every chunk would wait for a batch. The saving is $0.03-0.15 per run (Haiku is under 15% of the cost) | **not adopted**; worth it for a background mode at volume |
| Haiku `effort: low` for page reading | 25-35% cheaper per page, but missed PKP PLK on 5 of 12 pages | rejected |
| Fewer Exa results per query | The useful share does not fall with rank (68% at ranks 1-10, 71% at 26-50) | rejected |
| Drop Exa company discovery | It found organisations Google missed (22 customers, 34 freight operators) | rejected |
| Prompt caching | Shared prefix is ≈1-2k tokens and Haiku is under 15% of the cost | not worth it |

### Not yet applied

| Lever | Expected effect | Why not yet |
|---|---|---|
| Local cache of enriched pages and search results | Re-runs of a brief almost free for Exa, the largest cost | Needs an expiry policy; profiles change |
| Run both Exa types for thin groups only | Infrastructure: 39 organisations from both vs 25 from `deep` (probe above), for ≈$0.24 more | The extra organisations still need a people search to pay off |
| Skip enriching Google hits whose snippet shows no link to the organisation | Polish infrastructure: 480 of 579 candidates had no relevant position (≈$0.4) | Snippets are unreliable; needs measuring first |
| Lower the target to 20 qualified candidates | ≈20% less enrichment in ample groups | Leaves no margin for merges and borderline verdicts |

### Future option: a fast decision model for verdicts (Jev)

**Where it fits:** only the `Assessor` verdict and its 0-5 scores. That verdict is used by
the early-stop gate (4.4-4.6) and by ranking (4.10). Every step that writes text stays
with Haiku.

| Pipeline step | Haiku (today) | Jev (future option) |
|---|---|---|
| Early-stop gate verdict | Yes: ≈10 s per call, and the search waits for it | Yes, sub-second: the best fit |
| Ranking verdict and scores | Yes | Yes, with a confidence value; low-confidence cases go to Haiku |

**Why we kept Haiku:**
- **The saving is cents.** Most of a run's cost is Exa (≈70%).
- **Another vendor.** It means a second API key and bill, another processor of profile
  data, and more failure handling.
- **Haiku is still needed** for reasons, so Jev would add a second model rather than
  replace one.
- **Development time:** a second `Assessor` backend, a confidence threshold, and an
  evaluation to set that threshold.

**What Jev could bring later:**
- **A faster early-stop gate:** minutes off thin-group runs, and checks after smaller
  batches, so less is read beyond the target.
- **Confidence-based routing:** confident verdicts from Jev; uncertain ones go to Haiku
  or a researcher.
- **Lower verdict cost at volume:** roughly 7x cheaper per verdict.

---

## 8. Configuration (`config.py`, `.env`)

Every setting is declared once, as a field of the settings dataclasses in `config.py`
with its default and a comment; `from_env` takes the default from the field and only adds
the allowed range. `.env.example` lists every variable with its default, grouped as
below; copy it to `.env` and add the API keys.

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY`, `SERPER_API_KEY`, `EXA_API_KEY` | — | Credentials (never committed; `.env` is gitignored) |
| `LLM_PRICE_IN_PER_MTOK`, `LLM_PRICE_OUT_PER_MTOK` | blank (prefilled 0.10 / 0.50 in `.env.example`) | USD per million tokens for `LLM_MODEL`; blank = LLM cost unknown |
| `LLM_PRICES_AS_OF` | blank (2026-10-06 in `.env.example`) | Date the prices were taken, recorded with each run |
| `SERPER_PRICE_PER_1K` | blank (1.0 in `.env.example`) | USD per 1,000 Serper credits; blank = Serper cost unknown |
| `LLM_MODEL`, `LLM_EFFORT` | `claude-haiku-5-5`, `medium` | Model and effort for every LLM call |
| `TARGET_CANDIDATES_PER_GROUP` | 25 | Qualified candidates (verified + LLM "meets") per group after which searching and enriching stop (0 = never) |
| `QUALIFY_WITH_LLM` | true | false: count date fit instead of qualified candidates (no LLM calls while searching) |
| `QUERIES_PER_PROVIDER` | 5 | Max Exa queries and company-discovery queries per provider per group |
| `XRAY_QUERIES_PER_GROUP` | 20 | Max Google X-rays per named-organisation group |
| `ORG_PEOPLE_QUERIES_PER_GROUP` | 40 | Max organisations searched in stage 5; also how many organisations page reading aims for |
| `SERPER_MAX_PAGES` | 10 | Google pages per company query |
| `SERPER_PEOPLE_MAX_PAGES` | 3 | Most Google pages per LinkedIn X-ray; page 1 first, later pages only for groups still short |
| `EXA_NUM_RESULTS` | 50 | Exa results per query |
| `EXA_PEOPLE_SEARCH_TYPE`, `EXA_COMPANY_SEARCH_TYPE` | `auto`, `deep` | Exa search types |
| `EXA_TEXT_MAX_CHARS` | 4000 | Page text kept per Exa result |
| `EXTRACTION_MAX_PAGES_PER_GROUP` | 150 | Hard cap on discovery pages read per group |
| `EXTRACTION_BATCH_SIZE` | 50 | Profiles/pages enriched and read between early-stop checks |
| `EXTRACTION_LLM_WORKERS` | 8 | Parallel Haiku calls |
| `EXTRACTION_ENRICH` | true | Fetch content for Google hits via Exa `/contents` |
| `MAX_EXPERTS_PER_GROUP` | 20 | Experts returned per group |
| `RANK_POOL_PER_GROUP` | 60 | New LLM verdicts per group at ranking (best code-scored first); candidates with a cached verdict are always ranked as well |
| `RANK_WEIGHTS` | role_fit 0.25, relevance 0.2, evidence_strength 0.25, recency 0.15, corroboration 0.1, seniority 0.05 | e.g. `role_fit=0.3,recency=0.1`; renormalised to 1 |
| `NO_PROFILE_PENALTY` | 25 | Points (of 100) taken off an expert with no profile URL; 0 = off |
| `MAX_IDENTITY_LLM_PAIRS` | 40 | Ambiguous identity pairs Haiku may judge per run |
| `PROFILE_LOOKUPS_PER_GROUP` | 60 | People named on pages whose LinkedIn profile is looked up, per group (0 = off) |

---

## 9. Code map

```
expert-case-study/
├── src/sourcing_agent/
│   ├── __main__.py               python -m sourcing_agent → cli.main
│   ├── cli.py                    arguments, logging setup, calls pipeline.run, prints the summaries
│   ├── config.py                 every setting, declared once with its default; read from .env
│   ├── llm.py                    Anthropic SDK wrapper: structured outputs, usage tracking, quota errors
│   ├── metrics.py                RunTracker: stage timings, every LLM call and search round, per stage
│   ├── adaptive.py               early stopping for stages 4-5: search → extract → merge rounds,
│   │                             the Exa top-up, "qualified" counting
│   ├── evaluation.py             precision with 95% intervals from a labelled final_results.csv
│   │
│   ├── models/                   every Pydantic record / artifact schema, one module per domain
│   │   ├── common.py             literals (providers, statuses, decisions), schema version
│   │   ├── brief.py              stage 1    BriefDraft (LLM schema), BriefSpec (validated)
│   │   ├── queries.py            stage 2    search terms, SearchQuery, QueryPlan
│   │   ├── search.py             stage 3    SearchHit, QueryRun, SearchResults
│   │   ├── candidates.py         stages 4-6 Evidence, Position, Candidate, organisations, profile lookups
│   │   ├── shortlist.py          stages 7-9 identities, verification, LLM verdicts, experts, FinalResults
│   │   └── run.py                usage, timings, run summary and cost breakdown, RunMetadata
│   │
│   ├── pipeline/
│   │   ├── orchestrator.py       run(): executes the stages in order, writes each artifact
│   │   ├── start.py              STAGES; which saved artifact a run resumes from (checked)
│   │   └── artifacts.py          artifact file names, loaders and writers
│   │
│   ├── brief/
│   │   └── parser.py             stage 1    Haiku → BriefDraft → BriefSpec (date windows, quote checks)
│   │
│   ├── search/
│   │   ├── query_generator.py    stage 2    mode per group, Haiku terms, the QueryPlan
│   │   ├── people_plan.py                   people-query plans from templates (slices, round-robin, stage 5)
│   │   ├── people_queries.py                Google X-ray and Exa query templates, exclusions, date slices
│   │   ├── company_queries.py               company-discovery queries: checks, customer-story templates
│   │   ├── scope.py                         which organisations a brief / group is about
│   │   ├── site_search.py                   Google site search that finds customer-story sections
│   │   ├── runner.py             stage 3    runs a QueryPlan on both providers; usage, summaries
│   │   ├── serper.py                        Google via Serper: paging, filtering, cost from credits
│   │   ├── exa.py                           Exa search: people/company types, entities, reported cost
│   │   └── base.py                          retries, canonical URLs, profile/junk filters, quota errors
│   │
│   ├── extraction/
│   │   ├── candidate_extractor.py stage 4   chunked enrichment and extraction with early stop;
│   │   │                                    organisation canonicalisation and merging
│   │   ├── enrichment.py                    Exa /contents client
│   │   ├── profiles.py                      profile → Candidate (entities by code, LLM fallback, snippets)
│   │   ├── pages.py                         discovery page → quoted people + organisations (Haiku)
│   │   └── orgs.py                          organisation-name normalisation and matching
│   │
│   ├── people_search/
│   │   ├── organisations.py      stage 5    per-organisation people queries, run in batches
│   │   └── profile_lookup.py     stage 6    the profile of people named on pages (name + organisation)
│   │
│   ├── resolution/
│   │   └── identity.py           stage 7    canonical identities, name normalisation, merges, LLM pair check
│   │
│   ├── verification/
│   │   ├── employment.py                    departure status, whole-month comparison, useful statuses
│   │   └── eligibility.py        stage 8    deterministic rules → eligible / ineligible / needs_review
│   │
│   ├── ranking/
│   │   ├── ranker.py             stage 9    pool → verdicts → scores → top-N experts with cited evidence
│   │   ├── assessor.py                      the LLM verdict (Haiku), batched, cached per dossier hash
│   │   ├── dossier.py                       what the LLM sees: positions, status, evidence ids; quote cleaning
│   │   └── scoring.py                       weighted fit score; recency and corroboration by code
│   │
│   └── reporting/
│       ├── progress.py                      live progress lines (stderr): stage start/end, numbers, cost
│       ├── console.py                       summary tables printed after a run (stdout)
│       ├── run_summary.py                   time and cost summary at the top of run_metadata.json
│       ├── review_sheets.py                 candidates.csv / organisations.csv review sheets
│       └── shortlist_files.py               final_results.md / .csv and verification.csv
│
├── tests/                        244 offline tests (fake LLM and fake search clients, no API calls)
├── briefs/raw/                   the two case-study briefs (Markdown)
├── results/<brief_id>/           every artifact of a brief's runs (README "Outputs")
├── .env.example                  every setting with its default, and the prices for cost figures
└── pyproject.toml                package, dependencies, pytest and ruff configuration
```

**Layering.** `models`, `config`, `llm` and `metrics` are shared infrastructure and import
no stage. Each stage package imports infrastructure and earlier stages' helpers, never a
later stage; `adaptive` is shared by stages 4-5; `pipeline` sits on top and is the only
module that knows the stage order; `reporting` formats what the others produce. `ruff`
(`F`, `I` rules, `pyproject.toml`) checks undefined names, unused imports and import order.

---

## 10. Testing

`pytest` runs 244 offline tests: brief parsing and quote checks, date windows, query
templates (exclusions, 32-word trimming, quarters, round-robin), company-query
validation and customer-story discovery, Serper paging and Exa parsing against mocked
HTTP, filtering, profile/page extraction with fake LLM output, organisation matching
(legal forms, acronyms, Polish endings, subsidiaries), departure statuses, review
exports, the early stops (profiles, pages, top-up, stage-5 batches), entity resolution
(name normalisation, URL merges across groups, page-to-profile merges, same name without a
shared employer kept apart, LLM pair decisions), every verification rule (window edges,
current staff, alias entities, stale headlines, excluded-role patterns, customer
evidence, current-only exclusions keeping former staff), ranking (ineligible never
returned, score follows the weights, cited quote copied, cap), evaluation metrics, and
pipeline resumes from each artifact including `--candidates`. Live behaviour is checked by full runs on both briefs, recorded in
AGENT_LOG.

---

## 11. Known gaps and next steps

- **Polish infrastructure planners return 0, with 8 listed for review.** The brief wants
  former network planning, train path or access charge *leads* at PKP PLK or UTK, so
  each expert must show a lead role, one of those three functions, and one of those two
  bodies. The group never hit its stop target: it searched all 17 discovered
  organisations (56 Google and 5 Exa queries). Of 431 candidates:
  - 376 had no job at those bodies, 28 still work there, and 7 have no end date;
  - **12 of the remaining 20 were rejected**: specialists rather than leads, other
    functions (track diagnostics, dispatching), regional or private bodies, and one false
    match on "PLK";
  - **8 are unclear**: senior former PKP PLK staff such as division heads ("Naczelnik
    Wydziału") and a former CEO. Their profiles don't say what the unit did, so the
    function can't be confirmed. They are listed for review, never counted as experts.

  To fill the group: a researcher screens the 8; the reviewed spec names PKP PLK and UTK
  directly; and the search adds Polish department names and conference or regulator
  pages.
- **LLM judgements vary a little between runs** (Polish freight returned 9 and then 11
  with the same candidates; Haiku 5.5 does not accept a temperature setting). Within a
  run each person is judged once and the verdict reused; across runs, verdicts are
  cached by a hash of prompt, group definition, dossier and model, so re-ranking
  unchanged candidates gives identical answers at no cost.
- **Precision is measured on one brief only.** Tungsten: 60 of 60 valid (95% interval
  94-100%), one reviewer (section 4.11); the Polish brief is not labelled yet.
- **Staff-level extras** (refinement loop, streaming, learning from rejections) and the
  design note are not built.
- **The LLM's group check can be strict or lenient at the margin** (e.g. "product
  leader" vs an individual product manager). Its reasons are recorded per candidate in
  `final_results.json` → `assessments` and `verification.csv`, so a reviewer can see and
  overrule them.
- **Google results drift** between runs (the same queries found 33 and then 28 in-window
  former employees).
- **A faster verdict model (Jev) is a future option.** It would make the early-stop gate
  and ranking verdicts sub-second. Haiku stays for now (section 7, "Future option").

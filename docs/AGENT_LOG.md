# AI coding agent log

**Tool:** Claude Code (VS Code extension) with Claude Opus 5.5. The pipeline itself calls
Claude Haiku 5.5. Built over two days, 2026-10-10 and 2026-10-11.

**How I worked:** I set the direction and the acceptance bar for each step. The agent
measured before it built: it ran live API probes before writing an adapter and offline
simulations on saved runs before changing a strategy. Each step ended with offline tests
and a live run that I read by hand.

**Where it ended:**
- 244 offline tests.
- Tungsten: 60 experts, 60 of 60 valid when hand-labelled (95% interval 94-100%), $0.74
  and 5m 30s per full run.
- Polish: 40 experts, $2.65 and 16m 35s, with no code written for that brief.

| # | Step | What I asked | What it got wrong | How I verified | Outcome |
|---|---|---|---|---|---|
| 1 | Pre-planning and plan | Survey search, page-reading and extraction tools; sense-check Google X-rays and Exa by hand; then a plan for Senior on Tungsten, with the Polish brief as the generalisation test | The plan chose Brave for X-rays without probing it, though my hand checks had used Google; it planned to fetch profile pages; its spec assumed one target company; it missed three stages | Hand-run Google X-rays returned real former employees; the plan was checked against the case-study levels and both briefs, and at the end against its own definition of done | The design rules held; 6 departures, each driven by a measurement (Serper, Exa `/contents`, the spec model, 3 new stages) |
| 2 | Brief parsing | Haiku turns a brief into a structured spec | Stored the customers' *defining* criterion as a preference (0 hard rules); read the 6-month rule differently on two runs | Read every rule by hand; checked each verbatim quote against the brief | Parser prompt rewritten; later stages load a reviewed spec |
| 3 | Queries | Separate LLM query stage, then a split into company and people queries | 3 of 5 competitor queries named no competitor; a third of Polish "company" queries were people searches | Read all 60 queries; 51 tests with exact expected strings | People queries built from code templates; LLM writes only company queries |
| 4 | Search engines | Brave + Exa, then **my call**: replace Brave with Google via Serper | Brave corrupted or ignored the X-ray operators; my own Google pattern returned 0 results | Live probes before writing each adapter | Serper adapter, probe-verified operators; $1.63 → $0.64 per run |
| 5 | Extraction | My hunch: Haiku reads the search result pages | The hunch didn't fit the data: results are LinkedIn URLs that can't be fetched; Haiku invented a placeholder employer | Probed Exa `/contents` on 15 profiles: 15 of 15 returned dated work history | Code parses profiles; Haiku reads only web pages, with quote and name checks |
| 6 | Former-employee recall | At least 20 in-window former employees | Agent miscounted an Exa probe; a slice-merge bug overwrote a quarter; `deep` was worse for people search | Four measured query rounds (≈$1.2), scored by people no other query found | Departure-quarter slices: 4 → 28 in-window former employees |
| 7 | Customers | Find customers where they are named, then the people at them | Generic queries scattered (34 customers, placeholders); the vendor's listing page renders in JavaScript | Probes of listing pages and of `site:` paging; 150 tests | 131 named customers from 96 case studies; new stage: people at discovered organisations |
| 8 | Polish brief | Check the flow generalises, then run it | Agent's own regression: it skipped organisations excluded only for *current* staff, the very employers of the target groups; former-staff searches didn't exclude each organisation's current staff | Read the code against the Polish spec before running; full live run | Fixed generally; acronym and name-variant merging (41 → 37 organisations) |
| 9 | Cost | Cut cost, keep 20 great results per group, switch off weak engines | The money was elsewhere than assumed (Exa, not the LLM); `auto` isn't cheaper than `deep`; every X-ray fetched 3 pages | Offline simulations on saved runs; probes of Haiku effort, Exa type, Batches API | Adaptive early stop, Exa as a top-up, page 1 first: Tungsten $1.38 → $0.74, Polish $3.20 → $2.65 |
| 10 | Dedup, verification, ranking | Entity resolution, code verification, LLM ranking with sourced reasons | LLM treated "ideal" roles as hard rules; the early stop counted dates, not role fit; ranking dropped 'meets' verdicts; a run continued through Exa out-of-credit errors | Read every LLM rejection; automated checks on the returned list | Three-way verdicts, cached; stop on qualified people; out-of-credits is fatal |
| 11 | Profile lookup | Customer "profile" links were case-study pages: fix it; give no-profile experts a big minus | No stage looked up page-named people; ranking filled the profile column with any web page; acceptance missed past employers | Live probes of lookup queries; every work-history acceptance checked by hand | New stage 6; Tungsten customers with a profile 7 → 20 of 20 |
| 12 | Evaluation | Precision from my hand labels | Nothing material; the list changed between runs, so labels went stale | I opened every profile and evidence link | 60 of 60 valid; Wilson intervals; labels re-run on the final list |
| 13 | Code quality | Package structure, progress logs, per-API cost, prices in `.env` | Broken relative imports when splitting files; a re-run emptied the lookup file; settings declared three times | Import every module; ruff; tests for each bug | Clean packages, one declaration per setting, cost per API and stage in `run_metadata.json` |

---

## 1. Pre-planning and plan

### Pre-planning: what can the web give us?

Before writing the plan, I checked which tools could find people and read their pages,
and tried the searches by hand.

**Tools surveyed:**

| Job | Tool | What I found |
|---|---|---|
| Search | Brave | About $5 per 1,000 requests; no content extraction; no free tier |
| Search | Exa | $7 per 1,000 searches with page contents for 10 results; a `people` category of over a billion public profiles; reportedly 1,000 free searches a month |
| Search | Tavily | Search and extraction in one call; 1,000 free searches a month |
| Search | Serper, SerpAPI | Google results through an API; I didn't check pricing then (Serper turned out to be $1 per 1,000 credits) |
| Read pages | Jina Reader | Prefix a URL with `r.jina.ai` to get Markdown; can return incomplete content on JavaScript-heavy sites |
| Read pages | Firecrawl | Search, scrape and crawl, with JavaScript rendering |
| Read pages | Own `fetch.py` | Free, but fails on login walls and JavaScript pages |
| Extract | Claude API | Reads page text and returns structured JSON |

**Google X-rays by hand.** I tried LinkedIn X-ray searches on Google to see whether
former Tungsten employees could be found at all. Two patterns gave relevant profiles:

```
site:linkedin.com/in "Tungsten Automation" -intitle:tungsten -intitle:kofax -"Tungsten Automation · Present"

site:linkedin.com/in ("Tungsten Automation" OR "Tungsten Network" OR "Kofax")
  -intitle:tungsten -intitle:kofax -"Tungsten Automation · Present" -"Tungsten Network · Present"
  -"Kofax · Present" -"Tungsten Automation, a Kofax company · Present"
  -"Tungsten Network, a Kofax company · Present" -"at Tungsten" -"at Kofax"
```

- They returned real former employees, such as Brad Hamilton and Chris Quish. Brad
  Hamilton was later found by the pipeline too.
- The idea worked: people who list the company in past experience but not in their title
  or as current.
- The second query showed the renamed-company problem early: the company appears as
  Tungsten Automation, Tungsten Network, Kofax, and "a Kofax company".

**Exa by hand.** I ran the same groups through Exa to see its results and learn how to
prompt it:
- natural-language descriptions of the person worked better than keyword lists;
- the `people` category returns profile pages.

That shaped the plan's split: keyword X-rays for precision, Exa for semantic discovery.

### The plan

- **Why plan first:** the case study grades many things at once, including real experts,
  exclusions with renamed companies, dedup, evidence, precision, cost and a second brief
  that must run without code changes.
- **Scope:** Senior level on Tungsten, with all three groups, two providers, ranking with
  evidence, measured precision and cost per run. The Polish brief is the acceptance test
  and must run with no pipeline changes.
- **Rules it set:**
  - brief-specific rules live in a spec, not in code;
  - code enforces dates, exclusions, dedup and limits; the LLM only parses, writes search
    terms and reads unstructured pages;
  - hard constraints are kept apart from preferences;
  - exclusions match an explicit alias list (Tungsten = Kofax);
  - every claim keeps its source, and unknown stays unknown;
  - each stage reads and writes a file, so a run can resume from a reviewed artifact;
  - evaluation is a deliverable, and no recall is claimed.
- **Tool choices:**
  - Brave for keyword X-rays;
  - Exa for semantic search;
  - Claude Haiku for extraction;
  - no LinkedIn scraping (terms of service);
  - prices kept in config.

### What the plan got wrong

| The plan said | What happened | Entry |
|---|---|---|
| Brave for keyword X-rays | Brave corrupted or ignored the X-ray operators. I had sense-checked on Google but chose Brave on paper without probing it, so the switch to Google via Serper came only after building the Brave adapter | 4 |
| Fetch and read pages for profiles (`httpx` + `trafilatura`) | LinkedIn can't be fetched; Exa `/contents` returns dated work history that code parses | 5 |
| Exa people search with the departure window in the query text | Exa ignores "former": 18 of 25 results still worked there, so code filters on work-history dates | 4 |
| A single `target_name`; `dict` fields; ranking weights in the LLM-parsed spec | The Polish brief has no target; `dict` fields can't go in a structured-output schema; LLM-set weights would let the model invent scoring. The target became optional, constraints typed, weights moved to config | 2 |
| Per-brief ranking weight tables | One set of weights, scored against each brief's own topics, plus a no-profile penalty | 10, 11 |
| Seven stages | Three more were needed: people at discovered organisations, profile lookup, and an early stop on qualified candidates | 7, 9, 11 |
| Brief paths `briefs/tungsten_automation.md` | The repo uses `briefs/raw/*.md` | – |

### How the plan was checked

- **Before building:** I read it against the case-study levels (Mid, Senior, Staff) and
  both briefs. The Polish brief exposed the single-target assumption only once stage 1
  was built.
- **During the build:** each stage's first live run was read by hand. Every departure in
  the table above came from a probe or a measurement, not a change of mind.
- **At the end, against its own definition of done:**

  | Definition of done | Status |
  |---|---|
  | All three Tungsten groups end to end, each with its own search | Met: 20 / 20 / 20 |
  | Exclusions and aliases enforced; nobody twice | Met: 0 violations and 0 duplicates in the labelled list |
  | Up to 20 per group with sourced reasons, never padded | Met: Polish infrastructure returns 0 and lists 8 for review instead of padding |
  | Precision measured on a labelled sample | Met: 60 of 60 (95% interval 94-100%) |
  | Time and cost per run | Met: per API and per stage in `run_metadata.json` |
  | Tests for the critical rules | Met: 244 offline tests |
  | Polish brief with no pipeline changes | Met: no Polish-specific code. The run did expose two general fixes (entry 8) |
  | README, results and an honest agent log | Met |

- **Lesson:** the tool I had tried by hand was the one that worked. From entry 4 on,
  every provider was probed live with the real queries before an adapter was written.

## 2. Brief parsing

**Goal:** turn a free-text brief into a structured spec that every later stage reads, so
no brief-specific rule lives in code.

**How it works:**
- One Haiku call with structured outputs (`messages.parse` and the `BriefDraft` schema).
  The prompt asks for extraction only:
  - no organisations or aliases from the model's own knowledge;
  - an alias only when the brief itself says "formerly X";
  - time windows in months, never calendar dates.
- Code (`build_spec`) turns months into calendar dates from the run date and checks every
  rule's quote against the brief.
- `brief_spec.json` holds:
  - groups, organisations and aliases;
  - exclusions with their scope (current staff only, or current and former);
  - departure windows, hard constraints and preferences;
  - open ambiguities;
  - the brief's SHA-256.

**What went wrong:**
- On the first live Tungsten run the customers group had 0 hard constraints. The sentence
  that defines a customer expert ("direct experience implementing or running the
  product") was stored as a preference, so anyone working at a customer would have
  passed. The cause was my prompt's "if unsure, make it a preference" rule.
- Two runs disagreed on whether the 6-month minimum covers competitors. The brief puts
  that rule under "Excluded companies", so it is genuinely ambiguous.
- The plan's spec model didn't fit:
  - a single `target_name` (the Polish brief has no target);
  - `dict` fields, which a structured-output schema can't hold;
  - ranking weights the LLM would have set.

**Fix:**
- A generic prompt rule, with no Tungsten wording:
  ```
  Before: If you are unsure, record it as a preference and explain the reading in ambiguities.
  After:  The criteria that define who belongs to a group are hard constraints for that
          group even when written as a plain description without "must". [...] For anything
          else you are unsure about, record it as a preference.
  ```
- The target became optional, constraints became typed fields, and the weights moved to
  config.
- Later stages load a reviewed `brief_spec.json` (`--spec`) instead of re-parsing.

**Guards:**
- Every rule carries a verbatim `source_quote`. Code checks it against the brief, and a
  rule whose quote isn't found becomes a `validation_warning`.
- An exclusion that points at an unknown group is widened to all groups: over-excluding
  is the safe failure.
- Every later file records the brief's hash and is refused if it doesn't match.

**Verified:**
- I read every rule on both briefs by hand against the plan's eligibility rules.
- 0 quote warnings on either brief.
- Offline tests with a fake LLM, including month-end and leap-year date maths.

**Outcome:**
- After the fix: `customers HARD: ["Must have direct experience implementing or running
  Tungsten Automation's product in an enterprise."]`. "At meaningful volume" stayed a
  preference and was flagged as undefined.
- The Polish brief parsed with no code changes:
  - languages `en, pl`;
  - all four exclusions current-staff only;
  - "8+ years" and "Polish expected" read as preferences.
- One call: under $0.01, ≈30 s.

**Takeaway:** the LLM reads the brief once; code turns it into checkable rules, and a
human reviews the spec before anything is searched.

## 3. Queries: from LLM-written to templates

**Goal:** queries for each group that find the right people cheaply and come out the same
on every run.

**How it works:** the spec decides each group's mode.
- **Organisations named (`people` mode),** e.g. former Tungsten staff or competitor staff.
  Code builds Google LinkedIn X-rays from templates, and Haiku only supplies role and
  topic words. Current staff are excluded three ways:
  - one distinctive title word, `-intitle:tungsten`, because LinkedIn titles end with the
    current employer;
  - `-"at <name>"`, which catches "Title at Company" headlines;
  - `-"<name> · Present"`, which catches a current experience entry.

  Google reads at most 32 words, so keyword groups are trimmed first.
- **No organisations named (`companies_then_people` mode):** customers and all three
  Polish groups. Haiku writes 5 Google and 5 Exa company-discovery queries, and code
  validates them:
  - the count;
  - near-duplicates (token Jaccard ≥ 0.8);
  - missing languages;
  - people words;
  - whether the target is named, for groups defined by it.

**What went wrong:**
- **First design, where the LLM wrote every query:** 3 of 5 competitor Exa queries named
  no competitor, e.g. *"a document capture vendor's product leader"*. My coverage check
  missed it because it pooled both providers.
- The model added organisations the brief never names (Freightliner PL, Captrain Polska).
  I left that alone: it only widens discovery and never changes eligibility.
- After the split, about a third of the Polish "company" queries were really people
  searches ("former network planning manager at PKP PLK").
- A new default output folder overwrote an earlier `run_metadata.json`.

**My redirect:** a former-employee search is a formula (X-ray, every company name,
current staff excluded), so it belongs in code: exact, testable, free and stable between
runs.

**Fix:**
- People queries come from code templates; the LLM writes only company-discovery
  queries.
- A prompt rule plus a code check for people words in company queries.
- A rule that every query must name a group organisation. The competitor query became
  *"former ABBYY product leader"*.
- `run_history.jsonl` keeps every run's metadata.

**Guards:**
- Code checks on every LLM query, with warnings saved in `queries.json`.
- Caps per group: 5 Exa queries, 5 + 5 company queries, 20 Google X-rays.

**Verified:**
- I read all 60 queries by hand.
- 51 tests with exact expected X-ray strings; the real bad queries were kept as
  regression cases.

**Outcome:**
- The former-employee X-ray, exactly as a test asserts it:
  ```
  site:linkedin.com/in "Tungsten Automation" -intitle:tungsten -intitle:kofax
    -"at Tungsten Automation" -"at Kofax" -"at Tungsten"
    -"Tungsten Automation · Present" -"Kofax · Present" -site:linkedin.com/in/dir
  ```
  Slices (entry 6) add terms such as `("Jan 2025" OR "Feb 2025" OR "Mar 2025")`.
- Query generation: ≈12 s and under $0.01 per brief.

**Takeaway:** predictable queries are code, open-ended ones are LLM-written and then
checked by code.

## 4. Search engines: Brave → Google via Serper

**Goal:** one engine that honours X-ray operators exactly, plus one that finds people by
meaning.

**What went wrong:** the agent probed each API live before writing its adapter, Brave
first, then Google:

| Query part | Brave | Google (Serper) |
|---|---|---|
| `-intitle:"Tungsten Automation"` (multi-word) | corrupted the parsed query | **0 results** |
| `-intitle:tungsten` (single word) | works | works |
| `-"Kofax · Present"`, `-"at Kofax"` | ignored (18 of 20 still current staff) | works |
| Pagination | offsets 2-5 repeated page 2 while saying "more results" | 10 per page; `num` > 10 refused on the free plan |

- Part of my own Google pattern failed too: a multi-word `-intitle:` phrase returned
  nothing.
- **Exa's people search ignores "former":** by its own work-history data, 18 of 25
  "former employee" results still worked at Tungsten. It also rejects date filters.

**Decision (mine):** replace Brave with Google through Serper, because Google did better
in my own hand tests.

**Fix:**
- Templates use only operators that passed the probe, and one distinctive title word.
- Serper:
  - 10 results and 1 credit per page;
  - no country filter, since the briefs have none;
  - paging stops on an empty or short page, or one with no new URLs.
- Exa: `people` category with `type: auto` for people, `type: deep` for company
  discovery, exact cost from `costDollars`.
- People results are filtered to LinkedIn `/in/` profiles, and URLs are canonicalised.

**Guards:**
- Bounded retries on timeouts, 429 and 5xx errors.
- A failed query is recorded, not fatal.
- Employment status is always computed from Exa's work history, never trusted from the
  query wording.
- Offline tests with mocked HTTP:
  - paging stop rules, including the repeated page;
  - the 429 retry and auth errors;
  - no date filters sent to Exa people search.

**Verified:**
- 5 live Serper probes and 3 Brave probes.
- The Serper run compared per query with the saved Brave run.

**Outcome on Tungsten former employees:**
- candidates 246 → 140;
- in the window 4 → 6, plus 3 boundary cases;
- run cost $1.63 → $0.64.

Google gives fewer profiles per query (51 vs 196) but is far more precise, which moved
the recall problem to entry 6.

**Takeaway:** probe the operators with the real query before writing the adapter.

## 5. Candidate extraction

**Goal:** turn each search hit into a candidate with dated positions and evidence, without
inventing anything.

**My hunch:** Haiku reads the result pages.

**The agent's probe changed it:**
- Search results are LinkedIn URLs with thin snippets, and LinkedIn can't be fetched.
- Exa `/contents` returned 15 of 15 sample profiles with structured, dated work history,
  for $0.015.
- That settled cases at once:
  - Tony T. still worked at Tungsten despite the `-intitle:` exclusion;
  - Carrie Constantino had left in May 2026, inside the 6-month cooling-off.

**How it works:**

| Source | Method |
|---|---|
| LinkedIn profile with Exa work history (almost all) | Code parses employer, title, month-level dates and the current flag |
| Profile text without work history (≈2 in 1,100) | Haiku lists positions, each with a verbatim quote that code checks |
| Enrichment failed | Title from the snippet; status "unknown", never eligible on a snippet |
| Case study, press release, register | Haiku lists named people and organisations, each with a verbatim quote |

Hits are read in chunks of 50, best-ranked first, and only the hits actually read are
enriched.

**What went wrong:**
- **Hallucination:** Haiku returned the employer "IM (infrastructure manager, not named on
  the page)" with a quote that was real. A quote check alone is not enough.
- "Leading insurer (unnamed on the page)" passed for the same reason: its words were on
  the page.
- **Agent bug:** dotted legal forms ("S.A." splits into single letters) broke the
  organisation merge key, so "CLIP Group" and "CLIP Group S.A." stayed apart.

**Fix:**
- An organisation's name must appear on the page.
- `is_placeholder_org` rejects descriptions ("Regional US bank").
- `clean_org_name` strips rename notes ("Cerner, now Oracle Health" → "Cerner").
- A merge key that ignores legal forms.

**Guards, all in code:**
- Items whose quote isn't on the page are dropped, with a warning.
- `name_matches` compares whole tokens, never substrings ("IBM" ≠ "Mibmax"). It joins
  split names ("Open Text" = "OpenText") and matches acronyms ("UTK" = "Urząd Transportu
  Kolejowego").

**Verified:**
- Later, 135 of 136 Google URLs returned dated work history, for $0.14.
- 24 new tests, including organisation look-alikes that must not match.
- I read the candidates that matched no organisation.

**Outcome:** of 1,110 Tungsten profile candidates, 1,083 were parsed by code, 2 by Haiku
and 25 were snippet-only.

**Takeaway:** structured data over text, text over snippets; and the LLM never invents an
employer, because each claim needs its quote and its name on the page.

## 6. Former-employee recall: 4 → 28 in the window

**Goal:** at least 20 former employees who left 6 months to 3 years ago.

**Why it was hard:**
- A filtered X-ray returns ≈10 profiles. Page 2 comes back empty, and `start` repeats
  page 1.
- Role slices overlap: Brad Hamilton appeared in 5-7 of them.
- Exa ignores "former".

**What went wrong:**
- **Agent miscount:** it first reported 16 "real formers" from an Exa probe. The count
  included people who never worked there; re-measured, it was 4-6 per call. The config
  comment and the docs were corrected.
- **Exa `deep`, which I asked for:** for people search it returned at most 27 results in
  ≈12 s, against 50 in ≈2 s for `auto`, and no more former employees.
- **Boundary flags:** three good profiles were flagged "boundary". The window was
  day-exact, but LinkedIn dates are month-only, so "Apr 2026" could be any day in April.
- **Agent bug:** `runs[-2] += runs.pop()` read before the pop and wrote after it, so the
  Oct-Dec 2025 slice was overwritten by a duplicate of the Jan-Apr 2026 one.
- Google drifts: the same query set found 33 on one run and 28 on the next.

**Fix:**
- **Departure-quarter slices**, written the way LinkedIn writes dates:
  `"Tungsten Automation" ("Jan 2025" OR "Feb 2025" OR "Mar 2025")`. In one round they
  found 19 in-window people, 15 of them missed by every other query.
- These alternate with job-function slices (sales, engineering, services and others) and
  former-name slices (`"Kofax" ("2023" OR "2024")`).
- Product-name queries were dropped: they found people who list "Kofax" as a skill.
- A separate cap of 20 X-rays per group (≈$0.002 each); Exa stays at 5 queries.
- **My decision:** compare month-only dates in whole months.
- `deep` kept only for company discovery.

**Guards:**
- A regression test for the slice merge.
- Whole-month edge tests (Apr 2026 in, May 2026 out; Oct 2023 in, Sep 2023 out).
- Status always comes from work history.

**Verified:**
- Four experiment rounds through the real pipeline (≈$1.2), each query scored by the
  in-window people *no other query found*. A greedy set cover picked the mix.
- At 100 Exa results, every in-window find ranked in the top 22, so 50 is enough.

**Outcome, step by step:** 4 (Brave) → 6 (Google) → 9 (whole months) → 28 (slices) in
the window.

**Takeaway:** recall came from many differently sliced queries, chosen by measured
unique finds, not from deeper paging.

## 7. Customers: discovery, then people

**Goal:** the brief names no customers, so first find organisations that publicly use the
product, then people there with hands-on use.

**What went wrong:**
- **Diagnosis:** generic LLM-written company queries scattered over resellers, comparison
  sites and conference pages. That gave 34 customers, placeholders such as "Regional US
  bank" got through, and there was no people step at all.
- The vendor's case-study listing renders in JavaScript (Exa read 141 characters of it).
- Haiku didn't know the case-study path, and one query used the wrong domain
  (`tungsten-automation.com`, 0 results). The target-name check flagged it.
- People quoted on case studies came out "no relevant position", because they were
  extracted before the customer list existed.
- **Agent's wrong test assumption:** it expected organisations in insertion order, but
  they are sorted by evidence. The test was corrected, not the code.

**Fix:**
- **Discovery templates aimed at pages that name customers:**
  - the vendor's own case-study section, whose path code finds with one Google search
    (`site:tungstenautomation.com/case-studies` pages through 96 case studies, each
    titled with the customer);
  - case-study aggregators;
  - a generic "case study" query.

  Exa keeps the LLM's queries, and those found 22 customers Google did not.
- `organisations.json`, with an `include` flag a reviewer can switch off.
- **New stage 5, people at each discovered organisation:**
  - one X-ray per customer that must name the target (`"Kemira" ("Tungsten Automation"
    OR "Kofax")`);
  - no `-intitle:`, which would drop staff titled "Kofax developer";
  - run in batches of 10 until the group has enough.
- Page people are re-matched once the customer list exists.

**Measured and dropped:**
- Exa people search at customers: 249 profiles, 3 naming the target.
- A press-release template: 31 pages, 0 customers.

**Guards:**
- The `target_evidence` rule: working at a customer is not evidence of using the product,
  so a source must name the target. On the final Tungsten run 27 customer staff were
  rejected for that.
- The ranking verdict checks "direct experience" against the profile lines that name the
  target.

**Verified:**
- Live probes:
  - listing pages;
  - `site:` paging;
  - one query per customer (77 profiles on six customers) against shared queries (17).
- 150 offline tests and three live Tungsten runs.

**Outcome:**
- 131 named customers (was 34) and 107 people quoted in case studies.
- 115 customers with at least one relevant person.
- On the final shortlist, 20 customers, all labelled valid.

**Takeaway:** find the organisations where they are named, then search each one
separately, and demand evidence of use, not just employment.

## 8. Generalising to the Polish brief

**Goal:** show the design generalises to a brief with:
- no target company;
- no named organisations in any group;
- Polish and English sources;
- exclusions of state companies that apply to current staff only.

**What went wrong:**
- **Agent's own regression, caught before running.** Reading the code against the Polish
  spec, the agent found that page extraction skipped any organisation matching an
  exclusion. PKP PLK, PKP Cargo, DB Cargo Polska and UTK are excluded only for *current*
  staff, and their former staff are exactly who two groups want.
- **Former-staff searches** at discovered organisations didn't exclude each
  organisation's current staff.
- **Name variants wasted searches.** "PKP Polskie Linie Kolejowe S.A." / "PKP PLK" / "PKP
  Polskich Linii Kolejowych" (Polish case endings) and "Urząd" / "Urzędu Transportu
  Kolejowego" / "UTK" each counted as separate organisations. That was ≈10 of 41
  planner-group queries.
- The agent's first fix folded "PKP Cargo Terminale", a subsidiary, into "PKP Cargo". It
  caught this when re-checking the merged list.

**Fix, both general rather than Polish-specific:**
- Discovery skips only organisations excluded for current *and* former staff.
- Former-staff groups get the same three current-staff exclusions per organisation.
- Acronyms match the initials of a run of words, which also absorbs Polish case endings.
- Discovered variants fold into the brief's own names (`same_organisation`), using
  whole-name equality, so subsidiaries stay apart.

**Guards:**
- Tests:
  - acronyms with Polish case endings;
  - subsidiaries kept apart;
  - organisations excluded only for current staff stay discoverable.
- No brief names in code logic: a search of `src/` finds them only in comments,
  docstrings and examples.

**Verified:**
- The code was read against the Polish spec before spending on a run.
- An offline re-merge of the saved `organisations.json` measured the dedup before
  another live run.
- A full live run.

**Outcome:**
- Discovery found 108 freight operators, 118 terminal operators and 41 → 37
  infrastructure bodies (PKP PLK at 107 sources).
- Final run: freight 20, terminals 20, infrastructure 0 with 8 listed for review; $2.65
  in 16m 35s.
- Infrastructure is thin because the brief asks for former planning *leads*. Few have
  public profiles, and the list is never padded.
- Still open: English/Polish name pairs ("Office of Rail Transport" = UTK) need an alias
  in the spec.

**Takeaway:** the second brief needed two fixes, and both made the code more general,
not more specific.

## 9. Cost: measure where the money goes

**Goal:** cut cost while keeping 20 great results per group, and switch off weak engines.

**What went wrong:**
- **My assumption was off:** on the Polish $3.20 run, Exa was ≈75% of the cost and Haiku
  14% (almost all of it reading discovery pages).
- **For comparison:** an Exa people query costs ≈$0.047 for 50 results; a Google X-ray
  costs ≈$0.002.
- **No engine is useless everywhere:**
  - Exa added 0 useful customers;
  - but it carried the Polish infrastructure group (21 of 29 useful people were
    Exa-only).
- **Google got deeper:** it started returning 5-10 pages per X-ray, so one Tungsten run
  used 294 Serper credits and 283 s of search.
- **Pages fetched blindly:** later, every X-ray fetched 3 pages while extraction read only
  ranks 1-12.

**Rejected after measuring:**

| Lever | Why it was rejected |
|---|---|
| Haiku effort low | 25-35% cheaper per page, but missed PKP PLK on 5 of 12 pages |
| Exa `auto` for company discovery | ≈2x the cost of `deep` (Exa bills per result), and 14 vs 25 organisations on the thin group |
| Message Batches API | Half price, but 140 s vs ≈60 s, and the early stop would wait on every batch, for $0.03-0.15 saved |
| Fewer Exa results | The useful share didn't fall with rank |

**Applied:**
- **Adaptive early stop:** read the best-ranked hits first in chunks of 50, and stop a
  group at 25 *qualified* candidates (entry 10).
  - Discovery pages stop at 40 organisations.
  - What was not read is never enriched, and a warning records it.
- **Exa as a top-up:** it runs only when a group is still short after Google.
- **Stage 5 in batches** of 10 organisations, with Exa last.
- **Google page 1 first,** later pages only for groups still short, one page at a time.
  The agent's first version fetched pages 2 and 3 together.

**Guards:**
- Every run records its cost per API and per stage.
- Tests cover each stop rule, the top-up both ways and paging.

**Verified:**
- Offline simulations on saved runs, for free:
  - enrichments needed per group;
  - useful share by Exa rank;
  - organisations found per page read.
- Small live probes:
  - effort, ≈$0.03;
  - `auto` vs `deep`, $1.66;
  - Batches, $0.04.
- A search-only run for page 1 first ($0.18): 126 → 54 Serper requests, 1m 55s → 45 s.

**Outcome:**
- Tungsten $1.38 → $0.74 per run: Serper $0.23, Exa search $0.11, Exa pages $0.30,
  Haiku $0.10.
- Polish $3.20 → $2.65, with freight now filling 20 places; infrastructure is still
  thin.

**Takeaway:** measure where the money goes before cutting, and let the data switch an
engine on rather than a rule switching it off.

## 10. Dedup, verification and ranking

**Goal:** one record per person, a code decision for each candidate with the rule that
made it, and a ranked list where every reason is sourced.

**How it works:**
- **Entity resolution (`resolution/identity.py`):** records are clustered by:
  1. the same profile URL;
  2. the same name plus a shared employer, when one record is a page quote;
  3. Haiku, for ambiguous profile pairs only (budget 40).

  A name alone never merges two records. A person who fits two groups stays in both,
  marked `also_in_groups`.
- **Verification, code only:** seven rules, each recorded per candidate.
  - `identity`, `evidence` and `relevant_position`.
  - `employment_status`, which checks the window.
  - `exclusion`: a current role at an excluded company, *or* a stale headline "at
    <excluded>".
  - `excluded_role`: "C-level" maps to `chief … officer` and `CEO/CFO`.
  - `target_evidence`.

  Any failed rule makes the candidate ineligible, and any review result makes it
  `needs_review`.
- **Ranking:**
  - Code builds a dossier per eligible candidate: dated positions, status and numbered
    evidence items.
  - Haiku gives a verdict against the hard requirements only: `meets`, `unclear` or
    `does_not_meet`. It also scores role fit, relevance, seniority and evidence, and
    writes a reason that cites one evidence id.
  - Code copies that quote and link, and adds recency and corroboration.
  - Weights are the same for every brief.
  - Only `meets` is returned; `unclear` goes to a review list.

**What went wrong:**
- **The LLM treated "ideal" roles as hard rules:** the first pass rejected 10 Tungsten
  former employees in IT or marketing.
- **Design flaw:** the early stop counted date fit, not role fit. In the Polish "lead"
  groups only ≈25% of date-fit people were leads, so freight returned 11.
- Haiku 5.5 rejects a non-default temperature (checked in the docs), so verdicts varied
  between runs.
- **Agent bugs:**
  - the ranking pool dropped people already judged "meets" (freight returned 14);
  - NFKD folding turned "Paweł" into "pawe";
  - "Rob" / "Robert Duber" stayed apart;
  - escaped regexes in a shell heredoc became control characters.
- **Silent failure:** Exa ran out of credits mid-run (HTTP 402 on 1,722 of 2,102
  fetches), and the pipeline carried on to 0 eligible freight candidates.
- Two briefs run in parallel hit Haiku's rate limit and lost 16 pages.
- Quotes showed raw profile markdown, and once a phone number.

**Fix:**
- Target roles and preferences are labelled "scores only" for the LLM.
- **The gate:** a group stops at 25 candidates that pass code verification *and* get a
  `meets` verdict, which is the final list's own test.
- Verdicts are cached by a hash of the prompt, group definition, dossier and model.
- Cached verdicts always reach ranking.
- A shared tokeniser handles Polish letters, short first names are recognised, and a
  second blocking key was added.
- Out-of-credits is fatal (`QuotaExhausted` for Serper, Exa and Anthropic). The agent
  stopped the queued run before it overwrote good results, and restored them.
- SDK retries were raised to 8.
- Displayed quotes are cleaned, with phone numbers and emails redacted.

**Guards:**
- A ranker can't return anyone who failed a rule.
- A citation that isn't a real evidence id falls back to the strongest evidence item.
- Re-ranking from cached verdicts makes no LLM calls.
- Tests for every verification rule, including:
  - window edges;
  - stale headlines;
  - excluded-role patterns;
  - current-only exclusions.

**Verified:**
- I read every LLM rejection on both briefs.
- An automated check of the 91 returned experts found:
  - 0 current employees of excluded companies;
  - 0 duplicates;
  - every quote from the person's own sources.

**Outcome:**
- After the role fix, 1 rejection remained: an outside adviser.
- Polish freight 11 → 20.
- Tungsten 310 records → 294 people; Polish 1,420 → 1,380.
- "Tungsten Network, a Kofax company" counts as Kofax through whole-word matching, with
  nothing hard-coded.

**Takeaway:** code decides who is allowed, the LLM judges fit and writes the reason, and
code makes sure the quote behind it is real.

## 11. Profile lookup and the no-profile penalty

**Goal:** every expert should have a profile a client can open. People named only on case
studies need theirs looked up.

**What went wrong:**
- **Agent design gaps:**
  - nothing looked up the profile of people named on pages (0 of 46 Tungsten page-named
    customers had one);
  - ranking quietly filled the profile column with the first web page, so case-study
    pages looked like profiles.
- **Dossier evidence cap:** dossiers kept 6 evidence items, dated records first. Long
  profiles pushed out the case-study quote, and three customers fell from "meets" to
  "unclear".
- **Missed matches:**
  - acceptance required the organisation in the search snippet, but a past job is rarely
    there (Jakub Styczeń's result shows only his current CEO role, and three profiles
    share his name);
  - a quoted name with diacritics and a quoted "S.A." returned nothing, while LinkedIn
    spells him "Wojciech J. Skrzypinski";
  - the first round missed "Rob" for "Robert".
- **Caught on inspection:** a looser match accepted the wrong person ("Rail Polska" vs
  "DB Schenker Rail Polska").

**Fix: new stage 6.**
- **Up to three Google queries,** each run only if the earlier ones settled nothing, with
  the organisation searched without its legal form:
  1. the quoted full name;
  2. the name unquoted and without accents;
  3. surname and organisation.
- **Three acceptance tiers,** recorded as `accepted_on`:
  - the search result names the person and the organisation;
  - the profile's work history includes the organisation itself (`same_organisation`, not
    containment);
  - the profile's own text names it.
- The profile column holds only real profiles. Evidence of group fit goes first in the
  dossier.
- **My call:** a 25-point penalty (out of 100) for experts with no profile, applied in
  code after the verdict.

**Guards:**
- Exactly one profile must pass; two or more are "ambiguous" and none is taken.
- Same-name profiles read for checking never join the candidates.
- `LOOKUP_VERSION` makes re-runs retry only people not found before.
- Tests:
  - containment is not enough;
  - a profiled expert outranks a higher-scored page-only one;
  - a penalty of 0 restores the old order.

**Verified:**
- Live probes: the unquoted, accent-free query finds Skrzypinski at rank 1.
- I checked every work-history acceptance by hand, e.g. Brian Harris: "Executive
  Director, Web Operations & ECM Solutions, Loma Linda University, 2008-2013", the role
  the case study names.
- Both lookups were redone from backups after the loose match.

**Outcome:**
- Tungsten customers with a profile went 7 → 13 (lookup) → 18 (penalty) → 20 of 20 on
  the final run.
- 9 qualifying customers without a profile now score 16-41, below the lowest returned
  (55).
- All 60 Tungsten and 40 Polish experts have a LinkedIn profile.

**Takeaway:** find the person by name, accept only on a code-checked match with exactly
one candidate, and rank uncontactable experts last.

## 12. Evaluation

**Goal:** measure precision instead of asserting it.

**How it works:**
- **Labelling:** I label each expert in a copy of `final_results.csv` on seven checks,
  each `yes` / `no` / `unknown`:
  - identity;
  - role;
  - organisation relationship;
  - dates;
  - exclusions;
  - evidence supports the claim;
  - overall eligible.
- **Scoring:** `evaluation.py` reports per group and overall:
  - precision;
  - exclusion-violation rate;
  - claim-support rate;
  - duplicates (from `identity_id`, no labels needed).

  Every rate comes with its sample size, a 95% Wilson interval and an unknown count.

**What went wrong:**
- Nothing in the code. But the list changed as the pipeline improved, so labels went
  stale. Of the first labelled list, 47 of 60 were still listed. Of the 13 dropped, 11
  were still "meets" (ranked 21st or lower) and 2 had flipped to "unclear".

**Fix:**
- The sheet was rebuilt from the final run: 24 labels carried over, and 36 new rows
  prefilled `yes` for me to review.
- I opened every profile and evidence link.

**Outcome:**

| Group | Labelled | Precision | 95% interval |
|---|---|---|---|
| Former employees | 20 | 100% | 84-100% |
| Customers | 20 | 100% | 84-100% |
| Competitors | 20 | 100% | 84-100% |
| **All** | **60** | **100%** | **94-100%** |

0 exclusion violations and 0 duplicates.

**Limits I'd state:**
- One reviewer and one brief.
- Prefilled labels can anchor the reviewer.
- Per group, the evidence supports "very likely above ≈84%", not 100%. Ruling out a 1 in
  20 error rate would need ≈60 labels per group.
- No recall claim, because there is no complete list to compare against.
- Polish is not labelled.

**Next:** a Polish sample, a second reviewer on ≈15 experts, and a look at the `unclear`
review list to see whether the verdict is too strict.

**Takeaway:** 60 of 60 with an honest interval, and its limits stated.

## 13. Code quality and run reporting

**Goal:** a codebase a reviewer can follow, and runs that report their own time and cost.

**What went wrong (agent slips, each caught by a check):**
- A file split copied relative imports verbatim, so a module moved one level deeper
  imported `.models`. Caught by importing every module.
- `git mv` failed on untracked files mid-move.
- The profile-lookup stage rewrote its file on every run, so a re-run with nobody left to
  look up emptied it.
- Settings defaults were declared up to three times, and LLM prices lived in a code
  table.
- The first cost summary rounded small costs to $0.00.

**Fix:**
- **Packages by stage:** `brief/`, `search/`, `extraction/`, `people_search/`,
  `resolution/`, `verification/`, `ranking/`, `reporting/`, `pipeline/`, `models/`.
- **Progress logs:** one line per stage with duration and running cost (`-q` hides
  them, `-v` adds detail).
- **`run_metadata.json`** opens with a readable summary: total cost split by API, then
  time and cost per stage. `run_history.jsonl` keeps every run, including failed ones.
- **Prices only in `.env`,** with one declaration per setting.
- **Resume points:** any saved file can restart a run (`--spec`, `--queries`,
  `--organisations`, `--candidates`), so a re-rank costs cents.

**Guards:**
- Tests:
  - costs add up per API and per stage;
  - an unknown cost shows as "$?" rather than being understated;
  - a failed run says so;
  - a re-run keeps the lookup file;
  - prices come only from env.
- `ruff` (pyflakes and import order) clean.

**Verified:** a live run in a scratch folder: $0.095 in 1m 03s, split Anthropic $0.007,
Serper $0.073, Exa $0.015.

**Outcome:** 244 offline tests that spend no credits, and every figure in the docs traced
to a `run_metadata.json`.

**Takeaway:** each stage is a file in and a file out, and every run says what it cost and
where.

---

## Patterns across the log

- **Invented APIs:** none shipped. Two things were checked before the code relied on
  them:
  - the agent checked the docs and read the installed SDK source to confirm that
    `messages.parse` takes these parameters;
  - it confirmed in the docs that Haiku 5.5 rejects a non-default temperature.

  Search-operator behaviour was never assumed; it was probed live (entry 4).
- **Tests changed to pass:** one, the ordering test in entry 7, and the test's assumption
  was the error. Every other fix added a regression test and left existing tests alone.
- **Over-engineering removed:**
  - the Brave adapter;
  - country and product slices;
  - per-role query variants;
  - Exa people queries for customers;
  - the `boundary_review` status;
  - LLM-written former-employee queries.
- **Where I overruled the agent:** the Brave → Google switch, month-level date
  comparison, page-1-first paging, the no-profile penalty, and a clear yes/no on
  "Tungsten Network, a Kofax company" with nothing hard-coded.
- **Where the agent corrected me:** Haiku reading result pages (entry 5), `deep` for
  people search (entry 6), and where the money goes (entry 9).
- **Code I didn't fully follow at first:** [fill in, and whether you kept it].

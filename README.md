# External Sourcing Agent

Reads a project brief and returns, for each expert group in it, a ranked shortlist of up
to 20 experts. Each expert has a name, the position that makes them relevant (title and
company), a profile link, and a reason backed by a verbatim quote and its source link.
Excluded companies, including former names, are enforced in code.

It runs end to end with one command on both case-study briefs (*Tungsten Automation Due
Diligence* and *Polish Rail Freight*) with no brief-specific code. The results are
committed in [`results/`](results/).

| Document | What it covers |
|---|---|
| **README.md** (this file) | Results, how to run it, a high-level tour, design decisions, how the output is checked |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | How it works in detail: every stage, the code behind it, cost, configuration, code map, evaluation |
| [PLAN.md](docs/PLAN.md) | The initial plan written before any code |
| [AGENT_LOG.md](docs/AGENT_LOG.md) | How the AI coding agent was driven: what I asked, what it got wrong, how it was verified |

## Results (2026-10-11)

| Brief | Group | Search | Candidates read | Passed verification | Met the brief (LLM) | Returned | Borderline (for review) |
|---|---|---|---|---|---|---|---|
| Tungsten | Former employees (left 6 months-3 years ago) | stopped early: 150 of 328 profiles read | 150 | 27 | 26 | **20** | 1 |
| Tungsten | Customers (hands-on users) | stopped early: 50 of 77 profiles read, 30 people searches not run; plus 45 people named on case studies | 94 | 52 | 33 | **20** | 10 |
| Tungsten | Competitor product / GTM / partnerships leaders | stopped early: 50 of 315 profiles read | 50 | 37 | 28 | **20** | 5 |
| Polish | Former network-planning / train-path / access-charge leads | read every profile found | 431 | 20 | 0 | **0** | 8 |
| Polish | Former operations / commercial / fleet leads at private freight operators | read every profile found | 903 | 108 | 20 | **20** | 10 |
| Polish | Terminal and intermodal operators | stopped early: 50 of 77 profiles read, 35 people searches not run; plus 21 people named on web pages | 68 | 37 | 30 | **20** | 7 |

**How to read this table:**
- **The counts are not the limit of what the search can find.** A group stops searching
  once 25 candidates pass verification *and* the LLM judges that they meet the brief:
  the same test the final list uses, with 5 to spare over the 20 returned.
- **Profiles are read best-ranked first,** so the unread ones are the lower-ranked
  results. They weren't rejected; they weren't needed.
- **A group still short of 25 keeps going** (later Google pages, Exa, more
  organisations) and reads everything it can. That happened for Polish freight and
  infrastructure.
- **"Candidates read"** counts one per person after merging duplicates. It includes
  people named on case studies and other web pages, not only the profiles in the
  "Search" column: Tungsten customers are 50 people from profiles plus 45 from case
  studies, with one person found both ways. "Met the brief" is the LLM's `meets` verdict;
  the best 20 of those are returned.

Both are fresh full runs with one command (2026-10-11): Tungsten $0.74 in 5m 30s, Polish
$2.65 in 16m 35s. Every returned expert has a LinkedIn profile. The deliverable
is `results/<brief>/final_results.md`; borderline people are listed there separately and
never counted as experts.

- **Precision (hand-labelled, Tungsten):** 60 of 60 experts on the current list valid
  (95% interval 94-100%), with 0 exclusion violations and 0 duplicates. This is one brief
  and one reviewer. Details: [ARCHITECTURE §4.11](docs/ARCHITECTURE.md#411-evaluation--evaluationpy).
- **Automatic checks on all 100 returned:** 0 current employees of excluded companies, 0
  duplicate people within a group, and every quote is copied from a stored source: the
  person's own evidence or, for 9 Polish experts, the page that shows their organisation
  qualifies.
- **Cost and time per full run:** Tungsten $0.74 in 5m 30s; Polish $2.65 in 16m 35s, because
  a thin group searches everything. Re-ranking from saved candidates
  costs cents. Every run writes its own time and cost breakdown to `run_metadata.json`;
  the per-API and per-stage estimates are in [ARCHITECTURE §7](docs/ARCHITECTURE.md#7-cost-and-performance).
- **Why Polish infrastructure planners return 0:** the brief asks for former *leads* of
  network planning, train paths or access charges. The search found former PKP PLK
  division heads and directors, but their public profiles don't state that remit. The LLM
  marks 8 of them "unclear" for review, and the list is not padded. Details:
  [ARCHITECTURE §11](docs/ARCHITECTURE.md#11-known-gaps-and-next-steps).

## Quick start

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows; .venv/bin/activate on macOS/Linux
pip install -e ".[dev]"
cp .env.example .env            # then fill in ANTHROPIC_API_KEY, SERPER_API_KEY, EXA_API_KEY

python -m sourcing_agent --brief briefs/raw/tungsten-automation.md
python -m sourcing_agent --brief briefs/raw/polish-rail-freight.md
```

Results go to `results/<brief_id>/`. Progress is logged to stderr, one block per stage
with its duration, cost and per-group numbers, and a time and cost summary is printed at
the end.

### Resuming from a saved artifact

Every stage writes a file, and a run can start from any of them. You can review or edit
the spec, the queries or the organisation list without re-running (or re-paying for) the
stages before it. `brief_spec.json` must be in the same folder as the file you start from.

| Start from | Runs | Typical use |
|---|---|---|
| `--brief <brief.md>` | everything | a new brief |
| `--spec brief_spec.json` | from query generation | after reviewing the parsed rules (aliases, windows) |
| `--queries queries.json` | from search | after editing queries |
| `--search-results search_results.json` | from candidate extraction | re-extract without searching again |
| `--organisations organisations.json` | from people search at organisations | after switching off wrong organisations (`include: false`) |
| `--candidates candidates.json` | profile lookup, resolution, verification, ranking | re-rank; about a minute, cents, free when nothing changed |

### Options

| Option | Meaning |
|---|---|
| `--stop-after STAGE` | Last stage to run: `brief_parsing`, `query_generation`, `search`, `candidate_extraction`, `organisation_people`, `profile_lookup`, `entity_resolution`, `verification` or `ranking` (default) |
| `--output-dir DIR` | Where to write (default `results/<brief_id>`, or the input file's folder) |
| `--run-date YYYY-MM-DD` | Reference date for employment windows (default today). Only with `--brief`; a saved spec keeps its own date |
| `-q` / `-v` | Quiet (warnings and the final summary only) / verbose (every LLM call, query and file written) |

### Evaluation, tests, lint

```bash
# precision from a labelled copy of final_results.csv (fill its yes/no/unknown columns)
python -m sourcing_agent.evaluation results/tungsten_automation/eval.csv
pytest                              # 244 offline tests; fake LLM and search, no API calls
python -m ruff check src tests      # undefined names, unused imports, import order
```

### Configuration

All settings are environment variables. [`.env.example`](.env.example) lists every one
with its default and an explanation. Copy it to `.env`; only the three API keys are
required. The ones you are most likely to change:

| Variable | Default | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY`, `SERPER_API_KEY`, `EXA_API_KEY` | none | Required API keys |
| `LLM_PRICE_IN_PER_MTOK`, `LLM_PRICE_OUT_PER_MTOK`, `SERPER_PRICE_PER_1K` | prefilled list prices | Prices behind the cost figures, kept out of the code. Blank means the cost shows as "$?" rather than a guess. Exa reports its own cost |
| `LLM_MODEL` | `claude-haiku-5-5` | The model for every LLM step |
| `TARGET_CANDIDATES_PER_GROUP` | 25 | Qualified candidates a group needs before it stops searching. This is the main cost lever; 0 = search everything |
| `MAX_EXPERTS_PER_GROUP` | 20 | Experts returned per group, fewer if fewer qualify |
| `NO_PROFILE_PENALTY` | 25 | Points (of 100) taken off an expert with no profile, so people with one rank first |
| `RANK_WEIGHTS` | see `.env.example` | Override any ranking weight, e.g. `role_fit=0.3` |

## How it works

Nine stages, each writing a JSON artifact the next one reads. The LLM (Claude Haiku 5.5)
is used where judgement is needed; every rule that can be checked mechanically runs in
code.

| # | Stage | What it does | LLM? |
|---|---|---|---|
| 1 | Brief parsing | Turns the brief into a structured spec: groups, organisations and aliases, exclusions, date windows, hard requirements vs preferences. Every rule must quote the brief | yes, checked by code |
| 2 | Query generation | Per group: LinkedIn X-ray templates for named organisations (former staff, competitors); company-discovery queries where none are named (customers, all Polish groups) | role and topic words |
| 3 | Search | Google (via Serper) and Exa in parallel, with retries; keeps only usable results | no |
| 4 | Candidate extraction | Profiles: dated work history from Exa's structured data, parsed by code. Web pages (case studies, press releases): Haiku lists people and organisations, each with a verified quote | pages only |
| 5 | People at discovered organisations | One X-ray per discovered organisation (e.g. each Tungsten customer), in batches until the group has enough | no |
| 6 | Profile lookup | Finds the LinkedIn profile of people named only on a page; accepted only when the name and the organisation both match | no |
| 7 | Entity resolution | One record per real person across queries, engines, pages and groups; a name alone never merges two people | rare ambiguous pairs |
| 8 | Verification | Code-only eligibility: employment status and window, exclusions and aliases, excluded roles, customer evidence | no |
| 9 | Ranking | Haiku checks each group's hard requirements and scores fit, citing an evidence item. Code adds recency and corroboration, applies the weights and returns the top 20 | yes |

Searching stops for a group once it has 25 candidates that pass verification and the
LLM's check, the same test the final list applies. Ample groups stay cheap and thin groups
keep searching. Stage-by-stage detail, the measurements behind each choice and the cost
breakdown are in [ARCHITECTURE.md](docs/ARCHITECTURE.md) (sections 4 and 7).

## Key design decisions

| Decision | Why |
|---|---|
| Rules in code, judgement in the LLM | Dates, exclusions, organisation matching and merges are testable and never vary between runs. The LLM reads prose, checks requirements code can't ("a product *leader*") and writes reasons |
| Employment from structured data, never from snippets | Exa returns dated work history for LinkedIn profiles; code parses it, so no LLM can invent an employer or a date |
| No LinkedIn scraping | Profiles are found through search engines and read from Exa's index; LinkedIn is never fetched |
| Google via Serper instead of the planned Brave | Brave mangled the X-ray operators (live probes); Google honours them and gave more precise results |
| Many narrow X-ray slices (departure quarters, job functions) | Google returns ≈10 profiles per filtered X-ray; slicing took in-window former employees from 6 to 28 |
| Customers found through their case studies | The brief names no customers, so the agent finds the vendor's case-study section and aggregators, reads them, then searches people at each customer |
| Stop a group early once it has enough *qualified* candidates | The list needs 20, so searching stops at 25 that pass the final list's own test; early stopping cut cost by ≈35-40%. Counting date fit instead stopped "lead" groups too early |
| Hard requirements vs preferences kept apart | "Ideally 8+ years" scores but never rejects. Treating ideal roles as rules wrongly rejected 10 people before the fix |
| Never pad a group to 20 | Only `meets` is returned; `unclear` goes to a separate review table |
| Verdicts cached by a hash of prompt, dossier and model | LLM judgements vary at the margin; re-ranking unchanged candidates gives the same answers at no cost |
| Every stage resumable from its artifact | A human can review the spec, queries or organisations without re-paying for earlier stages |

## Where the pipeline can lie to you, and how it is checked

| Risk | What could happen | Guard | How it was checked |
|---|---|---|---|
| **Made-up people** | An LLM invents a name, employer or quote | Every person comes from a real search hit. Profiles are parsed by code from Exa's structured record (no LLM). A person read from a page needs their name on the page and a verbatim quote that code finds in the text. The quote under each reason is the evidence item the LLM *cited by id*, copied by code. | Tests drop unverifiable quotes and names not on the page; reviewer opened every link in the labelled sample |
| **Stale roles** | A profile still shows someone at a company they left, or hides that they joined an excluded one | Status comes from dated work history; a headline "at <excluded company>" fails the exclusion even if the record looks former; `data_as_of` is kept per candidate; month-only dates are compared in whole months | Tests for stale headlines and window edges |
| **Wrong profile for a page-named person** | Profile lookup attaches someone else's LinkedIn | Same name in the result, plus the organisation in the result or exactly in the profile's work history (not just a name containing it); exactly one profile must pass, so two same-name matches stay unresolved | Every work-history acceptance checked by hand; a looser rule that matched "Rail Polska" to "DB Schenker Rail Polska" was caught and replaced. Found: 15 of 33 (Tungsten), 9 of 30 (Polish); identity correct for all 60 labelled Tungsten experts |
| **Name collisions** | Two different people merged, or one person listed twice | A name never merges alone: same profile URL, or same name **and** a shared employer; two different profiles with similar names need overlapping dates at the same employer **and** an LLM judgement from both records. Nicknames ("Rob" / "Robert") and accents ("Paweł" / "Pawel") are normalised. | Tests: same name at different employers stays apart; 16 (Tungsten) and 18 (Polish) within-group duplicates merged; 0 duplicates in the labelled sample |
| **Renamed / acquired companies** | Former Kofax staff missed, or a Kofax subsidiary's current staff let through | Aliases come only from the brief; an employer containing the excluded name as whole words counts as it ("Kofax India Pvt Ltd", "Tungsten Network, a Kofax company"); substrings never match | Tests for alias entities and look-alike names; 0 exclusion violations in the labelled sample |
| **Customers who never used the product** | Anyone at a customer company is listed | A source must name the target (a profile line or a case-study quote), then the LLM checks direct implementation or use | 27 customer-company staff rejected on the Tungsten run because no source names the product |
| **Ideal roles treated as rules** | Good former employees dropped for not being product managers | Only the brief's hard requirements can reject; "ideal" roles and preferences only change the score | 10 wrongly rejected before the fix, 1 after (an outside adviser) |
| **Thin groups padded** | 20 names returned when few qualify | Only candidates that meet every hard requirement are returned; borderline ones go to a separate review table | Polish infrastructure planners return 0, with 8 for review |

## Outputs

Each brief's folder in `results/` holds:

| File | What it is |
|---|---|
| **`final_results.md`** | **The deliverable.** Per group, up to 20 experts: name linked to the profile, position, status, reason, and a verbatim quote with its source link. Then the borderline people for review |
| `final_results.csv` / `.json` | The same as data, with the score breakdown and sources. The CSV has empty label columns for evaluation; the JSON also keeps the LLM verdict for every ranked candidate |
| `verification.csv` | Every candidate's decision, the rule that decided it, and the ranking outcome: why someone is *not* on the list |
| `run_metadata.json` | The latest run. It opens with a readable summary: total cost and its split by API, time and cost per stage, where each cost figure comes from. Then every LLM call and search behind it. `run_history.jsonl` keeps one line per run |
| `brief_spec.json`, `queries.json` | The parsed brief and the generated queries: reviewable and editable before a resumed run |
| `search_results.json` | Every search hit with its query, rank and provenance |
| `candidates.json` / `.csv` | Everyone found, with dated positions and typed evidence. The CSV is a review sheet, most promising first |
| `organisations.json` / `.csv` | Organisations found by company discovery (e.g. Tungsten's customers), each with a quote and an `include` switch |
| `org_people_*.json`, `profile_lookup.json` | Stage 5 queries and results; each page-named person's profile lookup and how it was decided |
| `resolved_candidates.json`, `verification.json` | One record per real person (with what was merged and why), and every rule's outcome |

## Project structure

```
src/sourcing_agent/
  cli.py, __main__.py   command line (python -m sourcing_agent)
  pipeline/             runs the stages in order; resume points; artifact files
  brief/                stage 1   brief parsing
  search/               stages 2-3 query generation, Serper and Exa
  extraction/           stage 4   candidates from profiles and pages
  people_search/        stages 5-6 people at discovered organisations, profile lookup
  resolution/           stage 7   entity resolution
  verification/         stage 8   deterministic eligibility
  ranking/              stage 9   LLM verdicts, dossiers, scores, the shortlist
  reporting/            progress lines, console and run cost summaries, CSV/Markdown outputs
  models/               every record and artifact schema
  config.py, llm.py, metrics.py, adaptive.py, evaluation.py
```

File-by-file map and layering rules: [ARCHITECTURE §9](docs/ARCHITECTURE.md#9-code-map).

## Known gaps

- Precision is measured on one brief, by one reviewer; the Polish brief is not labelled yet.
- LLM verdicts vary a little at the margin between fresh runs. Cached verdicts keep
  re-runs stable.
- Google results drift between runs, and some profiles aren't in Exa's index, so they
  can't be read.
- The Staff-level extras (refinement loop, streaming, learning from rejections) and the
  design note are not built.

Full list: [ARCHITECTURE §11](docs/ARCHITECTURE.md#11-known-gaps-and-next-steps).

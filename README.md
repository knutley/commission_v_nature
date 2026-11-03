# sn_corpus: Nature Portfolio corpus on EU climate politics

Builds a reproducible corpus from the Springer Nature APIs. It needs Python 3.8+ and nothing else.

| File | What it is |
|---|---|
| `sn_corpus.py` | the collector |
| `journals.txt` | which Nature Portfolio journals to search |
| `queries.txt` | retrieval terms, plus tagging dictionaries for the call's linkage types |

## 1. Get an API key

Register at **dev.springernature.com** and create a key. The free Basic plan is enough to start. Its limits are roughly 25 records per request and a few hundred requests per day, so the script stops at 450 requests per run by default.

```bash
export SPRINGER_API_KEY=your_key
# only if your Open Access key is a different one:
export SPRINGER_OA_KEY=your_oa_key
```

## 2. Check what your plan supports (about 8 requests)

```bash
python3 sn_corpus.py probe
```

This prints the number of hits for a baseline query and for each filter: `issn:`, `openaccess:true`, `year:` and two date syntaxes. **What to look for:**
- **`issn:`:** the total should be much smaller than the baseline. The search depends on this filter. If it has no effect, tell me and I'll switch the script to filtering by journal name.
- **Date filters:** some are Premium-only. If one works, you can push dates to the server with `--extra "datefrom:2015-01-01"`. If not, the script filters dates locally, which works but uses more requests.
- **Abstracts:** should say `yes`.
- **Open Access JATS:** should say `ok` with a word count.

## 3. Resolve journal ISSNs

```bash
python3 sn_corpus.py journals
```

This looks up each name in Springer Nature's public nature.com title list and writes `journals_resolved.txt`. Keep that file with your replication materials. If a name isn't found, fix the spelling or add the ISSN by hand (`Nature Energy | 2058-7546`). If the title-list URL has expired, download the latest "NatureComAllTitles" KBART file and pass `--kbart path/to/file.txt`.

## 4. Search

```bash
python3 sn_corpus.py search --journals journals_resolved.txt --dry-run   # look at the queries first
python3 sn_corpus.py search --journals journals_resolved.txt --from 2015-01-01
```

The query for each journal is `([eu] AND [climate]) OR [standalone]`, restricted to that journal's ISSNs. Results are merged into **`corpus.csv`**, one row per DOI:

- metadata: title, journal, dates, article type (`genre`, `content_type`), open-access flag, authors, **abstract**, subjects, keywords, URL
- `matched_terms`: which retrieval terms actually appear in the title or abstract. Rows with `0` matched terms deserve a look, because they may be false positives.
- `tag_security`, `tag_trade`, `tag_multilevel`, …: counts of each linkage dictionary in title plus abstract. These are rough indicators for checking against your hand-coding, not a typology on their own.
- `retrieved_via`, `first_seen`, `last_seen`

Every run appends to **`runs.jsonl`**: the exact query, the total the API reported, records retrieved, and records kept or dropped, with the reason (non-Nature DOI or outside the date range). That gives you the numbers for a corpus-construction flow chart in the methods section.

If a run hits the request budget it saves its progress and stops. Re-run it the next day with `--only "Journal Name"` for the remaining journals. Re-running is safe, because rows are de-duplicated by DOI.

Two safety nets apply. Any record whose DOI isn't `10.1038/…` (that is, not Nature Portfolio) is dropped. And if one journal reports more than 3,000 hits, you get a warning, since the ISSN filter may have been ignored.

## 5. Full text (open-access articles)

```bash
python3 sn_corpus.py fulltext
```

For each article flagged open access, this downloads the JATS XML and a clean `.txt` of the body into `fulltext/`. Figure captions, tables and citation markers are removed. The script records `fulltext_status` (`ok`, `no_body` or `not_in_oa`) and a word count in `corpus.csv`. It's safe to re-run: finished articles are skipped. Paywalled articles will mostly come back `not_in_oa`. For those, rely on abstracts or your library's text-and-data-mining access.

## Things to check on the first real run

The script was tested against mocked API responses, not the live API, because the API can't be reached from where this was built. On your first run, confirm:
1. `probe` shows the `issn:` filter working.
2. A small `search --only "Nature Climate Change"` gives sensible totals. If totals look huge, the API may be matching terms in full text rather than metadata. In that case, tighten `[eu]`, for example by dropping the bare `EU`.
3. A few rows in `corpus.csv` have abstracts and the right journal.

## Tuning `queries.txt`

- Retrieval is deliberately neutral about linkages, so the corpus isn't biased toward what you're measuring. Keep linkage terms in the `[tag:…]` sections.
- Bare `EU` is case-sensitive locally. On the API side it is broad, so watch for noise.
- Record every change to `queries.txt` along with the date. `runs.jsonl` stores a hash of the query, so you can match each change to the runs it affected.

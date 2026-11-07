"""
Nature Portfolio abstracts: stream, score, keep only what passes
================================================================

No abstracts are stored during scoring. Each page of articles is fetched,
scored with the unchanged AdJUST keyword-density code, and the text discarded;
only metadata + hits + density are written. Peak memory is one page of
records, and the scores file is a few MB.

Three passes:
  score      Stream OpenAlex (by journal x year), fill missing abstracts from the
             Springer Nature Meta API by DOI, score, write scores.csv (no text).
  threshold  Read scores.csv, print percentile cut-offs (both bases), save the
             density plot, write kept.csv for the chosen cut-off.
  text       Re-fetch abstracts only for the kept articles -> kept_texts.csv
             (the input for the classifier).

Usage
-----
    pip install requests pandas nltk matplotlib
    python fetch_and_filter.py score     --adjust-repo PATH --springer-key KEY --email EMAIL
    python fetch_and_filter.py threshold --percentile 85 --base nonzero
    python fetch_and_filter.py text      --springer-key KEY --email EMAIL

Every pass is resumable: re-run the same command after an interruption.
"""

import argparse
import html
import os
import re
import sys
import time

import pandas as pd
import requests

# -----------------------------------------------------------------------------
# Journals (OpenAlex source IDs) and primary EUR-Lex directory
# (Table tab:directory_scope; 2026 launches excluded.)
# -----------------------------------------------------------------------------
JOURNALS = {
    "S183584863": ("Nature Climate Change", "Environment"),
    "S4210188283": ("Nature Sustainability", "Environment"),
    "S4210221880": ("Communications Earth & Environment", "Environment"),
    "S4210178125": ("Nature Reviews Earth & Environment", "Environment"),
    "S4387286383": ("Nature Water", "Environment"),
    "S5407042881": ("Nature Reviews Biodiversity", "Environment"),
    "S4387284748": ("npj Biodiversity", "Environment"),
    "S5407047664": ("npj Clean Air", "Environment"),
    "S4210171286": ("npj Clean Water", "Environment"),
    "S4210224503": ("npj Climate and Atmospheric Science", "Environment"),
    "S4387282441": ("npj Ocean Sustainability", "Environment"),
    "S2764528046": ("Nature Energy", "Energy"),
    "S5407047663": ("npj Clean Energy", "Energy"),
    "S4210207988": ("Nature Food", "Agriculture"),
    "S4387997482": ("npj Sustainable Agriculture", "Agriculture"),
    "S5407042854": ("npj Sustainable Mobility and Transport", "Transport"),
    "S5407042882": ("Nature Reviews Clean Technology", "Industrial policy"),
    "S4387287600": ("npj Climate Action", "External relations"),
    "S4210206302": ("Humanities and Social Sciences Communications", "External relations"),
    "S137773608": ("Nature", "No counterpart"),
    "S64187185": ("Nature Communications", "No counterpart"),
    "S4390297069": ("Nature Cities", "No counterpart"),
    "S2764884935": ("Nature Ecology & Evolution", "No counterpart"),
    "S2764866340": ("Nature Human Behaviour", "No counterpart"),
    "S4210223291": ("npj Urban Sustainability", "No counterpart"),
}

# Structural filter for the multidisciplinary titles: OpenAlex primary field
# (Env Sci, Economics, Social Sci, Energy, Earth & Planetary, Agri & Bio, Business)
MULTIDISCIPLINARY = {"S137773608", "S64187185"}
FIELD_FILTER = "primary_topic.field.id:23|20|33|21|19|11|14"

YEAR_FROM, YEAR_TO = 1990, 2025
OPENALEX = "https://api.openalex.org/works"
SPRINGER = "https://api.springernature.com/meta/v2/json"

SCORES = "scores.csv"
KEPT = "kept.csv"
KEPT_TEXT = "kept_texts.csv"


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def get(session, url, params, label):
    """GET with backoff; long waits on 429 so daily quotas can reset."""
    for attempt in range(8):
        try:
            r = session.get(url, params=params, timeout=60)
        except requests.RequestException:
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code in (401, 403):
            sys.exit(f"{label} refused the request ({r.status_code}): {r.text[:200]}")
        wait = 60 * (attempt + 1) if r.status_code == 429 else 2 ** attempt
        print(f"    {label} {r.status_code}; waiting {wait}s")
        time.sleep(wait)
    sys.exit(f"{label} kept failing; re-run later to resume.")


def rebuild_abstract(inv):
    if not inv:
        return None
    pos = {i: w for w, idxs in inv.items() for i in idxs}
    return " ".join(pos[i] for i in sorted(pos)) or None


def clean_html(text):
    if not isinstance(text, str):
        return None
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"^\s*Abstract\s*", "", text)
    return re.sub(r"\s+", " ", text).strip() or None


def springer_lookup(session, dois, key):
    """{doi: (abstract, content_type)} for a batch of DOIs."""
    if not dois:
        return {}
    q = " OR ".join(f'doi:"{d}"' for d in dois)
    data = get(session, SPRINGER, {"q": q, "p": len(dois), "api_key": key}, "Springer")
    out = {}
    for rec in data.get("records", []):
        out[(rec.get("doi") or "").lower()] = (clean_html(rec.get("abstract")),
                                                rec.get("contentType"))
    return out


def openalex_pages(session, filt, email, select):
    params = {"filter": filt, "select": select, "per-page": 200,
              "mailto": email, "cursor": "*"}
    while params["cursor"]:
        data = get(session, OPENALEX, params, "OpenAlex")
        yield data["results"]
        params["cursor"] = data["meta"].get("next_cursor")


def load_scorer(repo):
    kw = os.path.join(repo, "data_screening", "substantive_relevance")
    sys.path.insert(0, os.path.join(kw, "keyword_integration"))
    from keyword_density import load_keywords, score_document
    keywords = set(load_keywords(
        os.path.join(kw, "computational_approach", "filtered_seeds.csv"),
        os.path.join(kw, "computational_approach", "double_filtered_discovered_keywords.csv"),
        os.path.join(kw, "qualitative_approach", "Expert-Curated keywords.txt"),
    ))
    return lambda text: score_document(text, keywords)


# -----------------------------------------------------------------------------
# Pass 1: stream + score (no text written)
# -----------------------------------------------------------------------------
def pass_score(a):
    score = load_scorer(a.adjust_repo)
    path = os.path.join(a.out, SCORES)
    done = set()
    if os.path.exists(path):
        prev = pd.read_csv(path, usecols=["source_id", "year"])
        done = set(zip(prev["source_id"], prev["year"]))

    session = requests.Session()
    select = "id,doi,title,publication_year,abstract_inverted_index"
    for sid, (name, directory) in JOURNALS.items():
        for year in range(YEAR_FROM, YEAR_TO + 1):
            if (sid, year) in done:
                continue
            filt = f"primary_location.source.id:{sid},type:article,publication_year:{year}"
            if sid in MULTIDISCIPLINARY:
                filt += "," + FIELD_FILTER
            rows = []
            for page in openalex_pages(session, filt, a.email, select):
                missing = []
                for w in page:
                    doi = (w.get("doi") or "").replace("https://doi.org/", "").lower() or None
                    text = rebuild_abstract(w.get("abstract_inverted_index"))
                    rows.append({"openalex_id": w["id"].rsplit("/", 1)[-1], "doi": doi,
                                 "title": w.get("title"), "year": year, "source_id": sid,
                                 "journal": name, "directory": directory,
                                 "abstract_source": "openalex" if text else None,
                                 "content_type": None, "_text": text})
                    if not text and doi:
                        missing.append(len(rows) - 1)
                if a.springer_key:
                    for i in range(0, len(missing), a.batch):
                        idx = missing[i:i + a.batch]
                        found = springer_lookup(session, [rows[j]["doi"] for j in idx],
                                                a.springer_key)
                        for j in idx:
                            text, ctype = found.get(rows[j]["doi"], (None, None))
                            rows[j]["content_type"] = ctype
                            if text:
                                rows[j]["_text"], rows[j]["abstract_source"] = text, "springer"
                        time.sleep(a.pause)
                for r in rows:  # score, then drop the text
                    if r.get("_text") is not None:
                        r["hits"], r["density"] = score(r.pop("_text"))
                        r["n_tokens"] = r["hits"] / r["density"] if r["density"] else None
                    else:
                        r.pop("_text", None)
                        r["hits"] = r["density"] = r["n_tokens"] = None
            if rows:
                pd.DataFrame(rows).to_csv(path, mode="a", index=False,
                                          header=not os.path.exists(path))
            else:  # record empty journal-years so they're skipped on resume
                pd.DataFrame([{"openalex_id": None, "year": year, "source_id": sid,
                               "journal": name, "directory": directory}]).to_csv(
                    path, mode="a", index=False, header=not os.path.exists(path))
            n_abs = sum(r["density"] is not None for r in rows)
            if rows:
                print(f"  {name} {year}: {len(rows):,} articles, {n_abs:,} scored")
    print(f"Scores written to {path}")


# -----------------------------------------------------------------------------
# Pass 2: threshold
# -----------------------------------------------------------------------------
def pass_threshold(a):
    df = pd.read_csv(os.path.join(a.out, SCORES)).dropna(subset=["openalex_id"])
    scored = df.dropna(subset=["density"])
    nonzero = scored[scored["density"] > 0]

    print(f"Articles: {len(df):,} | scored (had abstract): {len(scored):,} "
          f"({len(scored) / len(df):.0%}) | non-zero density: {len(nonzero):,}")
    print("\n pctl | base=all (85per_corpus.py)  | base=nonzero (paper Methods)")
    for p in (75, 80, 85, 90):
        x, y = scored["density"].quantile(p / 100), nonzero["density"].quantile(p / 100)
        print(f" {p:>4} | >= {x:.4f} -> {(scored['density'] >= x).sum():>7,} | "
              f">= {y:.4f} -> {(scored['density'] >= y).sum():>7,}")
    print(f" AdJUST absolute cut-off >= 0.032 -> {(scored['density'] >= 0.032).sum():,}")

    sys.path.insert(0, os.path.join(a.adjust_repo, "data_screening",
                                    "substantive_relevance", "keyword_integration"))
    from keyword_density import plot_distribution
    plot_distribution(scored, a.out)

    base = nonzero if a.base == "nonzero" else scored
    cut = a.cutoff if a.cutoff is not None else base["density"].quantile(a.percentile / 100)
    kept = scored[scored["density"] >= cut]
    kept.to_csv(os.path.join(a.out, KEPT), index=False)

    cov = df.assign(scored=df["density"].notna()).groupby(["journal", "year"]).agg(
        articles=("openalex_id", "size"), with_abstract=("scored", "sum")).reset_index()
    cov.to_csv(os.path.join(a.out, "abstract_coverage_by_journal_year.csv"), index=False)
    print(f"\nCut-off density >= {cut:.4f}: kept {len(kept):,} -> {KEPT}")
    print(kept.groupby("journal").size().sort_values(ascending=False).to_string())


# -----------------------------------------------------------------------------
# Pass 3: text for kept articles only
# -----------------------------------------------------------------------------
def pass_text(a):
    kept = pd.read_csv(os.path.join(a.out, KEPT))
    path = os.path.join(a.out, KEPT_TEXT)
    have = set(pd.read_csv(path, usecols=["openalex_id"])["openalex_id"]) \
        if os.path.exists(path) else set()
    todo = kept[~kept["openalex_id"].isin(have)]
    session = requests.Session()
    print(f"{len(todo):,} kept articles to fetch text for")

    oa = todo[todo["abstract_source"] == "openalex"]
    for i in range(0, len(oa), 50):
        chunk = oa.iloc[i:i + 50]
        data = get(session, OPENALEX, {"filter": "openalex:" + "|".join(chunk["openalex_id"]),
                                       "select": "id,abstract_inverted_index",
                                       "per-page": 50, "mailto": a.email}, "OpenAlex")
        texts = {w["id"].rsplit("/", 1)[-1]: rebuild_abstract(w.get("abstract_inverted_index"))
                 for w in data["results"]}
        out = chunk.assign(text=chunk["openalex_id"].map(texts))
        out.to_csv(path, mode="a", index=False, header=not os.path.exists(path))

    sp = todo[todo["abstract_source"] == "springer"]
    if len(sp) and not a.springer_key:
        sys.exit("--springer-key needed for Springer-sourced abstracts")
    for i in range(0, len(sp), a.batch):
        chunk = sp.iloc[i:i + a.batch]
        found = springer_lookup(session, list(chunk["doi"]), a.springer_key)
        out = chunk.assign(text=chunk["doi"].map(lambda d: found.get(d, (None, None))[0]))
        out.to_csv(path, mode="a", index=False, header=not os.path.exists(path))
        time.sleep(a.pause)
    print(f"Texts written to {path}")


# -----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["score", "threshold", "text"])
    ap.add_argument("--adjust-repo", default="personal_AdJUST")
    ap.add_argument("--springer-key")
    ap.add_argument("--email", help="contact email for the OpenAlex polite pool")
    ap.add_argument("--out", default="nature_out")
    ap.add_argument("--batch", type=int, default=20, help="DOIs per Springer request")
    ap.add_argument("--pause", type=float, default=1.0, help="seconds between Springer calls")
    ap.add_argument("--percentile", type=float, default=85)
    ap.add_argument("--base", choices=["nonzero", "all"], default="nonzero")
    ap.add_argument("--cutoff", type=float, help="absolute density cut-off (overrides percentile)")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    if a.step in ("score", "text") and not a.email:
        sys.exit("--email is required for OpenAlex")
    {"score": pass_score, "threshold": pass_threshold, "text": pass_text}[a.step](a)


if __name__ == "__main__":
    main()

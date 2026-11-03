#!/usr/bin/env python3
"""
Build the science-side corpus for "Does science lead the Commission?"

Pulls every OpenAlex article/review (1990-2025, with an abstract) whose title,
abstract or keywords mention an EU term AND a climate-policy term, rebuilds the
abstracts, and writes one row per paper to science_corpus.csv.

Usage:
    pip install requests pandas
    python build_science_corpus.py --mailto you@uni.ac.uk [--api-key KEY]

The query mirrors the one used to find the Nature Portfolio items. Nothing is
dropped except duplicates; screening flags are added as columns so you decide.
"""
import argparse
import time

import pandas as pd
import requests

BASE = "https://api.openalex.org/works"
NATURE_PORTFOLIO = "P4310319908"

EU_TERMS = ['"European Union"', '"EU"', '"European Commission"', '"EU ETS"',
            '"European Green Deal"', '"Fit for 55"', '"carbon border adjustment"', '"CBAM"']
POLICY_TERMS = ['"climate policy"', '"climate policies"', '"climate target"', '"climate targets"',
                '"climate neutrality"', '"emissions trading"', '"carbon price"', '"carbon pricing"',
                '"carbon tax"', '"decarbonisation"', '"decarbonization"', '"net zero"',
                '"emission reduction"', '"emissions reduction"', '"mitigation policy"',
                '"climate law"', '"climate mitigation"', '"greenhouse gas emissions"',
                '"carbon leakage"', '"renewable energy target"']

SEARCH = f"({' OR '.join(EU_TERMS)}) AND ({' OR '.join(POLICY_TERMS)})"
FILTER = ",".join([
    f"title_abstract_keywords.search:{SEARCH}",
    "publication_year:1990-2025",
    "type:article|review",
    "is_retracted:false",
    "has_abstract:true",
])
SELECT = ",".join([
    "id", "doi", "title", "publication_year", "publication_date", "type", "language",
    "primary_location", "abstract_inverted_index", "cited_by_count", "open_access",
    "primary_topic", "authorships",
])


def rebuild_abstract(inv):
    """OpenAlex stores abstracts as {word: [positions]}; turn that back into text."""
    if not inv:
        return ""
    slots = {}
    for word, positions in inv.items():
        for p in positions:
            slots[p] = word
    return " ".join(slots[i] for i in sorted(slots))


def flatten(w):
    src = (w.get("primary_location") or {}).get("source") or {}
    lineage = src.get("host_organization_lineage") or []
    topic = w.get("primary_topic") or {}
    countries = sorted({c for a in (w.get("authorships") or []) for c in (a.get("countries") or [])})
    institutions = sorted({
        (i.get("display_name") or "")
        for a in (w.get("authorships") or []) for i in (a.get("institutions") or [])
    } - {""})
    return {
        "openalex_id": w["id"].rsplit("/", 1)[-1],
        "doi": w.get("doi"),
        "title": w.get("title"),
        "abstract": rebuild_abstract(w.get("abstract_inverted_index")),
        "year": w.get("publication_year"),
        "date": w.get("publication_date"),
        "type": w.get("type"),
        "language": w.get("language"),
        "journal": src.get("display_name"),
        "source_id": (src.get("id") or "").rsplit("/", 1)[-1],
        "publisher": src.get("host_organization_name"),
        "nature_portfolio": any(NATURE_PORTFOLIO in (x or "") for x in lineage),
        "cited_by": w.get("cited_by_count"),
        "is_oa": (w.get("open_access") or {}).get("is_oa"),
        "primary_topic": topic.get("display_name"),
        "primary_field": (topic.get("field") or {}).get("display_name"),
        "author_countries": ";".join(countries),
        # Joint Research Centre / Commission co-authorship: the science-policy crossover cases
        "commission_coauthor": any(
            k in inst for inst in institutions
            for k in ("European Commission", "Joint Research Centre", "Joint Research Center")
        ),
    }


def add_screening_flags(df):
    text = (df["title"].fillna("") + " " + df["abstract"].fillna("")).str.lower()
    # "EU" also matches Eu (europium) in chemistry/materials papers
    df["flag_europium"] = text.str.contains(r"\beu\d|\beu-doped|\beu3\+|\beu2\+|europium", regex=True)
    df["flag_non_english"] = df["language"].fillna("en") != "en"
    df["flag_short_abstract"] = df["abstract"].str.split().str.len() < 50
    df["flag_off_field"] = df["primary_field"].isin(
        ["Materials Science", "Chemistry", "Physics and Astronomy", "Medicine",
         "Biochemistry, Genetics and Molecular Biology"])
    df["keep_default"] = ~(df["flag_europium"] | df["flag_non_english"] | df["flag_off_field"])
    return df


def fetch(mailto, api_key=None, per_page=200, pause=0.15):
    params = {"filter": FILTER, "select": SELECT, "per-page": per_page,
              "cursor": "*", "mailto": mailto}
    if api_key:
        params["api_key"] = api_key
    rows, total = [], None
    with requests.Session() as s:
        while True:
            for attempt in range(5):
                r = s.get(BASE, params=params, timeout=60)
                if r.status_code == 200:
                    break
                time.sleep(2 ** attempt)
            r.raise_for_status()
            data = r.json()
            total = total or data["meta"]["count"]
            rows.extend(flatten(w) for w in data["results"])
            print(f"\r{len(rows):,} / {total:,}", end="", flush=True)
            cursor = data["meta"].get("next_cursor")
            if not cursor or not data["results"]:
                break
            params["cursor"] = cursor
            time.sleep(pause)
    print()
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mailto", required=True, help="your email, for OpenAlex's polite pool")
    ap.add_argument("--api-key", help="OpenAlex API key, if you have one")
    ap.add_argument("--out", default="science_corpus.csv")
    a = ap.parse_args()

    df = pd.DataFrame(fetch(a.mailto, a.api_key))
    df = df.drop_duplicates("openalex_id")
    # same paper indexed twice (preprint + journal version, etc.)
    df["_t"] = df["title"].str.lower().str.replace(r"\W+", " ", regex=True).str.strip()
    df = df.sort_values("cited_by", ascending=False).drop_duplicates(["_t", "year"]).drop(columns="_t")
    df = add_screening_flags(df).sort_values(["year", "openalex_id"])
    df.to_csv(a.out, index=False)

    k = df[df.keep_default]
    print(f"Wrote {len(df):,} rows to {a.out}; {len(k):,} pass default screening")
    print(f"Nature Portfolio: {k.nature_portfolio.sum():,} | Commission/JRC co-authored: {k.commission_coauthor.sum():,}")
    print(k.groupby("year").size().to_string())


if __name__ == "__main__":
    main()

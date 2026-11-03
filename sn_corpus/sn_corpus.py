#!/usr/bin/env python3
"""
sn_corpus.py -- build a corpus of Nature Portfolio articles on EU climate
politics from the Springer Nature APIs.

  Meta API v2        metadata + abstracts for every matching article
  Open Access API    full text (JATS XML) for the open-access subset

Subcommands (run them in this order the first time):

  probe      Check which query features your API plan supports.
  journals   Resolve the journal names in journals.txt to ISSNs.
  search     Run the queries and merge results into corpus.csv.
  fulltext   Download open-access full text for articles in corpus.csv.

Your API key is read from the SPRINGER_API_KEY environment variable (or
--api-key). If your Open Access key is different, set SPRINGER_OA_KEY.

Every search and fulltext run is logged to runs.jsonl (query, totals,
records kept and dropped), so you can report exactly how the corpus was
built in the paper's methods section.

Only the Python standard library is used.
"""

import argparse
import csv
import datetime as dt
import hashlib
import html
import json
import os
import random
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

META_URL = 'https://api.springernature.com/meta/v2/json'
OA_JATS_URL = 'https://api.springernature.com/openaccess/jats'

# Springer Nature's public KBART list of all nature.com titles (with ISSNs).
# These files are re-issued with a new date in the name; if this one has
# gone, download the latest "NatureComAllTitles" file and pass --kbart PATH.
DEFAULT_KBART = ('https://metadata.springernature.com/metadata/kbart/'
                 'SpringerNature_Global_NatureComAllTitles_2026-07-06.txt')

# Every Nature Portfolio article has a DOI under this prefix. Records with any
# other prefix are dropped, as a safety net in case an ISSN filter is ignored.
NATURE_DOI_PREFIX = '10.1038/'

USER_AGENT = 'sn-corpus/1.0 (academic text-as-data research)'
csv.field_size_limit(sys.maxsize)


class ApiError(Exception):
    pass


class BudgetExhausted(Exception):
    pass


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def now():
    return dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Client(object):
    """Rate-limited GET with retries and a per-run request budget."""

    def __init__(self, api_key, max_requests=None, delay=1.0):
        self.api_key = api_key
        self.max_requests = max_requests
        self.delay = delay
        self.count = 0
        self._last = 0.0

    def get(self, base, params, accept='application/json'):
        if self.max_requests is not None and self.count >= self.max_requests:
            raise BudgetExhausted('request budget of %d used up' % self.max_requests)
        query = dict(params)
        query['api_key'] = self.api_key
        url = base + '?' + urllib.parse.urlencode(query)
        shown = url.replace(urllib.parse.quote_plus(self.api_key), '***') \
            if self.api_key else url

        for attempt in range(5):
            wait = self.delay - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.count += 1
            try:
                req = urllib.request.Request(
                    url, headers={'User-Agent': USER_AGENT, 'Accept': accept})
                with urllib.request.urlopen(req, timeout=90) as resp:
                    return resp.read()
            except urllib.error.HTTPError as err:
                body = err.read()[:600].decode('utf-8', 'replace')
                if err.code in (429, 500, 502, 503, 504) and attempt < 4:
                    pause = 5 * (2 ** attempt) + random.random()
                    log('  HTTP %d, retrying in %.0fs' % (err.code, pause))
                    time.sleep(pause)
                    continue
                raise ApiError('HTTP %d for %s\n  %s' % (err.code, shown, body.strip()))
            except urllib.error.URLError as err:
                if attempt < 4:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise ApiError('network error for %s: %s' % (shown, err.reason))
        raise ApiError('giving up on %s' % shown)


def make_client(args, key_env='SPRINGER_API_KEY'):
    key = args.api_key or os.environ.get(key_env) or os.environ.get('SPRINGER_API_KEY')
    if not key:
        sys.exit('No API key. Set SPRINGER_API_KEY (get one at dev.springernature.com) '
                 'or pass --api-key.')
    return Client(key, max_requests=args.max_requests, delay=args.delay)


# --------------------------------------------------------------------------
# Config files
# --------------------------------------------------------------------------

def read_sections(path):
    """Reads an INI-like file of [section] headers followed by one term per line."""
    sections, current = {}, None
    with open(path, encoding='utf-8') as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.split('#', 1)[0].strip()
            if not line:
                continue
            m = re.fullmatch(r'\[(.+)\]', line)
            if m:
                current = m.group(1).strip().lower()
                sections.setdefault(current, [])
                continue
            if current is None:
                sys.exit('%s:%d: term %r appears before any [section]' % (path, lineno, line))
            sections[current].append(line)
    return sections


def api_term(term):
    """Formats a term for the API query: multi-word terms become phrases."""
    term = term.strip()
    if term.startswith('"') and term.endswith('"'):
        return term
    if re.fullmatch(r'[\w*]+', term):
        return term
    return '"%s"' % term.replace('"', '')


def or_block(terms):
    return '(' + ' OR '.join(api_term(t) for t in terms) + ')'


def build_topic_query(cfg):
    for need in ('eu', 'climate', 'standalone'):
        if not cfg.get(need):
            sys.exit('queries file needs a non-empty [%s] section' % need)
    return '((%s AND %s) OR %s)' % (or_block(cfg['eu']), or_block(cfg['climate']),
                                    or_block(cfg['standalone']))


def term_regex(term):
    """Local matcher: whole words, flexible space/hyphen, trailing * = prefix.
    Short all-caps acronyms (EU, ETS, CBAM) match case-sensitively, so that
    e.g. the chemical symbol 'Eu' does not count as 'EU'."""
    term = term.strip().strip('"')
    parts = [re.escape(p).replace(r'\*', r'\w*')
             for p in re.split(r'[\s\-]+', term) if p]
    pattern = r'(?<!\w)' + r'[\s\-]+'.join(parts) + r'(?!\w)'
    acronym = term.replace(' ', '').isupper() and len(term) <= 6
    return re.compile(pattern, 0 if acronym else re.IGNORECASE)


class Matcher(object):
    def __init__(self, cfg):
        self.retrieval = [(t, term_regex(t))
                          for sec in ('eu', 'climate', 'standalone')
                          for t in cfg.get(sec, [])]
        self.groups = {sec: [term_regex(t) for t in cfg.get(sec, [])]
                       for sec in ('eu', 'climate', 'standalone')}
        self.tags = {}
        for sec, terms in cfg.items():
            if sec.startswith('tag:'):
                self.tags[sec[4:].strip()] = [(t, term_regex(t)) for t in terms]

    def relevant(self, text):
        """Does the title/abstract itself satisfy ([eu] AND [climate]) OR [standalone]?"""
        hit = {sec: any(rx.search(text) for rx in rxs) for sec, rxs in self.groups.items()}
        return (hit['eu'] and hit['climate']) or hit['standalone']

    def match(self, text):
        found = sorted({t for t, rx in self.retrieval if rx.search(text)}, key=str.lower)
        tag_counts = {}
        for name, terms in self.tags.items():
            # Count distinct stretches of text, so "energy security" counts
            # once even though both "energy security" and "security" match.
            spans = sorted(m.span() for _, rx in terms for m in rx.finditer(text))
            count, end = 0, -1
            for start, stop in spans:
                if start >= end:
                    count += 1
                    end = stop
                else:
                    end = max(end, stop)
            tag_counts[name] = count
        return found, tag_counts


# --------------------------------------------------------------------------
# Journals
# --------------------------------------------------------------------------

ISSN_RE = re.compile(r'^\d{4}-\d{3}[\dXx]$')


def norm_name(name):
    name = name.lower().replace('&', ' and ')
    return ' '.join(re.sub(r'[^a-z0-9 ]', ' ', name).split())


def load_kbart(src, cache_dir):
    if re.match(r'https?://', src):
        cache = os.path.join(cache_dir, 'kbart_' + os.path.basename(src))
        if not os.path.exists(cache):
            log('downloading journal list %s' % src)
            req = urllib.request.Request(src, headers={'User-Agent': USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = resp.read()
            except urllib.error.URLError as err:
                sys.exit('Could not download the KBART journal list (%s).\n'
                         'Either add ISSNs to journals.txt by hand ("Name | eISSN, pISSN"), '
                         'or download the latest NatureComAllTitles KBART file from '
                         'Springer Nature and pass --kbart PATH.' % err)
            with open(cache, 'wb') as fh:
                fh.write(data)
        src = cache
    table = {}
    with open(src, encoding='utf-8-sig', errors='replace', newline='') as fh:
        for row in csv.DictReader(fh, delimiter='\t'):
            title = (row.get('publication_title') or '').strip()
            if not title:
                continue
            issns = [(row.get(k) or '').strip() for k in ('online_identifier', 'print_identifier')]
            entry = table.setdefault(norm_name(title), {'title': title, 'issns': []})
            for issn in issns:
                if ISSN_RE.match(issn) and issn not in entry['issns']:
                    entry['issns'].append(issn)
    return table


def load_journals(path, kbart_src, cache_dir):
    entries = []
    with open(path, encoding='utf-8') as fh:
        for raw in fh:
            line = raw.split('#', 1)[0].strip()
            if not line:
                continue
            name, _, issn_part = line.partition('|')
            issns = [i for i in re.split(r'[,\s]+', issn_part.strip()) if i]
            bad = [i for i in issns if not ISSN_RE.match(i)]
            if bad:
                sys.exit('%s: bad ISSN(s) %s for %r' % (path, bad, name.strip()))
            entries.append([name.strip(), issns])

    if any(not issns for _, issns in entries):
        table = load_kbart(kbart_src, cache_dir)
        missing = []
        for entry in entries:
            if entry[1]:
                continue
            hit = table.get(norm_name(entry[0]))
            if hit and hit['issns']:
                entry[1] = hit['issns']
            else:
                missing.append(entry[0])
        if missing:
            sys.exit('Not found in the KBART list (check spelling, or add ISSNs by hand '
                     'as "Name | eISSN"):\n  ' + '\n  '.join(missing))
    return entries


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------

def flatten(x):
    if x is None:
        return ''
    if isinstance(x, str):
        return x
    if isinstance(x, (list, tuple)):
        return ' '.join(flatten(i) for i in x)
    if isinstance(x, dict):
        return ' '.join(flatten(v) for k, v in x.items() if k not in ('h1', 'h2', 'h3', 'title'))
    return str(x)


def clean_text(s):
    s = html.unescape(re.sub(r'<[^>]+>', ' ', s or ''))
    return ' '.join(s.split())


def as_list(x):
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def parse_record(rec):
    doi = rec.get('doi') or ''
    if not doi:
        ident = rec.get('identifier') or ''
        doi = ident[4:] if ident.startswith('doi:') else ident
    authors = []
    for c in as_list(rec.get('creators')):
        name = c.get('creator') if isinstance(c, dict) else c
        if name:
            authors.append(str(name))
    url = ''
    for u in as_list(rec.get('url')):
        if isinstance(u, dict):
            if not url or u.get('format') == 'html':
                url = u.get('value') or url
        elif isinstance(u, str) and not url:
            url = u
    subjects = [flatten(s) for s in as_list(rec.get('subjects'))]
    for d in as_list(rec.get('disciplines')):
        term = d.get('term') if isinstance(d, dict) else d
        if term and term not in subjects:
            subjects.append(str(term))
    pub_date = rec.get('publicationDate') or ''
    online_date = rec.get('onlineDate') or ''
    date = pub_date or online_date
    return {
        'doi': doi.strip().lower(),
        'title': clean_text(flatten(rec.get('title'))),
        'journal': clean_text(flatten(rec.get('publicationName'))),
        'issn': rec.get('issn') or '',
        'eissn': rec.get('eIssn') or rec.get('eissn') or '',
        'publication_date': pub_date,
        'online_date': online_date,
        'year': date[:4],
        'content_type': flatten(rec.get('contentType')),
        'genre': '; '.join(flatten(g) for g in as_list(rec.get('genre'))),
        'open_access': str(rec.get('openaccess', '')).lower(),
        'authors': '; '.join(authors),
        'abstract': clean_text(flatten(rec.get('abstract'))),
        'subjects': '; '.join(s for s in subjects if s),
        'article_keywords': '; '.join(flatten(k) for k in as_list(rec.get('keyword'))),
        'url': url,
    }


# --------------------------------------------------------------------------
# Dataset
# --------------------------------------------------------------------------

BASE_FIELDS = ['doi', 'title', 'journal', 'issn', 'eissn', 'publication_date',
               'online_date', 'year', 'content_type', 'genre', 'open_access',
               'authors', 'abstract', 'subjects', 'article_keywords', 'url',
               'ta_relevant', 'matched_terms', 'n_matched_terms']
TAIL_FIELDS = ['retrieved_via', 'first_seen', 'last_seen',
               'fulltext_status', 'fulltext_words', 'fulltext_file']
REFRESH = BASE_FIELDS


class Dataset(object):
    """corpus.csv: one row per DOI, merged across journals and runs."""

    def __init__(self, path, tag_names=()):
        self.path = path
        self.rows = {}
        self.fields = list(BASE_FIELDS)
        existing = []
        if os.path.exists(path):
            with open(path, newline='', encoding='utf-8') as fh:
                reader = csv.DictReader(fh)
                existing = reader.fieldnames or []
                for row in reader:
                    self.rows[row['doi']] = row
        tags = [f for f in existing if f.startswith('tag_')]
        tags += ['tag_' + t for t in tag_names if 'tag_' + t not in tags]
        self.fields += tags + TAIL_FIELDS
        self.new = 0
        self.updated = 0

    def upsert(self, values, via, timestamp):
        row = self.rows.get(values['doi'])
        if row is None:
            row = dict.fromkeys(self.fields, '')
            row['first_seen'] = timestamp
            self.rows[values['doi']] = row
            self.new += 1
        else:
            self.updated += 1
        for k, v in values.items():
            if v != '' or k not in REFRESH:
                row[k] = v
        vias = [v for v in (row.get('retrieved_via') or '').split('; ') if v]
        if via not in vias:
            vias.append(via)
        row['retrieved_via'] = '; '.join(vias)
        row['last_seen'] = timestamp

    def save(self):
        directory = os.path.dirname(os.path.abspath(self.path))
        fd, tmp = tempfile.mkstemp(dir=directory, suffix='.tmp')
        with os.fdopen(fd, 'w', newline='', encoding='utf-8') as fh:
            writer = csv.DictWriter(fh, fieldnames=self.fields, extrasaction='ignore')
            writer.writeheader()
            for row in sorted(self.rows.values(),
                              key=lambda r: (r.get('publication_date') or '', r['doi'])):
                writer.writerow(row)
        os.replace(tmp, self.path)


def append_runlog(path, entry):
    with open(path, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + '\n')


# --------------------------------------------------------------------------
# Subcommands
# --------------------------------------------------------------------------

def cmd_probe(args):
    """Each test is compared with a baseline that uses the SAME search term,
    so a smaller total really means the extra constraint did something."""
    client = make_client(args)
    eu, egd = '"European Union"', '"European Green Deal"'
    issn, jname = None, None
    if args.journals and os.path.exists(args.journals):
        try:
            jname, issns = load_journals(args.journals, args.kbart, args.cache_dir)[0]
            issn = issns[0]
        except SystemExit as err:
            log('(skipping ISSN test: %s)' % err)

    # key, label, query
    tests = [
        ('eu', 'A  "European Union", all of Springer Nature', eu),
        ('issn', 'B  A + issn:%s (%s)' % (issn, jname), '%s AND issn:%s' % (eu, issn)),
        ('title', 'C  title:"European Union"', 'title:' + eu),
        ('egd', 'D  "European Green Deal"', egd),
        ('egd_from', 'E  D + datefrom:2021-01-01', egd + ' AND datefrom:2021-01-01'),
        ('egd_to', 'F  D + dateto:2019-11-30 (before the EGD existed)', egd + ' AND dateto:2019-11-30'),
        ('ack', 'G  "Horizon 2020 research and innovation programme"',
         '"Horizon 2020 research and innovation programme"'),
    ]
    if not issn:
        tests = [t for t in tests if t[0] != 'issn']

    log('Probing Meta API v2 (one request per test)...\n')
    totals, oa_doi = {}, None
    print('%-56s %10s  %s' % ('test', 'total', 'abstract'))
    for key, label, q in tests:
        try:
            data = json.loads(client.get(META_URL, {'q': q, 's': 1, 'p': 5}))
        except ApiError as err:
            totals[key] = None
            print('%-56s %10s  %s' % (label, '-', 'ERROR ' + str(err).splitlines()[0][:12]))
            continue
        totals[key] = int(((data.get('result') or [{}])[0]).get('total') or 0)
        recs = [parse_record(r) for r in data.get('records') or []]
        has_abs = 'yes' if recs and recs[0]['abstract'] else ('no' if recs else '-')
        print('%-56s %10d  %s' % (label, totals[key], has_abs))
        if not oa_doi:
            oa_doi = next((r['doi'] for r in recs if r['open_access'] == 'true'), None)

    def verdict(key, base, works, fails):
        if totals.get(key) is None or totals.get(base) is None:
            return 'not supported on your plan (error)' if totals.get(key) is None else '?'
        return works if totals[key] < 0.5 * totals[base] else fails

    print('\nWhat this means:')
    if issn:
        print('  Journal filter (issn:)   ' + verdict(
            'issn', 'eu', 'WORKS (B much smaller than A)',
            'PROBLEM: B is not much smaller than A, so issn: may be ignored'))
    print('  title: constraint        ' + verdict(
        'title', 'eu', 'WORKS (C much smaller than A)', 'no clear effect'))
    if totals.get('egd_to') is not None and totals.get('egd'):
        print('  dateto: filter           ' + (
            'WORKS (F is near zero)' if totals['egd_to'] < 0.05 * totals['egd']
            else 'no clear effect (F should be near zero)'))
    if totals.get('ack') is not None:
        print('  Search scope             ' + (
            'FULL TEXT is searched (G is large: acknowledgement wording matches). '
            'Expect many incidental hits; use the ta_relevant column to filter.'
            if totals['ack'] > 500 else
            'looks like metadata only (G is small)'))

    oa_client = make_client(args, key_env='SPRINGER_OA_KEY')
    oa_client.count = client.count
    if oa_doi:
        try:
            xml = oa_client.get(OA_JATS_URL, {'q': 'doi:%s' % oa_doi}, accept='application/xml')
            text, status = extract_jats_body(xml)
            print('  Open Access full text    %s for %s (%d words)'
                  % (status, oa_doi, len(text.split())))
        except ApiError as err:
            print('  Open Access full text    ERROR: %s' % str(err).splitlines()[0])
    print('\nRequests used: %d' % oa_client.count)


def cmd_journals(args):
    journals = load_journals(args.journals, args.kbart, args.cache_dir)
    out = args.out or os.path.splitext(args.journals)[0] + '_resolved.txt'
    with open(out, 'w', encoding='utf-8') as fh:
        fh.write('# Resolved %s on %s\n' % (args.journals, now()))
        for name, issns in journals:
            fh.write('%s | %s\n' % (name, ', '.join(issns)))
            print('%-48s %s' % (name, ', '.join(issns)))
    log('\nwrote %s (use it with: search --journals %s)' % (out, out))


def cmd_search(args):
    cfg = read_sections(args.queries)
    topic = build_topic_query(cfg)
    matcher = Matcher(cfg)
    journals = load_journals(args.journals, args.kbart, args.cache_dir)
    if args.only:
        wanted = {norm_name(n) for n in args.only}
        journals = [j for j in journals if norm_name(j[0]) in wanted]
        if not journals:
            sys.exit('--only matched no journal in %s' % args.journals)

    queries = []
    for name, issns in journals:
        q = '%s AND (%s)' % (topic, ' OR '.join('issn:' + i for i in issns))
        if args.server_dates and args.date_from:
            q += ' AND datefrom:%s' % args.date_from
        if args.server_dates and args.date_to:
            q += ' AND dateto:%s' % args.date_to
        if args.extra:
            q += ' AND ' + args.extra
        queries.append((name, issns, q))

    if args.dry_run:
        for name, _, q in queries:
            print('## %s  (%d chars)\n%s\n' % (name, len(q), q))
        return

    client = make_client(args)
    ds = Dataset(args.corpus, matcher.tags.keys())
    timestamp = now()
    qhash = hashlib.sha1(topic.encode('utf-8')).hexdigest()[:10]
    stopped = None

    for name, issns, q in queries:
        s, total, seen, kept, relevant = 1, 0, 0, 0, 0
        dropped = {'not_nature_doi': 0, 'before_from': 0, 'after_to': 0, 'no_doi': 0}
        try:
            while True:
                data = json.loads(client.get(META_URL, {'q': q, 's': s, 'p': args.page_size}))
                total = int(((data.get('result') or [{}])[0]).get('total') or 0)
                recs = data.get('records') or []
                if s == 1 and total > args.warn_total:
                    log('  WARNING: %s reports %d hits -- the ISSN filter may be ignored. '
                        'Check with "probe".' % (name, total))
                for rec in recs:
                    seen += 1
                    row = parse_record(rec)
                    if not row['doi']:
                        dropped['no_doi'] += 1
                        continue
                    if not row['doi'].startswith(NATURE_DOI_PREFIX):
                        dropped['not_nature_doi'] += 1
                        continue
                    date = row['publication_date'] or row['online_date']
                    if args.date_from and date and date < args.date_from:
                        dropped['before_from'] += 1
                        continue
                    if args.date_to and date and date > args.date_to:
                        dropped['after_to'] += 1
                        continue
                    ta_text = row['title'] + ' \n ' + row['abstract']
                    found, tags = matcher.match(ta_text)
                    is_rel = matcher.relevant(ta_text)
                    row['ta_relevant'] = 'yes' if is_rel else 'no'
                    relevant += is_rel
                    row['matched_terms'] = '; '.join(found)
                    row['n_matched_terms'] = str(len(found))
                    for tag, n in tags.items():
                        row['tag_' + tag] = str(n)
                    ds.upsert(row, name, timestamp)
                    kept += 1
                s += len(recs)
                if not recs or s > total:
                    break
        except BudgetExhausted as err:
            stopped = '%s (stopped in %s at record %d of %d)' % (err, name, s, total)
        except ApiError as err:
            stopped = str(err)
        ds.save()
        log('%-45s reported %5d  retrieved %5d  kept %5d  relevant in title/abstract %5d'
            % (name, total, seen, kept, relevant))
        append_runlog(args.runlog, {
            'time': timestamp, 'command': 'search', 'journal': name, 'issns': issns,
            'query': q, 'query_hash': qhash, 'reported_total': total, 'retrieved': seen,
            'kept': kept, 'ta_relevant': relevant, 'dropped': dropped, 'date_from': args.date_from,
            'date_to': args.date_to, 'complete': stopped is None,
        })
        if stopped:
            break

    log('\n%d new, %d updated, %d documents in %s; %d API requests'
        % (ds.new, ds.updated, len(ds.rows), args.corpus, client.count))
    if stopped:
        log('STOPPED: %s\nRe-run later; use --only to continue with the remaining journals.'
            % stopped)
        sys.exit(2)


SKIP_JATS = {'fig', 'table-wrap', 'disp-formula', 'inline-formula', 'fn-group', 'ref-list'}
DROP_INLINE = {'xref', 'inline-formula'}


def _tag(el):
    return el.tag.rsplit('}', 1)[-1] if isinstance(el.tag, str) else ''


def _text_of(el):
    parts = [el.text or '']
    for child in el:
        if _tag(child) not in DROP_INLINE:
            parts.append(_text_of(child))
        parts.append(child.tail or '')
    return ''.join(parts)


def _walk(el, out):
    for child in el:
        tag = _tag(child)
        if tag in SKIP_JATS:
            continue
        if tag in ('title', 'p'):
            text = ' '.join(_text_of(child).split())
            if text:
                out.append(text)
        else:
            _walk(child, out)


def extract_jats_body(xml_bytes):
    """Returns (plain text of the article body, status)."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return '', 'bad_xml'
    article = next((el for el in root.iter() if _tag(el) == 'article'), None)
    if article is None:
        return '', 'not_in_oa'
    body = next((el for el in article.iter() if _tag(el) == 'body'), None)
    if body is None:
        return '', 'no_body'
    out = []
    _walk(body, out)
    return '\n\n'.join(out), ('ok' if out else 'no_body')


def safe_name(doi):
    return re.sub(r'[^\w.-]+', '_', doi)


def cmd_fulltext(args):
    client = make_client(args, key_env='SPRINGER_OA_KEY')
    ds = Dataset(args.corpus)
    if not ds.rows:
        sys.exit('%s is empty -- run "search" first.' % args.corpus)
    os.makedirs(args.out_dir, exist_ok=True)
    done_states = {'ok', 'no_body', 'not_in_oa'}
    todo = [r for r in ds.rows.values()
            if (args.all or r.get('open_access') == 'true')
            and (args.refetch or r.get('fulltext_status') not in done_states)]
    log('%d articles to fetch (%s)' % (len(todo), 'all' if args.all else 'open access only'))
    counts, stopped = {}, None
    for i, row in enumerate(todo, 1):
        try:
            xml = client.get(OA_JATS_URL, {'q': 'doi:%s' % row['doi']}, accept='application/xml')
        except BudgetExhausted as err:
            stopped = str(err)
            break
        except ApiError as err:
            stopped = str(err)
            break
        text, status = extract_jats_body(xml)
        counts[status] = counts.get(status, 0) + 1
        row['fulltext_status'] = status
        if status == 'ok':
            base = os.path.join(args.out_dir, safe_name(row['doi']))
            with open(base + '.xml', 'wb') as fh:
                fh.write(xml)
            with open(base + '.txt', 'w', encoding='utf-8') as fh:
                fh.write(row['title'] + '\n\n' + text + '\n')
            row['fulltext_file'] = base + '.txt'
            row['fulltext_words'] = str(len(text.split()))
        if i % 20 == 0:
            ds.save()
            log('  %d/%d %s' % (i, len(todo), counts))
    ds.save()
    append_runlog(args.runlog, {'time': now(), 'command': 'fulltext', 'attempted': len(todo),
                                'results': counts, 'complete': stopped is None})
    log('done: %s; %d API requests' % (counts, client.count))
    if stopped:
        log('STOPPED: %s\nRe-run later; finished articles are skipped.' % stopped)
        sys.exit(2)


# --------------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--api-key', help='Springer Nature API key (default: $SPRINGER_API_KEY)')
    common.add_argument('--journals', default='journals.txt',
                        help='journal list, one per line, optionally "Name | ISSN, ISSN"')
    common.add_argument('--kbart', default=DEFAULT_KBART,
                        help='URL or path of the nature.com KBART title list (for ISSN lookup)')
    common.add_argument('--cache-dir', default='.', help='where to cache the KBART list')
    common.add_argument('--corpus', default='corpus.csv', help='output dataset')
    common.add_argument('--runlog', default='runs.jsonl', help='run log for the methods section')
    common.add_argument('--delay', type=float, default=1.0,
                        help='seconds between requests (Basic plan allows ~100/min)')
    common.add_argument('--max-requests', type=int, default=450,
                        help='stop after this many API requests this run (Basic plan: ~500/day)')

    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('probe', parents=[common], help='test which query features your plan supports')

    sp = sub.add_parser('journals', parents=[common], help='resolve journal names to ISSNs')
    sp.add_argument('--out', help='output file (default: journals_resolved.txt)')

    sp = sub.add_parser('search', parents=[common], help='query the Meta API into corpus.csv')
    sp.add_argument('--queries', default='queries.txt', help='query/tag term file')
    sp.add_argument('--from', dest='date_from', default='2015-01-01',
                    help='keep articles published on/after this date (YYYY-MM-DD)')
    sp.add_argument('--to', dest='date_to', default=None, help='keep articles up to this date')
    sp.add_argument('--extra', help='extra API constraint(s) ANDed to every query, '
                                    'e.g. "datefrom:2015-01-01" if probe shows it works')
    sp.add_argument('--server-dates', action='store_true',
                    help='also send --from/--to to the API as datefrom:/dateto: '
                         '(saves requests; probe shows whether they work)')
    sp.add_argument('--only', action='append', metavar='JOURNAL',
                    help='run only this journal (repeatable)')
    sp.add_argument('--page-size', type=int, default=25,
                    help='records per request (Basic max 25, Premium max 100)')
    sp.add_argument('--warn-total', type=int, default=3000,
                    help='warn if one journal reports more hits than this')
    sp.add_argument('--dry-run', action='store_true', help='print the queries and exit')

    sp = sub.add_parser('fulltext', parents=[common], help='fetch open-access full text')
    sp.add_argument('--out-dir', default='fulltext', help='where to save .xml and .txt files')
    sp.add_argument('--all', action='store_true',
                    help='try every article, not only those flagged open access')
    sp.add_argument('--refetch', action='store_true', help='fetch again even if done before')

    args = p.parse_args(argv)
    {'probe': cmd_probe, 'journals': cmd_journals,
     'search': cmd_search, 'fulltext': cmd_fulltext}[args.cmd](args)


if __name__ == '__main__':
    main()

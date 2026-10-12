#!/usr/bin/env python3
"""Found footage finder.

Two subcommands, standard library only:

  python find_footage.py search  [--date YYYY-MM-DD] [--out candidates.json]
      Query the Internet Archive with rotating queries built from taste.md's
      interests, filter and de-duplicate, pre-rank, and write candidates.json
      split into "aligned" and "wildcard" buckets.

  python find_footage.py digest  --picks picks.json [--date YYYY-MM-DD]
      Turn judged picks into digests/YYYY-MM-DD.md and record them in seen.json
      so they never repeat.

The judging step (reading the candidates against taste.md and choosing 3 aligned
plus 2 wildcards) is done by Claude in the scheduled run, because "emotionally
evoking" is a judgment call. This script does retrieval, hygiene and pre-ranking.
"""

import argparse
import datetime as dt
import hashlib
import json
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEEN_PATH = HERE / "seen.json"
DIGEST_DIR = HERE / "digests"
IA_SEARCH = "https://archive.org/advancedsearch.php"

# --------------------------------------------------------------------------
# Taste: query phrases. Edit freely. "aligned" lean 1980-2009, "wildcard" drifts.
# --------------------------------------------------------------------------

ALIGNED_QUERIES = [
    # institutional footage that unknowingly shows an ideology (strongest signal)
    '"office of the future"', '"computer of the future"', '"training video" computer',
    '"corporate video" 1990s', '"trade show" demonstration', '"product demonstration" prototype',
    '"virtual reality" demonstration', '"cyberspace" documentary', '"internet" introduction tape',
    '"robot" demonstration', '"multimedia" CD-ROM promo', '"teleconference" demonstration',
    '"computer animation" early', '"screen saver" OR "demo reel" computer graphics',
    '"video wall" OR "kiosk" promotional', '"futurist" OR "tomorrow" corporate film',
    # unselfconscious / unplaceable
    '"home video" camcorder unedited', '"security camera" footage tape', '"surveillance" tape 1990s',
    '"VHS" "found" tape', '"public access" television 1990s', '"local news" blooper outtake',
    '"outtakes" video unedited', '"raw footage" street', '"b-roll" unused',
    # medium texture
    '"tape" damaged OR degraded video', '"static" OR "tracking" VHS glitch', '"analog video" synthesizer',
    '"video art" 1980s', '"broadcast" test pattern signal',
    # ordinary things behaving oddly
    '"weather" strange footage amateur', '"phenomenon" unexplained amateur video',
    '"experiment" laboratory film 1980s', '"factory" machine footage industrial',
    # east asia / diaspora, made from inside
    '"Hong Kong" 1990s video', '"Beijing" 1990s video', '"Shenzhen" OR "Guangzhou" footage 1990s',
    '"Chinatown" home video', '"Taiwan" 1990s video tape', '"Shanghai" 1990s documentary',
]

WILDCARD_QUERIES = [
    '"silent film" actuality', '"newsreel" unused', '"nitrate" fragment film',
    '"1930s" amateur film', '"1950s" home movie', '"1960s" industrial film', '"1970s" educational film',
    '"ritual" film documentation', '"festival" amateur film', '"dance" film archive',
    '"puppet" OR "marionette" film', '"mask" ceremony film', '"opera" backstage film',
    '"scientific" film microscope', '"astronomy" observatory film', '"deep sea" OR "cave" expedition film',
    '"craft" OR "workshop" process film', '"mine" OR "quarry" footage', '"market" street film',
    '"2000s" webcam OR "early YouTube"', '"mobile phone" video 2005', '"dashcam" OR "CCTV" 2000s',
    '"language lesson" OR "classroom" film', '"sleep" OR "dream" experiment film',
]

# Words that nudge the pre-score up or down (judge makes the real call).
BOOST = [
    "demonstration", "prototype", "future", "tomorrow", "experimental", "unedited", "raw",
    "outtake", "outtakes", "surveillance", "camcorder", "home video", "training", "corporate",
    "trade show", "kiosk", "virtual reality", "cyberspace", "multimedia", "synthesizer",
    "tape", "analog", "glitch", "unexplained", "strange", "found", "rehearsal", "test",
    "factory", "machine", "robot", "laboratory", "interface", "public access", "amateur",
]
PENALTY = [
    "trailer", "official video", "music video", "full movie", "feature film", "cartoon",
    "commercial compilation", "best of", "top 10", "nostalgia", "remember when", "retro",
    "reaction", "vlog", "tutorial", "gameplay", "walkthrough", "anime", "sermon",
]

MAX_DOWNLOADS = 15000      # "not viral": skip clips the internet already found
MIN_DOWNLOADS = 3          # skip things nobody ever looked at? no, keep very obscure, but not zero-signal junk
MIN_DESC_CHARS = 0
ROWS_PER_QUERY = 40
QUERIES_PER_BUCKET = {"aligned": 7, "wildcard": 5}


def log(msg):
    print(msg, file=sys.stderr)


def load_seen():
    if SEEN_PATH.exists():
        try:
            return json.loads(SEEN_PATH.read_text())
        except json.JSONDecodeError:
            log("seen.json unreadable; starting fresh")
    return {"ids": {}}


def save_seen(seen):
    SEEN_PATH.write_text(json.dumps(seen, indent=2, ensure_ascii=False))


def http_get_json(url, retries=3, timeout=30):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "found-footage-finder/1.0 (personal art research)"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:  # network errors, bad JSON
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET failed: {url[:120]}... ({last})")


def ia_url(query, year_from, year_to, rows=ROWS_PER_QUERY, page=1):
    q = f"({query}) AND mediatype:(movies)"
    if year_from and year_to:
        q += f" AND date:[{year_from}-01-01 TO {year_to}-12-31]"
    params = [
        ("q", q),
        ("fl[]", "identifier"), ("fl[]", "title"), ("fl[]", "description"), ("fl[]", "date"),
        ("fl[]", "downloads"), ("fl[]", "collection"), ("fl[]", "creator"), ("fl[]", "runtime"),
        ("rows", str(rows)), ("page", str(page)), ("output", "json"),
        ("sort[]", "downloads asc"),   # obscure first: the opposite of "trending"
    ]
    return IA_SEARCH + "?" + urllib.parse.urlencode(params)


def ia_search(query, year_from, year_to, rows=ROWS_PER_QUERY, page=1):
    data = http_get_json(ia_url(query, year_from, year_to, rows, page))
    if "response" not in data:   # e.g. {"error": "[BACKEND_ERROR] ..."}: a failure, not zero results
        raise RuntimeError(f"archive.org error: {str(data.get('error', data))[:160]}")
    return data["response"].get("docs", [])


def flat(v):
    if v is None:
        return ""
    if isinstance(v, list):
        return " ".join(str(x) for x in v)
    return str(v)


def clean(text, n=400):
    text = re.sub(r"<[^>]+>", " ", flat(text))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:n]


def parse_runtime(r):
    """Return seconds or None from IA runtime strings like '00:12:31' or '754.2'."""
    r = flat(r).strip()
    if not r:
        return None
    try:
        if ":" in r:
            parts = [float(p) for p in r.split(":")]
            secs = 0
            for p in parts:
                secs = secs * 60 + p
            return secs
        return float(r)
    except ValueError:
        return None


def year_of(date):
    m = re.match(r"(\d{4})", flat(date))
    return int(m.group(1)) if m else None


def prescore(doc, bucket):
    text = (flat(doc.get("title")) + " " + clean(doc.get("description"), 800)).lower()
    s = 0.0
    for w in BOOST:
        if w in text:
            s += 1.0
    for w in PENALTY:
        if w in text:
            s -= 2.0
    dl = int(doc.get("downloads") or 0)
    # obscurity bonus: log-ish, max ~2
    if dl < 50:
        s += 2.0
    elif dl < 500:
        s += 1.2
    elif dl < 3000:
        s += 0.5
    else:
        s -= 0.5
    y = year_of(doc.get("date"))
    if bucket == "aligned" and y and 1980 <= y <= 2009:
        s += 1.0
    if bucket == "wildcard" and y and not (1980 <= y <= 2009):
        s += 1.0
    if not clean(doc.get("description")):
        s -= 0.8   # no description: harder for the judge, but can be pure found footage
    return round(s, 2)


def to_candidate(doc, bucket, query):
    ident = doc["identifier"]
    return {
        "id": f"ia:{ident}",
        "source": "internet_archive",
        "bucket": bucket,
        "query": query,
        "title": clean(doc.get("title"), 200),
        "description": clean(doc.get("description"), 500),
        "date": flat(doc.get("date"))[:10],
        "creator": clean(doc.get("creator"), 120),
        "collection": flat(doc.get("collection"))[:160],
        "downloads": int(doc.get("downloads") or 0),
        "runtime_s": parse_runtime(doc.get("runtime")),
        "url": f"https://archive.org/details/{ident}",
        "thumb": f"https://archive.org/services/img/{ident}",
        "prescore": prescore(doc, bucket),
    }


def pick_queries(pool, n, seed):
    rng = random.Random(seed)
    qs = pool[:]
    rng.shuffle(qs)
    return qs[:n]


def add_docs(candidates, seen_ids, docs, bucket, query):
    """Apply hygiene filters to raw Internet Archive docs; add survivors to candidates."""
    kept = 0
    for d in docs:
        ident = d.get("identifier")
        if not ident or f"ia:{ident}" in seen_ids or f"ia:{ident}" in candidates:
            continue
        try:
            dl = int(d.get("downloads") or 0)
        except (TypeError, ValueError):
            dl = 0
        if dl > MAX_DOWNLOADS:
            continue
        c = to_candidate(d, bucket, query)
        rt = c["runtime_s"]
        if rt is not None and (rt < 8 or rt > 3 * 3600):
            continue
        if not c["title"]:
            continue
        candidates[c["id"]] = c
        kept += 1
    return kept


def finalize(candidates, errors, day, out_path, keep):
    ranked = {"aligned": [], "wildcard": []}
    for c in candidates.values():
        ranked[c["bucket"]].append(c)
    for b in ranked:
        ranked[b].sort(key=lambda c: c["prescore"], reverse=True)
        ranked[b] = ranked[b][:keep]
    out = {
        "date": day.isoformat(),
        "errors": errors,
        "counts": {b: len(v) for b, v in ranked.items()},
        "candidates": ranked,
    }
    Path(out_path).write_text(json.dumps(out, indent=2, ensure_ascii=False))
    log(f"wrote {out_path}: {out['counts']}, {len(errors)} query errors")
    if not candidates and errors:
        return 2   # nothing reachable: caller should say so, not fake a digest
    return 0


def todays_plan(day):
    seed = int(hashlib.sha256(day.isoformat().encode()).hexdigest(), 16) % (2**31)
    plan = []
    for bucket, pool, y0, y1, off in (
        ("aligned", ALIGNED_QUERIES, 1980, 2009, 0),
        ("wildcard", WILDCARD_QUERIES, None, None, 1),
    ):
        for q in pick_queries(pool, QUERIES_PER_BUCKET[bucket], seed + off):
            plan.append((bucket, q, y0, y1))
    return plan


def cmd_queries(args):
    """Print today's queries and their API URLs, for when the shell can't reach archive.org
    and the URLs have to be fetched some other way (e.g. a web-fetch tool)."""
    day = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    rows = [{"bucket": b, "query": q, "url": ia_url(q, y0, y1)} for b, q, y0, y1 in todays_plan(day)]
    print(json.dumps(rows, indent=2, ensure_ascii=False))
    return 0


def cmd_ingest(args):
    """Same filtering and ranking as `search`, but from docs fetched elsewhere.
    --raw file shape: {"results": [{"bucket": "aligned|wildcard", "query": "...", "docs": [ ... ]}]}"""
    day = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    raw = json.loads(Path(args.raw).read_text())
    seen_ids = set(load_seen()["ids"])
    candidates, errors = {}, list(raw.get("errors", []))
    for r in raw.get("results", []):
        if r.get("bucket") not in ("aligned", "wildcard"):
            errors.append(f"bad bucket in result for {r.get('query')!r}")
            continue
        kept = add_docs(candidates, seen_ids, r.get("docs", []), r["bucket"], r.get("query", ""))
        log(f"  {r['bucket']:8s} {str(r.get('query'))[:48]:48s} -> {kept} kept of {len(r.get('docs', []))}")
    return finalize(candidates, errors, day, args.out, args.keep)


def cmd_search(args):
    day = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    seed = int(hashlib.sha256(day.isoformat().encode()).hexdigest(), 16) % (2**31)
    seen = load_seen()
    seen_ids = set(seen["ids"])
    candidates, errors = {}, []

    plan = [
        ("aligned", ALIGNED_QUERIES, 1980, 2009),
        ("wildcard", WILDCARD_QUERIES, None, None),
    ]
    for bucket, pool, y0, y1 in plan:
        for q in pick_queries(pool, QUERIES_PER_BUCKET[bucket], seed + (0 if bucket == "aligned" else 1)):
            try:
                docs = ia_search(q, y0, y1)
            except Exception as e:
                errors.append(f"{bucket} {q}: {e}")
                log(f"  ! {q}: {e}")
                continue
            kept = add_docs(candidates, seen_ids, docs, bucket, q)
            log(f"  {bucket:8s} {q[:48]:48s} -> {kept} kept of {len(docs)}")
            time.sleep(0.7)   # be polite to archive.org

    return finalize(candidates, errors, day, args.out, args.keep)


def cmd_digest(args):
    day = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    picks = json.loads(Path(args.picks).read_text())
    items = picks["picks"]
    DIGEST_DIR.mkdir(exist_ok=True)
    lines = [f"# Found footage, {day.strftime('%A %B %d, %Y')}", ""]
    lines.append("Watch each one and decide for yourself which parts land. "
                 "Then add a line to taste.md's feedback log with the why.")
    lines.append("")
    for i, p in enumerate(items, 1):
        tag = "wildcard" if p.get("bucket") == "wildcard" else "aligned"
        lines.append(f"## {i}. {p['title']}  ({tag})")
        lines.append("")
        meta = [x for x in (p.get("date"), p.get("creator"), p.get("collection_hint")) if x]
        if p.get("runtime_s"):
            meta.append(f"{int(p['runtime_s'] // 60)}m{int(p['runtime_s'] % 60):02d}s")
        if meta:
            lines.append(" · ".join(meta))
            lines.append("")
        lines.append(f"[Watch on Internet Archive]({p['url']})")
        lines.append("")
        lines.append(f"Why it was picked: {p['why']}")
        if p.get("look_for"):
            lines.append("")
            lines.append(f"Look for: {p['look_for']}")
        lines.append("")
    path = DIGEST_DIR / f"{day.isoformat()}.md"
    path.write_text("\n".join(lines))

    seen = load_seen()
    for p in items:
        seen["ids"][p["id"]] = {"date": day.isoformat(), "title": p["title"]}
    save_seen(seen)
    print(path)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search")
    s.add_argument("--date")
    s.add_argument("--out", default=str(HERE / "candidates.json"))
    s.add_argument("--keep", type=int, default=25, help="candidates kept per bucket")
    s.set_defaults(fn=cmd_search)
    d = sub.add_parser("digest")
    d.add_argument("--picks", required=True)
    d.add_argument("--date")
    d.set_defaults(fn=cmd_digest)
    q = sub.add_parser("queries")
    q.add_argument("--date")
    q.set_defaults(fn=cmd_queries)
    i = sub.add_parser("ingest")
    i.add_argument("--raw", required=True)
    i.add_argument("--date")
    i.add_argument("--out", default=str(HERE / "candidates.json"))
    i.add_argument("--keep", type=int, default=25)
    i.set_defaults(fn=cmd_ingest)
    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()

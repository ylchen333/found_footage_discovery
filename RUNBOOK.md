# Found footage discovery: runbook for Claude Code

Hand this file to a Claude Code cloud session running in the `my_default` environment.

## What this does

Every morning, find 5 pieces of found footage from the Internet Archive (and, once reachable, other open archives) that fit Lorie's taste, and write a **digest of links only**. She watches each clip herself and decides which parts she likes. Never download, embed, or process video. Never summarize what footage "means".

## Files (saved in the memory_abc_scifi Project under `found_footage/`)

| File | Purpose |
|---|---|
| `find_footage.py` | Standard-library Python. Subcommands: `search`, `queries`, `ingest`, `digest` |
| `taste.md` | Taste profile, judging rubric and the **Feedback log** at the bottom. Read it fresh every run |
| `seen.json` | Clip ids already used, so nothing repeats |
| `digests/YYYY-MM-DD.md` | One digest per day |

If the files aren't in the working directory, restore them with `project_read` (or copy from `/mnt/project-files` if mounted) into a folder called `found_footage/`.

## Step 0: check the environment first

The first run showed that archive.org is blocked by default. In `my_default`, **Network access must be Custom** (keep "Also include default list of common package managers" checked) with these allowed domains, one per line:

```
archive.org
*.archive.org
www.loc.gov
commons.wikimedia.org
```

Test it:

```
curl -s -o /dev/null -w "%{http_code}\n" "https://archive.org/advancedsearch.php?q=prelinger&rows=1&output=json"
```

- `200` means the shell can reach it. Go to Step 1.
- `000` or a "CONNECT tunnel failed, response 403" error means the domain is blocked by the egress policy. Do not retry or work around it (no mirrors, caches or proxies). Use the Step 1b fallback and tell Lorie which host was blocked.

Changing a project's environment only reaches **new** threads, so start a fresh thread after the change.

## Step 1: retrieve candidates

**1a. Normal path**

```
python3 find_footage.py search
```

It runs 7 aligned and 5 wildcard Internet Archive queries (rotated daily from a date-seeded shuffle), drops anything already in `seen.json`, anything with more than 15,000 downloads (the anti-viral filter), anything under 8 seconds or over 3 hours, and anything without a title. It then pre-ranks by keyword and obscurity and writes `candidates.json`. Exit 0 means success. Exit 2 means nothing was reachable.

**1b. Fallback when the shell can't reach archive.org**

```
python3 find_footage.py queries          # prints today's queries with their API URLs
```

WebFetch each URL, asking for the `docs` array verbatim (identifier, title, description, date, downloads, creator, runtime, collection). Assemble `raw.json`:

```json
{"results": [{"bucket": "aligned", "query": "...", "docs": [ ... ]}]}
```

then run:

```
python3 find_footage.py ingest --raw raw.json
```

WebFetch summarizes pages, so descriptions arrive paraphrased. The digest must say so in its first line. If WebFetch is also blocked, **stop**: send Lorie a short message naming the blocked host and the error, write no digest, and don't touch `seen.json`. Never invent clips.

## Step 2: judge

1. Read `taste.md` in full, including the Feedback log. Later feedback overrides earlier assumptions. If the log has entries, say in the digest's first line how they shifted today's picks.
2. Read `candidates.json`. Choose **5 picks: 3 from `aligned`, 2 from `wildcard`**.
3. Score against the rubric in `taste.md`: unselfconsciousness, maker/viewer gap, texture, strangeness, emotional charge. Penalties: polished or viral, nostalgia bait, heavy narration. Corporate, industrial, trade-show and demo footage from the 1980s–2000s is a high prior.
4. Spread the picks across eras, registers and collections. Wildcards must deliberately miss the taste profile (different era, region or genre).
5. Use only facts from the record's title, description and metadata. If a pick is too thin to judge, fetch its `archive.org/details/...` page. Phrase uncertainty as uncertainty.
6. Write `picks.json`:

```json
{"picks": [{"id": "ia:<identifier>", "title": "", "bucket": "aligned|wildcard",
  "date": "", "creator": "", "runtime_s": 0, "url": "", "why": "", "look_for": ""}]}
```

`why` is one or two sentences on which rubric traits it hits, without pre-explaining the footage. `look_for` is one concrete thing to watch for.

## Step 3: write and save the digest

```
python3 find_footage.py digest --picks picks.json
```

This writes `digests/YYYY-MM-DD.md` and records the picks in `seen.json`. Then:

1. `project_write` the digest to `found_footage/digests/YYYY-MM-DD.md` (with `present_to_user: true`).
2. `project_write` the updated `found_footage/seen.json`.
3. Send the digest to Lorie with `SendUserFile`.
4. Final message: the five titles, one line each, tagged aligned or wildcard. Nothing else.

## Scheduling

- A scheduled task already exists: **"Found footage digest"**, id `trig_01C3HFy5fMmbGf8rk2zB7D8n`, daily at 6:56am America/Detroit.
- Its environment defaults to the environment of the session that created it. Check the routine editor and set it to `my_default`, or the scheduled run will keep hitting the same network block.
- It runs with automatic approval, which is another reason to keep the allowlist narrow (Custom, not Full).

## Verify before calling it done

- [ ] The curl test in Step 0 returns 200
- [ ] `search` exits 0 and `candidates.json` has candidates in both buckets
- [ ] Every link in the digest comes from a real result (spot-check two ids against archive.org)
- [ ] The digest has exactly 3 aligned and 2 wildcard picks
- [ ] `seen.json` gained the 5 new ids and the digest is saved in the Project

## Open work

1. **Secondary sources.** Add Library of Congress (`www.loc.gov`, JSON via `fo=json`) and Wikimedia Commons (MediaWiki API) as extra retrieval functions in `find_footage.py`. Verify each against a real response before writing the parser, since the field names haven't been confirmed. Reuse `add_docs`, `to_candidate` and the `bucket` convention. Keyed sources (US National Archives, Europeana, DPLA) come later and need free API keys.
2. **Taste updates.** Lorie adds lines to the Feedback log in `taste.md` ("liked X because Y"). The reason matters more than the verdict. When it grows, fold recurring patterns into the profile and adjust the `BOOST` and `PENALTY` lists and query pools in `find_footage.py`.
3. **Later ideas, not built:** keyframe embeddings (CLIP/SigLIP) for a real taste vector, and an MMR diversity pass across a week's picks.

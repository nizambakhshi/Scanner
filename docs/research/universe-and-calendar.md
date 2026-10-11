# NSE reference data: trading holiday calendar & Nifty 500 universe

Research snapshot retrieved **2026-10-11T17:27+05:30** (Asia/Kolkata). All findings below were
obtained by direct HTTP retrieval from NSE-owned properties; nothing on this page is extrapolated.

Artifacts produced:

| File | Contents |
| --- | --- |
| `/Users/mohdnizam/Desktop/Scanner/data/calendar/nse_holidays.json` | Equity trading holidays Oct–Dec 2026 + verified session timings |
| `/Users/mohdnizam/Desktop/Scanner/data/universe/nifty500_2026-10-11.csv` | Official constituent list, saved byte-verbatim |
| `/Users/mohdnizam/Desktop/Scanner/data/universe/nifty500_manifest.json` | Source, HTTP status, row count, columns, sha256, caveats |

---

## 1. Holiday calendar — VERIFIED, but only half-covered

### The source that actually works

The human page is <https://www.nseindia.com/resources/exchange-communication-holidays>. It renders
its holiday table with JavaScript, so the HTML contains no dates; scraping it is a dead end. The page
loads `/dist/js/sections/resources/ex-comm-holidays.js`, which reveals the real data endpoints:

```
GET https://www.nseindia.com/api/holiday-year-list
GET https://www.nseindia.com/api/holiday-master?type=trading&year=2026
```

Both returned **HTTP 200 / application/json** and are the exchange's own source of truth
(`source_type: "official"`). No secondary source (Groww / Zerodha / Bajaj listicles surfaced in search)
was needed or used.

Gotcha: these are Akamai-protected. A bare `curl` is rejected — you must first `GET` the holidays page
with a browser `User-Agent` to obtain the `ASP.NET_SessionId` cookie, then call the API with that cookie
jar **and** `Referer: https://www.nseindia.com/resources/exchange-communication-holidays`.

### The equity array is `CBM`

The response is keyed by segment: `CBM, CD, CM, CMOT, COM, EGR, FO, IRD, MF, NDM, NTRP, SLBS`.
`CBM` (20 entries) is the equity list. Verified that `CM`, `FO` and `NDM` are *byte-identical* to `CBM`,
while `EGR` is a different 10-entry list — so keying on any of the first four is safe for equity, but
`EGR` is not.

### Dates found for the requested window (Oct 2026 → Mar 2027)

| Date | Day | Reason |
| --- | --- | --- |
| 2026-10-20 | Tue | Dussehra |
| 2026-11-08 | Sun | Diwali Laxmi Pujan* |
| 2026-11-10 | Tue | Diwali-Balipratipada |
| 2026-11-24 | Tue | Prakash Gurpurb Sri Guru Nanak Dev |
| 2026-12-25 | Fri | Christmas |

### The gap: Jan–Mar 2027 does not exist yet

NSE publishes by **calendar year**, not financial year. `holiday-year-list` currently returns exactly
`{"year":["2026"]}` and `year=2027` returns `{}`. **The last quarter of the requested window is
unpublished by the exchange**, so `nse_holidays.json` deliberately contains no 2027 dates. This is the
one real correctness risk in the file: a scanner covering Feb/Mar 2027 will assume those days are
trading days until the CY2027 list lands. Treat the file as a flagged partial snapshot and re-run the
refresh.

### Half-days and Muhurat

* `morning_session` / `evening_session` are `null` for **all 20** equity entries (0 non-null), so NSE
  published **no half-day equity sessions** for CY2026.
* Verbatim from the NSE page: *"November 08, 2026, shall be a trading holiday on account of Diwali Laxmi
  Pujan. Muhurat Trading will be conducted on that day. Timings of Muhurat Trading shall be notified
  subsequently through a circular."* The muhurat window was still unannounced at retrieval time, so no
  times are recorded. 08-Nov is a Sunday regardless.

### Session timings — verified, and one assumption was wrong

Taken from <https://www.nseindia.com/static/market-data/market-timings> (Equities table), not assumed:

| Session | Time |
| --- | --- |
| Block Deal 1 | 08:45–09:00 |
| Pre-open (Regular) | 09:00–**09:08** |
| Special pre-open (SPOS) | 09:00–09:45 |
| Normal / Odd lot | 09:15–15:30 |
| Auction market | 14:00–14:30 |
| Block Deal 2 | 14:05–14:20 |
| **Closing session** | **15:40–16:00** |
| Trade-modification cut-off | 16:15 |
| T+0 market | 09:15–13:30 |

`post_close_end` is therefore recorded as **16:00**, not the `15:40` originally proposed in the spec —
15:40 is the *open* of NSE's closing session, not its end. Continuous trading still ends at 15:30, so if
the scanner uses `post_close_end` as a "stop emitting signals" boundary, 15:30 is the meaningful number
and the 15:40–16:00 closing session only accepts market-on-close style orders. Flagging this for a
product decision rather than silently keeping either value.

---

## 2. Nifty 500 universe — VERIFIED and downloaded

### Working URL

```
GET https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv
```

**HTTP 200, `Content-Type: application/octet-stream`, `Content-Length: 32785`.** Saved verbatim; the
local file's sha256 (`2959bf20…f5cb3`) matches a second independent download, and it is byte-identical
to the copy originally fetched to `/tmp`.

The request requirement is subtle and worth scripting correctly. Tested with curl 8.7.1:

| User-Agent sent | Result |
| --- | --- |
| `curl/8.7.1` (default) | **fails** — `http_code 000`, curl exit 56, 0 bytes after 25 s |
| `Mozilla/5.0 … Chrome/120` | 200, 32785 bytes, octet-stream |
| header suppressed (`-H "User-Agent:"`) | 200, 32785 bytes, octet-stream |

So it is a **User-Agent string denylist, not a cookie or JS challenge** — no cookies, referer or auth
are needed. Just never ship the literal default curl UA.

### Columns

`Company Name, Industry, Symbol, Series, ISIN Code` — five fields, no quoting, no embedded commas
(all 501 rows parse to exactly NF=5). `Series` is the listing series (`EQ` 495 / `BE` 3 / `RR` 3),
**not** the cap tier.

### Row count: 501, not 500 — and why

The file has **501 constituent rows**, confirmed real (0 duplicate symbols, 0 malformed rows, byte count
matches `Content-Length`). The nominal 100/150/250 structure was then verified by set arithmetic against
three sibling files, all of which also return 200 octet-stream today:

| Leg | File | Rows |
| --- | --- | --- |
| Large | `IndexConstituent/ind_nifty100list.csv` | 100 |
| Mid | `IndexConstituent/ind_niftymidcap150list.csv` | 150 |
| Small | `IndexConstituent/ind_niftysmallcap250list.csv` | **251** |

`Nifty100 ∪ Midcap150 ∪ Smallcap250 == Nifty500` **exactly** — zero overlaps, zero unmatched symbols in
either direction. The 100 and 150 legs are as documented; the extra name comes from the **small-cap leg
currently holding 251 names**, which is what makes the parent 501. Note the mid-cap filename needs the
`midcap` infix — `ind_nifty150list.csv` is a soft-404.

Joining on these three files is the reproducible way to tag each constituent with its cap segment.

### URLs that do NOT work

| URL | Result |
| --- | --- |
| `archives.nseindia.com/IndIndices/Nifty500/NIFTY_500_List_Series1.csv` | HTTP **503**, `AkamaiGHost` |
| `nsearchives.nseindia.com/IndIndices/Nifty500/NIFTY_500_List_Series1.csv` | HTTP **503**, `AkamaiGHost` |
| `www.nseindia.com/content/indices/ind_nifty500list.csv` | HTTP **404** |
| `www.niftyindices.com/IndexLibrary/NIFTY500.csv` (and other `IndexLibrary`/`reports` guesses) | HTTP **200** but `text/html` "Error 404" page |
| `www.niftyindices.com/indices/equity/broad-market-indices/nifty-500-index` | 200 + soft-404; real path is `…/broad-based-indices/nifty-500` |

**Critical trap: niftyindices.com serves soft-404s with HTTP 200.** A status check alone will silently
persist an HTML error page as your universe. Validate `Content-Type == application/octet-stream` *and*
that line 1 is `Company Name,Industry,Symbol,Series,ISIN Code`.

---

## 3. Verified vs unverified

**Verified from official sources**

* All 5 Oct–Dec 2026 equity holiday dates, reasons and weekday labels (NSE holiday API, `CBM`).
* Equity session times 09:00 / 09:15 / 15:30 / 15:40–16:00 / 16:15 (NSE market-timings page).
* Absence of half-day sessions in CY2026 (all 20 `morning_session`/`evening_session` null).
* Nifty 500 CSV columns, 501 rows, sha256, and its exact equality with the 100∪150∪251 union.
* Which endpoints work and which are dead, each with an observed status code.

**Unverified / not available**

* **Jan–Mar 2027 holidays** — not published by NSE at all; `holiday-year-list` returns only `2026`.
* **Muhurat trading window for 08-Nov-2026** — NSE said timings come "subsequently through a circular".
* Whether the 501st small-cap name is intentional or a transient reconstitution artefact — the row is
  present in the official file and there is no exchange note explaining it.
* Cap-tier assignment *inside* `nifty500_2026-10-11.csv` — the file has no segment column; tiers only
  exist via the three sibling files.
* The Angel One SmartAPI / NSE India API MCP servers configured for this session were **not actually
  connected** (`mcp_list` resolves neither), so nothing here is cross-checked against SmartAPI
  `instrument_type` data. The 3 REITs (`BAGMANE`, `BIRET`, `EMBASSY`, `Series=RR`) and 3 `BE` names
  are the likely join-failure candidates and should be sanity-checked against SmartAPI when it is next
  reachable.

---

## 4. Refresh procedure to script

**A. Holidays — weekly, and mandatory again once CY2027 publishes**

1. `GET https://www.nseindia.com/resources/exchange-communication-holidays` with a browser UA + cookie
   jar (bootstrap).
2. `GET /api/holiday-year-list` with those cookies + the page as `Referer`.
3. For **every** year returned (today: `2026`; when `2027` appears, fetch it too),
   `GET /api/holiday-master?type=trading&year=<Y>`.
4. Read the `CBM` array. Map `tradingDate` `DD-MMM-YYYY` → `YYYY-MM-DD`, `description` → `reason`,
   set `segments: ["NSE Equity"]`.
5. If any entry has a non-null `morning_session`/`evening_session`, record it as a half-day in `notes`
   instead of flattening it into `holidays`.
6. Overwrite `data/calendar/nse_holidays.json`, bump `retrieved_at`. **Alert if the year list is missing
   a year that overlaps the scanner's forward window** — that is the Jan–Mar 2027 condition today.
7. Keep `source_type` as `official`; if this ever falls back to a news listing, change it to `secondary`.

**B. Universe — daily (cheap) or at minimum semi-annually**

1. `GET https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv` with a browser UA, no cookies.
2. Assert `http_status == 200`, `content_type == "application/octet-stream"`, and that line 1 equals the
   known header — otherwise the file is a soft-404 page and the run must abort, not write.
3. Assert every row has 5 fields and that `Symbol` is unique.
4. Write to a **new dated file** `nifty500_<YYYY-MM-DD>.csv`; never overwrite a prior snapshot, so
   reconstitution diffs stay auditable.
5. Recompute sha256, `row_count`, byte size and the `EQ/BE/RR` breakdown into the manifest.
6. Skip the write when sha256 is unchanged — the list is static between reconstitutions, so a hash
   change is itself the signal that reconstitution or an ad-hoc circular landed.
7. Also fetch `ind_nifty100list.csv`, `ind_niftymidcap150list.csv`, `ind_niftysmallcap250list.csv` and
   re-assert `union == nifty500`; if that equality breaks, NSE has changed methodology and the segment
   tagging logic needs review rather than a silent pass.

**C. Cadence expectations**

* Nifty 500 is reconstituted semi-annually (September and March cycles, effective end of each month) —
  refresh daily and hash-diff so these are picked up automatically.
* The CY2027 holiday list is expected on the same page in/around Dec 2026 – Jan 2027; that refresh is
  the single blocking dependency for scanning in Q4 FY2026-27 / Q1 FY2027-28.

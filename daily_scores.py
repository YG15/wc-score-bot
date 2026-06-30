#!/usr/bin/env python3
"""
Daily World Cup score tracker. Two outputs each morning (launchd, 8am):

  1. Telegram: today's games with their most-probable scoreline (with flags).
  2. Google Sheet: a full, always-current table of EVERY known WC game -
     predicted score, probability, and the actual result once the game finishes.

Standard library only (runs under /usr/bin/python3). No pip installs.

Prediction source, in priority order per game:
  - Polymarket's live exact-score market (de-vigged argmax) -> source "market"
  - a simple independent-Poisson model fitted to the de-vigged moneyline -> source "model"
    (used for games Polymarket hasn't opened an exact-score market for yet)
  Each live prediction is cached to ~/wc-scores/predictions.json, so once a game's
  markets resolve (prices go degenerate) the row keeps its LAST pre-game prediction.

Actual result: when a game's exact-score market resolves, the winning cell's price
goes to ~1.0; that cell's score is the actual result. A >3-goal result resolves the
"Any Other Score" cell, shown as ">3 (other)".

Testing:  python3 daily_scores.py --print [YYYY-MM-DD]
"""
import sys, os, json, math, time, datetime, urllib.request, urllib.parse, re, gzip, unicodedata

UA = {"User-Agent": "Mozilla/5.0", "Accept-Encoding": "gzip, deflate"}
TELEGRAM_FILE = os.path.expanduser("~/wc-scores/telegram.txt")           # line1 token, line2 chat id
SHEET_WEBHOOK_FILE = os.path.expanduser("~/wc-scores/sheet_webhook.txt")  # Apps Script /exec URL
CACHE_FILE = os.path.expanduser("~/wc-scores/predictions.json")           # last good prediction per game
SLUG_CACHE_FILE = os.path.expanduser("~/wc-scores/game_slugs.json")       # all known game slugs (survives pagination failures)
META_CACHE_FILE = os.path.expanduser("~/wc-scores/meta_cache.json")        # winner + top-scorer + goals (survives API failures)
WC_TAG_ID = 102232
END_DATE = "2026-07-20"      # day after the final; past this the job goes silent
KNOCKOUT_DATE = "2026-06-29"  # R32 starts here; 5 pts exact / 2 pts right direction
SHEET_TITLE = "World Cup 2026 - Most-Probable Scores"

TEAM_FLAGS = {
    "Mexico": "🇲🇽", "South Africa": "🇿🇦", "Korea Republic": "🇰🇷", "Czechia": "🇨🇿",
    "Canada": "🇨🇦", "Bosnia and Herzegovina": "🇧🇦", "United States": "🇺🇸", "Paraguay": "🇵🇾",
    "Qatar": "🇶🇦", "Switzerland": "🇨🇭", "Brazil": "🇧🇷", "Morocco": "🇲🇦", "Haiti": "🇭🇹",
    "Scotland": "🏴󠁧󠁢󠁳󠁣󠁴󠁿", "Australia": "🇦🇺", "Türkiye": "🇹🇷", "Germany": "🇩🇪", "Curaçao": "🇨🇼",
    "Netherlands": "🇳🇱", "Japan": "🇯🇵", "Côte d'Ivoire": "🇨🇮", "Ecuador": "🇪🇨", "Sweden": "🇸🇪",
    "Tunisia": "🇹🇳", "Spain": "🇪🇸", "Cabo Verde": "🇨🇻", "Belgium": "🇧🇪", "Egypt": "🇪🇬",
    "Saudi Arabia": "🇸🇦", "Uruguay": "🇺🇾", "IR Iran": "🇮🇷", "New Zealand": "🇳🇿", "France": "🇫🇷",
    "Senegal": "🇸🇳", "Iraq": "🇮🇶", "Norway": "🇳🇴", "Argentina": "🇦🇷", "Algeria": "🇩🇿",
    "Austria": "🇦🇹", "Jordan": "🇯🇴", "Portugal": "🇵🇹", "DR Congo": "🇨🇩", "England": "🏴󠁧󠁢󠁥󠁮󠁧󠁿",
    "Croatia": "🇭🇷", "Ghana": "🇬🇭", "Panama": "🇵🇦", "Uzbekistan": "🇺🇿", "Colombia": "🇨🇴",
}


def flag(team):
    return TEAM_FLAGS.get(team, "")


# flagcdn.com ISO codes (England/Scotland use GB subdivisions). Used for IMAGE() in Sheets,
# because Google Sheets does NOT render flag emoji - it needs an actual image.
FLAG_CODE = {
    "Mexico": "mx", "South Africa": "za", "Korea Republic": "kr", "Czechia": "cz",
    "Canada": "ca", "Bosnia and Herzegovina": "ba", "United States": "us", "Paraguay": "py",
    "Qatar": "qa", "Switzerland": "ch", "Brazil": "br", "Morocco": "ma", "Haiti": "ht",
    "Scotland": "gb-sct", "Australia": "au", "Türkiye": "tr", "Germany": "de", "Curaçao": "cw",
    "Netherlands": "nl", "Japan": "jp", "Côte d'Ivoire": "ci", "Ecuador": "ec", "Sweden": "se",
    "Tunisia": "tn", "Spain": "es", "Cabo Verde": "cv", "Belgium": "be", "Egypt": "eg",
    "Saudi Arabia": "sa", "Uruguay": "uy", "IR Iran": "ir", "New Zealand": "nz", "France": "fr",
    "Senegal": "sn", "Iraq": "iq", "Norway": "no", "Argentina": "ar", "Algeria": "dz",
    "Austria": "at", "Jordan": "jo", "Portugal": "pt", "DR Congo": "cd", "England": "gb-eng",
    "Croatia": "hr", "Ghana": "gh", "Panama": "pa", "Uzbekistan": "uz", "Colombia": "co",
}


def flag_formula(team):
    """A Google Sheets IMAGE() formula showing the team's flag, or '' if unknown."""
    code = FLAG_CODE.get(team)
    return '=IMAGE("https://flagcdn.com/w40/%s.png")' % code if code else ""


def labeled(team):
    f = flag(team)
    return (f + " " + team) if f else team


def get_json(url, retries=4, timeout=12, deadline=None):
    """GET + parse JSON, retrying transient network failures with backoff.

    Polymarket drops large responses intermittently (IncompleteRead). Retrying
    fixes it because a fresh connection usually succeeds. `deadline` caps the
    TOTAL seconds spent across all attempts - so when the API is genuinely down
    we give up fast and let the caller fall back to cache, instead of hanging
    for minutes. Requests gzip so large tag pages stay small."""
    last = None
    start = time.monotonic()
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            resp = urllib.request.urlopen(req, timeout=timeout)
            raw = resp.read()
            if resp.headers.get("Content-Encoding", "") == "gzip":
                raw = gzip.decompress(raw)
            return json.loads(raw)
        except Exception as ex:
            last = ex
            if deadline is not None and time.monotonic() - start >= deadline:
                break
            time.sleep(min(1.5 * (attempt + 1), 3))  # backoff, capped at 3s
    raise last


def teams(title):
    m = re.search(r"(.+?)\s+vs\.?\s+(.+)", title)
    return (m.group(1).strip(), m.group(2).strip()) if m else (title, "")


# FIFA slug code → full team name (for metadata reconstruction when API is unavailable)
_SLUG_CODE = {
    "alg": "Algeria", "arg": "Argentina", "aus": "Australia", "aut": "Austria",
    "bel": "Belgium", "bih": "Bosnia and Herzegovina", "bra": "Brazil", "can": "Canada",
    "cdr": "DR Congo", "che": "Switzerland", "civ": "Côte d'Ivoire", "col": "Colombia",
    "cvi": "Cabo Verde", "cze": "Czechia", "ecu": "Ecuador", "egy": "Egypt",
    "eng": "England", "esp": "Spain", "fra": "France", "ger": "Germany",
    "gha": "Ghana", "hai": "Haiti", "hrv": "Croatia", "irn": "IR Iran",
    "irq": "Iraq", "jor": "Jordan", "jpn": "Japan", "kor": "Curaçao",
    "kr": "Korea Republic", "ksa": "Saudi Arabia", "mar": "Morocco", "mex": "Mexico",
    "nld": "Netherlands", "nor": "Norway", "nzl": "New Zealand", "pan": "Panama",
    "par": "Paraguay", "prt": "Portugal", "qat": "Qatar", "rsa": "South Africa",
    "sco": "Scotland", "sen": "Senegal", "swe": "Sweden", "tun": "Tunisia",
    "tur": "Türkiye", "ury": "Uruguay", "usa": "United States", "uzb": "Uzbekistan",
}


def _slug_meta(slug):
    """Derive (home, away, date) from a slug. Returns (None, None, None) if unparseable."""
    m = re.match(r"fifwc-(\w+)-(\w+)-(\d{4}-\d{2}-\d{2})$", slug)
    if not m:
        return None, None, None
    home = _SLUG_CODE.get(m.group(1))
    away = _SLUG_CODE.get(m.group(2))
    date = m.group(3)
    return home, away, date


def yes_price(market):
    outs, pr = market.get("outcomes"), market.get("outcomePrices")
    if isinstance(outs, str):
        outs = json.loads(outs)
    if isinstance(pr, str):
        pr = json.loads(pr)
    if not pr:
        return None
    d = dict(zip(outs, [float(x) for x in pr]))
    return d.get("Yes", float(pr[0]))


# ---------- pure-python independent-Poisson fallback model ----------

def _pois(k, lam):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def _outcome_probs(lh, la, maxg=8):
    ph = [_pois(i, lh) for i in range(maxg + 1)]
    pa = [_pois(j, la) for j in range(maxg + 1)]
    sh, sa = sum(ph), sum(pa)
    ph = [x / sh for x in ph]
    pa = [x / sa for x in pa]
    home = draw = away = 0.0
    for i in range(maxg + 1):
        for j in range(maxg + 1):
            p = ph[i] * pa[j]
            if i > j:
                home += p
            elif i == j:
                draw += p
            else:
                away += p
    return home, draw, away


def _fit_lambdas(ml):
    tgt = (ml["home"], ml["draw"], ml["away"])

    def err(lh, la):
        h, d, a = _outcome_probs(lh, la)
        return (h - tgt[0]) ** 2 + (d - tgt[1]) ** 2 + (a - tgt[2]) ** 2

    best = None
    grid = [0.15 + 0.1 * k for k in range(35)]  # 0.15 .. 3.55
    for lh in grid:
        for la in grid:
            e = err(lh, la)
            if best is None or e < best[0]:
                best = (e, lh, la)
    _, lh0, la0 = best
    for lh in [lh0 + 0.02 * k for k in range(-4, 5)]:
        for la in [la0 + 0.02 * k for k in range(-4, 5)]:
            if lh <= 0 or la <= 0:
                continue
            e = err(lh, la)
            if e < best[0]:
                best = (e, lh, la)
    return best[1], best[2]


def _argmax_score(lh, la, maxg=8):
    ph = [_pois(i, lh) for i in range(maxg + 1)]
    pa = [_pois(j, la) for j in range(maxg + 1)]
    sh, sa = sum(ph), sum(pa)
    ph = [x / sh for x in ph]
    pa = [x / sa for x in pa]
    best = (0, 0, -1.0)
    for i in range(maxg + 1):
        for j in range(maxg + 1):
            p = ph[i] * pa[j]
            if p > best[2]:
                best = (i, j, p)
    return best[0], best[1], best[2]


def model_score(ml):
    return _argmax_score(*_fit_lambdas(ml))


def moneyline_from_event(event, home, away):
    wins, draw = {}, None
    for mk in event.get("markets", []):
        q = (mk.get("question") or "").lower()
        if "draw" in q or "tie" in q:
            draw = yes_price(mk)
        else:
            m = re.search(r"will\s+(.*?)\s+win", q)
            if m:
                wins[m.group(1).strip()] = yes_price(mk)
    if draw is None or len(wins) < 2 or None in wins.values():
        return None
    hl, al = home.lower(), away.lower()
    def match(n):
        for k in wins:
            if k in n or n in k:
                return k
        return None
    hk, ak = match(hl), match(al)
    ks = list(wins)
    if hk is None or ak is None or hk == ak:
        hk, ak = ks[0], ks[1]
    raw = {"home": wins[hk], "draw": draw, "away": wins[ak]}
    s = sum(raw.values())
    return {k: v / s for k, v in raw.items()}


# ---------- ESPN actual-score fallback ----------

# Team names that differ between our data (Polymarket) and ESPN
_ESPN_NAME = {
    "Korea Republic": "South Korea",
    "Côte d'Ivoire":  "Ivory Coast",
    "Cabo Verde":     "Cape Verde",
    "IR Iran":        "Iran",
    "DR Congo":       "Congo DR",
}

def espn_result(slug, home, away):
    """Return actual score 'H:A' from ESPN for a finished game, or None.
    Uses the date embedded in the slug (YYYY-MM-DD suffix) to avoid UTC off-by-one."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})$", slug)
    if not m:
        return None
    date_compact = m.group(1).replace("-", "")
    try:
        d = get_json("https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"
                     "?dates=" + date_compact)
    except Exception:
        return None

    def norm(n):
        return re.sub(r"[^a-z0-9]", "", n.lower())

    hn = norm(_ESPN_NAME.get(home, home))
    an = norm(_ESPN_NAME.get(away, away))

    for event in d.get("events", []):
        comps = event.get("competitions", [{}])[0]
        competitors = comps.get("competitors", [])
        ht = next((t for t in competitors if t.get("homeAway") == "home"), None)
        at = next((t for t in competitors if t.get("homeAway") == "away"), None)
        if not ht or not at:
            continue
        ehn = norm(ht.get("team", {}).get("displayName", ""))
        ean = norm(at.get("team", {}).get("displayName", ""))
        if ehn == hn and ean == an:
            # Only trust a FINAL score. A live/halftime game reports an in-progress
            # score (often 0:0) that would otherwise get cached and frozen forever.
            status = event.get("status", {}).get("type", {})
            if not status.get("completed") and status.get("state") != "post":
                return None
            hs, as_ = ht.get("score", ""), at.get("score", "")
            if hs != "" and as_ != "":
                return "%s:%s" % (hs, as_)
    return None


# ---------- Polymarket exact-score market ----------

def exact_score_event(slug):
    """Return (cells, any_other, closed) where cells = {(h,a): (yes, closed)},
    any_other = (yes, closed) or None, closed = whole-event closed flag. None if no event."""
    try:
        d = get_json("https://gamma-api.polymarket.com/events?" +
                     urllib.parse.urlencode({"slug": slug + "-exact-score"}),
                     retries=4, timeout=12)
    except Exception:
        return None
    if not d:
        return None
    ev = d[0]
    cells, any_other = {}, None
    for mk in ev.get("markets", []):
        title = (mk.get("groupItemTitle") or mk.get("question") or "").strip()
        y = yes_price(mk)
        c = bool(mk.get("closed"))
        if "any other" in title.lower():
            any_other = (y, c)
            continue
        m = re.search(r"(\d+)\s*-\s*(\d+)", title)
        if m:
            cells[(int(m.group(1)), int(m.group(2)))] = (y, c)
    return cells, any_other, bool(ev.get("closed"))


def analyze_game(event, now_iso, cached_actual=None, fetch_market=True):
    """Return a row dict for one game.

    cached_actual: if this game already resolved on a prior run, its final score
    is passed in here. We then skip ALL live network fetches (exact-score grid,
    ESPN, moneyline fit) - the result can't change, and gather() restores the
    stored model/market scores from cache. This keeps each run's live lookups
    limited to the handful of upcoming / just-finished games.

    fetch_market: when False (global time budget exhausted), skip the per-game
    Polymarket exact-score lookup - the only slow per-game network call. The
    MODEL score still computes from the moneyline already in `event` (no network),
    and gather() restores the last good market score from cache."""
    slug, title = event["slug"], event.get("title", "")
    home, away = teams(title)
    date = event.get("endDate", "")[:10]
    finished_by_clock = event.get("endDate", "") and event.get("endDate", "") < now_iso

    if cached_actual:
        return {"slug": slug, "date": date, "home": home, "away": away,
                "model_score": None, "model_prob": None,
                "market_score": None, "market_prob": None,
                "actual": cached_actual, "status": "Final", "xg": None}

    actual = xg = None
    model_score = model_prob = market_score = market_prob = None
    ml = moneyline_from_event(event, home, away)

    es = exact_score_event(slug) if fetch_market else None
    if es:
        cells, any_other, ev_closed = es
        # actual result, if resolved
        resolved = [(sc, y) for sc, (y, c) in cells.items() if y is not None and y >= 0.99]
        if resolved:
            (h, a), _ = max(resolved, key=lambda t: t[1])
            actual = "%d:%d" % (h, a)
        elif any_other and any_other[0] is not None and any_other[0] >= 0.99:
            actual = espn_result(slug, home, away) or ">3 (other)"
        # Live MARKET score - only if the book is real. Two guards:
        #  - junk: an untraded/early book quotes many cells at implausible mids (several near ~0.45);
        #    a real exact-score probability is never that high. 2+ cells over 0.30 => stale, skip.
        #  - consistency: the market-implied 1X2 must be within 0.20 of the (always-liquid) moneyline.
        if not ev_closed and not actual and cells:
            vals = [y for (y, _) in cells.values() if y is not None]
            grid = sum(vals)
            junk = sum(1 for y in vals if y > 0.30) >= 2
            if grid > 0 and ml is not None and not junk:
                ph = sum(y for (h, a), (y, _) in cells.items() if y and h > a) / grid
                pdr = sum(y for (h, a), (y, _) in cells.items() if y and h == a) / grid
                paw = sum(y for (h, a), (y, _) in cells.items() if y and h < a) / grid
                if max(abs(ph - ml["home"]), abs(pdr - ml["draw"]), abs(paw - ml["away"])) <= 0.20:
                    denom = grid + (any_other[0] if any_other and any_other[0] else 0)
                    (h, a), (y, _) = max(cells.items(), key=lambda kv: (kv[1][0] or 0))
                    market_score, market_prob = "%d:%d" % (h, a), 100.0 * y / denom

    # ESPN fallback: fetch actual score for any finished game where Polymarket didn't resolve
    if actual is None and finished_by_clock:
        actual = espn_result(slug, home, away)

    # MODEL score + expected goals from the moneyline Poisson fit (computed for every game).
    # Skip once resolved - prices are degenerate then; the cache keeps the last good values.
    if actual is None and ml:
        lh, la = _fit_lambdas(ml)
        xg = [round(lh, 2), round(la, 2)]
        h, a, p = _argmax_score(lh, la)
        model_score, model_prob = "%d:%d" % (h, a), 100.0 * p

    status = "Final" if actual else ("Awaiting result" if finished_by_clock else "Scheduled")
    return {"slug": slug, "date": date, "home": home, "away": away,
            "model_score": model_score, "model_prob": model_prob,
            "market_score": market_score, "market_prob": market_prob,
            "actual": actual, "status": status, "xg": xg}


# ---------- cache (keeps last pre-game prediction) ----------

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            return json.load(open(CACHE_FILE))
        except Exception:
            return {}
    return {}


def save_cache(cache):
    try:
        json.dump(cache, open(CACHE_FILE, "w"), ensure_ascii=False, indent=0)
    except Exception as ex:
        sys.stderr.write("cache save failed: %s\n" % ex)


# ---------- build outputs ----------

def _load_slug_cache():
    try:
        return set(json.load(open(SLUG_CACHE_FILE)))
    except Exception:
        return set()


def _save_slug_cache(slugs):
    try:
        json.dump(sorted(slugs), open(SLUG_CACHE_FILE, "w"), ensure_ascii=False)
    except Exception as ex:
        sys.stderr.write("slug cache save failed: %s\n" % ex)


def fetch_wc_events():
    """All events under the World Cup tag, in two passes:

    Pass 1 - pagination: tag_id scan with limit=50 (gzip keeps each page ~200 KB;
    100 caused IncompleteRead). Skips failed pages rather than aborting so
    high-offset knockout events are not lost. Saves every newly found game slug
    to game_slugs.json.

    Pass 2 - slug cache: for any slug in game_slugs.json NOT already returned by
    pass 1, fetch the event directly by slug (always small, always reliable). This
    ensures that once a knockout game is discovered it stays in the output even on
    days when the paginator cannot reach it."""
    seen = set()
    events = []
    t0 = time.monotonic()

    # --- pass 1: paginate (max 30 seconds) ---
    offset, consecutive_failures, total_failures = 0, 0, 0
    while time.monotonic() - t0 < 30:
        try:
            page = get_json("https://gamma-api.polymarket.com/events?" + urllib.parse.urlencode(
                {"tag_id": WC_TAG_ID, "limit": 50, "offset": offset}), retries=2, timeout=15)
            if not page:
                break
            for e in page:
                slug = e.get("slug", "")
                if slug not in seen:
                    seen.add(slug)
                    events.append(e)
            consecutive_failures = 0
            if len(page) < 50:
                break
        except Exception:
            consecutive_failures += 1
            total_failures += 1
            if consecutive_failures >= 5 or total_failures >= 15:
                break
        offset += 50
        if offset > 5000:
            break

    # save any newly found game slugs
    game_slugs = _load_slug_cache()
    newly_found = {e.get("slug", "") for e in events
                   if re.match(r"fifwc-.+-2026-\d{2}-\d{2}$", e.get("slug", ""))}
    if newly_found - game_slugs:
        game_slugs |= newly_found
        _save_slug_cache(game_slugs)

    # --- pass 2: fill in slugs the paginator missed (max 45 seconds) ---
    # Only recent + future games; old resolved ones are already in predictions cache.
    cutoff = (datetime.datetime.now(datetime.timezone.utc) -
              datetime.timedelta(days=5)).strftime("%Y-%m-%d")
    # sort by date descending: most recent / upcoming games fetched first within time cap
    missed = sorted(
        (s for s in game_slugs - seen if s[-10:] >= cutoff),
        key=lambda s: s[-10:], reverse=True
    )
    for slug in missed:
        if time.monotonic() - t0 > 55:
            break  # hard cap: leave time for analyze_game calls
        try:
            ev = get_json("https://gamma-api.polymarket.com/events?" +
                          urllib.parse.urlencode({"slug": slug}), retries=3, timeout=15)
            if ev:
                events.append(ev[0])
                seen.add(slug)
        except Exception:
            pass  # best-effort; retry on the next run

    return events


def gather(now_iso, cache):
    events = fetch_wc_events()
    # Strict slug match: fifwc-{team}-{team}-2026-MM-DD — excludes halftime/exact-score sub-markets
    games = [e for e in events if re.match(r"fifwc-.+-2026-\d{2}-\d{2}$", e.get("slug", ""))]
    games.sort(key=lambda e: e.get("endDate", ""))

    seen_slugs = set()
    rows = []
    # Global budget for per-game live market fetches. Once exhausted we keep going
    # with model scores (no network) + cached market scores, so a flaky API can
    # never make a run drag on for minutes. Plenty when the API is healthy.
    market_t0 = time.monotonic()
    MARKET_BUDGET_S = 75
    for e in games:
        cached_actual = cache.get(e.get("slug", ""), {}).get("actual")
        within_budget = time.monotonic() - market_t0 < MARKET_BUDGET_S
        r = analyze_game(e, now_iso, cached_actual=cached_actual,
                         fetch_market=within_budget)
        seen_slugs.add(r["slug"])
        # save game metadata so we can reconstruct rows if API is unavailable in a future run
        cache.setdefault(r["slug"], {}).update({
            "home": r["home"], "away": r["away"],
            "date": r["date"], "end_date": e.get("endDate", ""),
        })
        # update / fall back to cache for the prediction
        live = r["model_score"] is not None or r["market_score"] is not None
        if live:
            entry = cache[r["slug"]]
            if r["model_score"] is not None:
                entry["model_score"] = r["model_score"]
                entry["model_prob"] = round(r["model_prob"], 1) if r["model_prob"] is not None else None
            if r["market_score"] is not None:
                entry["market_score"] = r["market_score"]
                entry["market_prob"] = round(r["market_prob"], 1) if r["market_prob"] is not None else None
            if r["xg"] is not None:
                entry["xg"] = r["xg"]
            # restore cached market score if this run's exact-score lookup failed
            if r["market_score"] is None and entry.get("market_score"):
                r["market_score"] = entry["market_score"]
                r["market_prob"] = entry.get("market_prob")
        elif r["slug"] in cache:
            c = cache[r["slug"]]
            r["model_score"], r["model_prob"] = c.get("model_score"), c.get("model_prob")
            r["market_score"], r["market_prob"] = c.get("market_score"), c.get("market_prob")
            if r["xg"] is None:
                r["xg"] = c.get("xg")
        # persist actual score once resolved; restore from cache if API failed this run
        if r["actual"]:
            cache[r["slug"]]["actual"] = r["actual"]
        elif cache[r["slug"]].get("actual"):
            r["actual"] = cache[r["slug"]]["actual"]
            r["status"] = "Final"
        # carry the full kickoff timestamp so same-day games sort by playtime
        r["end_date"] = cache[r["slug"]].get("end_date", "") or e.get("endDate", "")
        rows.append(r)

    # Reconstruct rows for games the API didn't return (pagination gaps, rate limiting).
    # Combines cached metadata/predictions with slug-derived metadata as fallback.
    slug_cache = _load_slug_cache()
    all_known = {s for s in (set(cache) | slug_cache)
                 if re.match(r"fifwc-.+-2026-\d{2}-\d{2}$", s)}
    for slug in sorted(all_known):
        if slug in seen_slugs:
            continue
        c = cache.get(slug, {})
        home = c.get("home") or _slug_meta(slug)[0]
        away = c.get("away") or _slug_meta(slug)[1]
        date = c.get("date") or _slug_meta(slug)[2]
        if not home or not away or not date:
            continue
        end_date = c.get("end_date", "")
        # estimate end_date as game-date + 1 day if not cached
        if not end_date:
            try:
                gd = datetime.datetime.strptime(date, "%Y-%m-%d")
                end_date = (gd + datetime.timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
            except Exception:
                end_date = date + "T23:59:59Z"
        finished_by_clock = end_date < now_iso
        actual = c.get("actual")
        if actual is None and finished_by_clock:
            actual = espn_result(slug, home, away)
            if actual:
                c["actual"] = actual
                cache[slug] = c
        status = "Final" if actual else ("Awaiting result" if finished_by_clock else "Scheduled")
        rows.append({
            "slug": slug, "date": date, "home": home, "away": away,
            "model_score": c.get("model_score"), "model_prob": c.get("model_prob"),
            "market_score": c.get("market_score"), "market_prob": c.get("market_prob"),
            "actual": actual, "status": status, "xg": c.get("xg"),
            "end_date": end_date,
        })

    # Order by match date, then kickoff time within the day, then slug as a tiebreak.
    rows.sort(key=lambda r: (r["date"], r.get("end_date") or "", r["slug"]))
    return rows


def prediction_points(actual, predicted, is_knockout=False):
    """Group stage: 3 pts exact, 1 pt right direction, 0 wrong.
    Knockout (R32+): 5 pts exact, 2 pts right direction, 0 wrong."""
    if not actual or not predicted:
        return None
    am = re.match(r"(\d+):(\d+)", actual)
    pm = re.match(r"(\d+):(\d+)", predicted)
    if not am or not pm:
        return None
    ah, aa = int(am.group(1)), int(am.group(2))
    ph, pa = int(pm.group(1)), int(pm.group(2))
    if ah == ph and aa == pa:
        return 5 if is_knockout else 3
    def direction(h, a):
        return "H" if h > a else ("A" if a > h else "D")
    return (2 if is_knockout else 1) if direction(ah, aa) == direction(ph, pa) else 0


def sheet_values(rows, today):
    title = "%s   (updated %s; source: Polymarket)" % (SHEET_TITLE, today)
    header = ["Match Date", "", "Home", "", "Away",
              "Model Score", "Model %", "Market Score", "Market %", "xG H:A",
              "Actual Score", "Status", "Updated", "Points"]
    width = len(header)
    out = [[title] + [""] * (width - 1), header]
    total_pts = max_pts = 0
    for r in rows:
        mdl_p = ("%.0f%%" % r["model_prob"]) if r["model_prob"] is not None else ""
        mkt_p = ("%.0f%%" % r["market_prob"]) if r["market_prob"] is not None else ""
        xg = ("%.2f:%.2f" % (r["xg"][0], r["xg"][1])) if r["xg"] else ""
        best_pred = r["market_score"] or r["model_score"]
        is_ko = r["date"] >= KNOCKOUT_DATE
        pts = prediction_points(r["actual"], best_pred, is_knockout=is_ko)
        if pts is not None:
            total_pts += pts
            max_pts += (5 if is_ko else 3)
        out.append([r["date"], flag_formula(r["home"]), r["home"],
                    flag_formula(r["away"]), r["away"],
                    r["model_score"] or "", mdl_p,
                    r["market_score"] or "", mkt_p, xg,
                    r["actual"] or "", r["status"], today,
                    pts if pts is not None else ""])
    # totals row
    out.append([""] * (width - 3) + ["TOTAL", "",
                "%d / %d pts" % (total_pts, max_pts)])
    return out


def fetch_outcome_market(slug, top=16):
    """Return [(name, yes_price), ...] sorted desc for a multi-outcome Polymarket event
    (e.g. world-cup-winner, world-cup-golden-boot-winner). Raw prices, as the site shows them."""
    try:
        # winner / golden-boot are large single-event responses that Polymarket
        # drops intermittently (IncompleteRead). Failed attempts return fast, so
        # we keep retrying for up to 30s (empirically succeeds within ~5-7 tries);
        # if the API is truly down we give up at the deadline and fall back to cache.
        ev = get_json("https://gamma-api.polymarket.com/events?" +
                      urllib.parse.urlencode({"slug": slug}),
                      retries=20, timeout=8, deadline=25)
    except Exception:
        return []
    if not ev:
        return []
    rows = []
    for m in ev[0].get("markets", []):
        name = (m.get("groupItemTitle") or m.get("question") or "").strip()
        y = yes_price(m)
        if name and y is not None:
            rows.append((name, y))
    rows.sort(key=lambda x: -x[1])
    return rows[:top]


def fetch_goal_leaders():
    """Return {player_name: goals} from ESPN's tournament statistics endpoint."""
    try:
        d = get_json("https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/statistics?season=2026")
        goals = {}
        for stat in d.get("stats", []):
            if stat.get("name") == "goalsLeaders":
                for entry in stat.get("leaders", []):
                    name = entry.get("athlete", {}).get("displayName", "")
                    val = entry.get("value")
                    if name and val is not None:
                        goals[name] = int(val)
                break
        return goals
    except Exception:
        return {}


def _ascii_name(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s.lower())
                   if ord(c) < 128 and (c.isalpha() or c == " ")).strip()


def _match_name(player, goal_map):
    """Look up player in goal_map; try exact then accent-stripped fallback."""
    if player in goal_map:
        return goal_map[player]
    norm = _ascii_name(player)
    for k, v in goal_map.items():
        if _ascii_name(k) == norm:
            return v
    return None


def _load_meta_cache():
    try:
        return json.load(open(META_CACHE_FILE))
    except Exception:
        return {}


def _save_meta_cache(mc):
    try:
        json.dump(mc, open(META_CACHE_FILE, "w"), ensure_ascii=False, indent=0)
    except Exception as ex:
        sys.stderr.write("meta cache save failed: %s\n" % ex)


def meta_tab_values(today):
    """Second tab: World Cup winner + Golden Boot top scorer (Polymarket implied % + current goals)."""
    mc = _load_meta_cache()

    winner = fetch_outcome_market("world-cup-winner")
    scorer = fetch_outcome_market("world-cup-golden-boot-winner")
    goal_map = fetch_goal_leaders()

    # persist fresh data; fall back to cache when API fails
    if winner:
        mc["winner"] = winner
    else:
        winner = [(n, p) for n, p in mc.get("winner", [])]
    if scorer:
        mc["scorer"] = scorer
    else:
        scorer = [(n, p) for n, p in mc.get("scorer", [])]
    if goal_map:
        mc["goal_map"] = goal_map
    else:
        goal_map = mc.get("goal_map", {})
    _save_meta_cache(mc)

    out = [["World Cup 2026 - Winner & Top Scorer (updated %s; Polymarket implied %%)" % today, "", "", "", ""]]
    out.append(["Winner (World Cup champion)", "", "", "", ""])
    out.append(["Rank", "", "Team", "Implied %", ""])
    for i, (team, p) in enumerate(winner, 1):
        out.append([i, flag_formula(team), team, "%.1f%%" % (p * 100), ""])
    out.append(["", "", "", "", ""])
    out.append(["Top Scorer (Golden Boot)", "", "", "", ""])
    out.append(["Rank", "", "Player", "Goals", "Implied %"])
    for i, (player, p) in enumerate(scorer, 1):
        goals = _match_name(player, goal_map)
        out.append([i, "", player, goals if goals is not None else "", "%.1f%%" % (p * 100)])
    return out


def telegram_message(rows, today):
    tomorrow = (datetime.datetime.strptime(today, "%Y-%m-%d") + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    day = [r for r in rows if r["date"] in (today, tomorrow)]
    if not day:
        return "No World Cup games in the next 24 hours."
    lines = ["*World Cup - next 24h (%s / %s)*" % (today, tomorrow),
             "_model = Poisson on moneyline; market = Polymarket exact-score_"]
    for r in day:
        parts = []
        if r["model_score"]:
            parts.append("model %s" % r["model_score"].replace(":", "-"))
        if r["market_score"]:
            parts.append("market %s" % r["market_score"].replace(":", "-"))
        core = " | ".join(parts) if parts else "not priced yet"
        if r["actual"]:
            core += "  ->  *actual %s*" % r["actual"]
        lines.append("- [%s] %s vs %s:  %s" % (r["date"], labeled(r["home"]), labeled(r["away"]), core))
    return "\n".join(lines)


# ---------- delivery ----------

def post_to_telegram(text):
    if not os.path.exists(TELEGRAM_FILE):
        sys.stderr.write("No Telegram file; skipping Telegram.\n")
        return
    parts = [ln.strip() for ln in open(TELEGRAM_FILE).read().splitlines() if ln.strip()]
    if len(parts) < 2:
        sys.stderr.write("Telegram file needs token (line1) + chat id (line2).\n")
        return
    token, chat_id = parts[0], parts[1]
    body = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}).encode("utf-8")
    req = urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % token,
                                 data=body, headers={"Content-Type": "application/json"})
    sys.stderr.write("Telegram responded: %s\n" % urllib.request.urlopen(req, timeout=30).status)


def post_to_sheet(sheets, title):
    """sheets = [{"name": tab_name, "values": [[...]], "frozen": n}, ...]"""
    if not os.path.exists(SHEET_WEBHOOK_FILE):
        sys.stderr.write("No Sheet webhook file; skipping Sheet.\n")
        return
    url = open(SHEET_WEBHOOK_FILE).read().strip()
    body = json.dumps({"title": title, "sheets": sheets}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=60)
    sys.stderr.write("Sheet responded: %s  body: %s\n" % (resp.status, resp.read(300).decode("utf-8", "replace")))


def main():
    args = [a for a in sys.argv[1:]]
    do_print = "--print" in args
    args = [a for a in args if a != "--print"]
    today = args[0] if args else datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if not do_print and today >= END_DATE:
        sys.stderr.write("Past END_DATE (%s); nothing to do.\n" % END_DATE)
        return

    cache = load_cache()
    rows = gather(now_iso, cache)
    save_cache(cache)

    title = "%s   (updated %s)" % (SHEET_TITLE, today)
    sheets = [
        {"name": "Scores", "values": sheet_values(rows, today), "frozen": 2},
        {"name": "Winner & Top Scorer", "values": meta_tab_values(today), "frozen": 1},
    ]
    msg = telegram_message(rows, today)

    if do_print:
        print(msg)
        for s in sheets:
            print("\n[tab '%s': %d rows]" % (s["name"], len(s["values"])))
            for v in s["values"][:5]:
                print(v)
            print("...")
    else:
        post_to_telegram(msg)
        post_to_sheet(sheets, title)


if __name__ == "__main__":
    main()

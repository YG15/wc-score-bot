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
import sys, os, json, math, datetime, urllib.request, urllib.parse, re

UA = {"User-Agent": "Mozilla/5.0"}
TELEGRAM_FILE = os.path.expanduser("~/wc-scores/telegram.txt")           # line1 token, line2 chat id
SHEET_WEBHOOK_FILE = os.path.expanduser("~/wc-scores/sheet_webhook.txt")  # Apps Script /exec URL
CACHE_FILE = os.path.expanduser("~/wc-scores/predictions.json")           # last good prediction per game
WC_TAG_ID = 102232
END_DATE = "2026-07-20"  # day after the final; past this the job goes silent
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


def get_json(url):
    req = urllib.request.Request(url, headers=UA)
    return json.load(urllib.request.urlopen(req, timeout=30))


def teams(title):
    m = re.search(r"(.+?)\s+vs\.?\s+(.+)", title)
    return (m.group(1).strip(), m.group(2).strip()) if m else (title, "")


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


# ---------- Polymarket exact-score market ----------

def exact_score_event(slug):
    """Return (cells, any_other, closed) where cells = {(h,a): (yes, closed)},
    any_other = (yes, closed) or None, closed = whole-event closed flag. None if no event."""
    try:
        d = get_json("https://gamma-api.polymarket.com/events?" +
                     urllib.parse.urlencode({"slug": slug + "-exact-score"}))
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


def analyze_game(event, now_iso):
    """Return a row dict for one game."""
    slug, title = event["slug"], event.get("title", "")
    home, away = teams(title)
    date = event.get("endDate", "")[:10]
    finished_by_clock = event.get("endDate", "") and event.get("endDate", "") < now_iso

    actual = xg = None
    model_score = model_prob = market_score = market_prob = None
    ml = moneyline_from_event(event, home, away)

    es = exact_score_event(slug)
    if es:
        cells, any_other, ev_closed = es
        # actual result, if resolved
        resolved = [(sc, y) for sc, (y, c) in cells.items() if y is not None and y >= 0.99]
        if resolved:
            (h, a), _ = max(resolved, key=lambda t: t[1])
            actual = "%d:%d" % (h, a)
        elif any_other and any_other[0] is not None and any_other[0] >= 0.99:
            actual = ">3 (other)"
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

def gather(now_iso, cache):
    events = get_json("https://gamma-api.polymarket.com/events?" + urllib.parse.urlencode(
        {"tag_id": WC_TAG_ID, "limit": 500, "active": "true", "closed": "false"}))
    games = [e for e in events if e.get("slug", "").startswith("fifwc-")]
    games.sort(key=lambda e: e.get("endDate", ""))

    rows = []
    for e in games:
        r = analyze_game(e, now_iso)
        # update / fall back to cache for the prediction
        live = r["model_score"] is not None or r["market_score"] is not None
        if live:
            cache[r["slug"]] = {
                "model_score": r["model_score"],
                "model_prob": round(r["model_prob"], 1) if r["model_prob"] is not None else None,
                "market_score": r["market_score"],
                "market_prob": round(r["market_prob"], 1) if r["market_prob"] is not None else None,
                "xg": r["xg"]}
        elif r["slug"] in cache:
            c = cache[r["slug"]]
            r["model_score"], r["model_prob"] = c.get("model_score"), c.get("model_prob")
            r["market_score"], r["market_prob"] = c.get("market_score"), c.get("market_prob")
            if r["xg"] is None:
                r["xg"] = c.get("xg")
        rows.append(r)
    return rows


def sheet_values(rows, today):
    title = "%s   (updated %s; source: Polymarket)" % (SHEET_TITLE, today)
    header = ["Match Date", "", "Home", "", "Away",
              "Model Score", "Model %", "Market Score", "Market %", "xG H:A",
              "Actual Score", "Status", "Updated"]
    width = len(header)
    out = [[title] + [""] * (width - 1), header]
    for r in rows:
        mdl_p = ("%.0f%%" % r["model_prob"]) if r["model_prob"] is not None else ""
        mkt_p = ("%.0f%%" % r["market_prob"]) if r["market_prob"] is not None else ""
        xg = ("%.2f:%.2f" % (r["xg"][0], r["xg"][1])) if r["xg"] else ""
        out.append([r["date"], flag_formula(r["home"]), r["home"],
                    flag_formula(r["away"]), r["away"],
                    r["model_score"] or "", mdl_p,
                    r["market_score"] or "", mkt_p, xg,
                    r["actual"] or "", r["status"], today])
    return out


def fetch_outcome_market(slug, top=16):
    """Return [(name, yes_price), ...] sorted desc for a multi-outcome Polymarket event
    (e.g. world-cup-winner, world-cup-golden-boot-winner). Raw prices, as the site shows them."""
    try:
        ev = get_json("https://gamma-api.polymarket.com/events?" +
                      urllib.parse.urlencode({"slug": slug}))
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


def meta_tab_values(today):
    """Second tab: World Cup winner + Golden Boot top scorer (Polymarket implied %)."""
    winner = fetch_outcome_market("world-cup-winner")
    scorer = fetch_outcome_market("world-cup-golden-boot-winner")
    out = [["World Cup 2026 - Winner & Top Scorer (updated %s; Polymarket implied %%)" % today, "", "", ""]]
    out.append(["Winner (World Cup champion)", "", "", ""])
    out.append(["Rank", "", "Team", "Implied %"])
    for i, (team, p) in enumerate(winner, 1):
        out.append([i, flag_formula(team), team, "%.1f%%" % (p * 100)])
    out.append(["", "", "", ""])
    out.append(["Top Scorer (Golden Boot)", "", "", ""])
    out.append(["Rank", "", "Player", "Implied %"])
    for i, (player, p) in enumerate(scorer, 1):
        out.append([i, "", player, "%.1f%%" % (p * 100)])
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

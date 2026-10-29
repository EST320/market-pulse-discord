"""Buy-the-dip study for one stock: every large single-day drop since the IPO,
what the stock did 1, 20, 60 and 250 trading days later and how much further
it fell first, split into drops on an earnings release and the rest, and set
against what buying on an ordinary day would have returned.

Usage:
    python -m market_pulse.dip GOOGL [threshold]

Posted as the reply to the /dip slash command when DISCORD_APPLICATION_ID and
INTERACTION_TOKEN are set (SYMBOL and THRESHOLD then come from the
environment). Set DRY_RUN=true to render without posting.
"""

import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import matplotlib.patches as mpatches
import requests

from market_pulse import nasdaq
from market_pulse.discord import edit_interaction_response
from market_pulse.theme import (
    AMBER, GREEN, MUTED, RED, RULE, TEXT, Fonts, box, canvas, label, save, text_width,
)

APPLICATION_ID_ENV = "DISCORD_APPLICATION_ID"
INTERACTION_TOKEN_ENV = "INTERACTION_TOKEN"
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() in ("1", "true", "yes")

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
# Start of the history request: 1900, so that listings older than 1970 are complete.
HISTORY_START = -2208988800
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
CALENDAR_WORKERS = 6
# Nasdaq's historical earnings calendar starts in January 2008. Earlier drops
# cannot be classified, and an empty calendar there must not read as "no
# earnings that day".
CALENDAR_START = date(2008, 1, 2)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
OUTPUT_FILE = Path("dip.png")

SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,9}$")
HORIZONS = (1, 20, 60, 250)
# Window for the further fall after a drop: the lowest close in the next 60 sessions.
LOW_WINDOW = 60
# Drops this close to an earlier one share most of their aftermath.
CLUSTER_DAYS = 5
DEFAULT_THRESHOLD = 8.0
# Without an explicit threshold the study stays at 8% even for volatile
# stocks: raising it would leave only the largest crashes, which for older
# listings all date from the dot-com era. The tables then show the most
# recent events. Only a steady stock with almost no 8% days gets a lower one.
LOWER_THRESHOLDS = (7, 6, 5)
MIN_EVENTS = 4
MAX_ROWS_PER_PANEL = 10

# Tickers that changed over the years, so that old earnings dates still match.
SYMBOL_ALIASES = {
    "GOOGL": {"GOOG"},
    "GOOG": {"GOOGL"},
    "META": {"FB"},
}


class StudyError(Exception):
    """A problem worth telling the user in plain words, such as an unknown ticker."""


# ============================================================
# Data
# ============================================================

def normalise_symbol(raw):
    """Share classes are written BRK.B or BRK/B elsewhere and BRK-B on Yahoo."""
    symbol = str(raw or "").strip().upper().replace("/", "-").replace(".", "-")
    if not SYMBOL_PATTERN.match(symbol):
        raise StudyError(f"“{raw}” 不是有效的股票代码。")
    return symbol


def fetch_history(symbol):
    """Daily adjusted closes since the first trading day. Returns (dates, closes, meta)."""
    params = {"period1": HISTORY_START, "period2": int(time.time()) + 86400, "interval": "1d", "events": "div,splits"}
    response = requests.get(CHART_URL.format(symbol=symbol), params=params, headers=HEADERS, timeout=30)
    if response.status_code == 404:
        raise StudyError(f"找不到股票代码 {symbol}。")
    response.raise_for_status()

    result = ((response.json().get("chart") or {}).get("result") or [None])[0]
    if not result or not result.get("timestamp"):
        raise StudyError(f"找不到 {symbol} 的历史价格。")

    adjusted = ((result.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose") or []
    dates, closes = [], []
    for ts, close in zip(result["timestamp"], adjusted):
        if isinstance(close, (int, float)) and close > 0:
            # Daily bars are stamped at the US open, so the UTC date is the trading date.
            # (Computed from the epoch: fromtimestamp rejects pre-1970 times on Windows.)
            dates.append((EPOCH + timedelta(seconds=ts)).date())
            closes.append(float(close))
    if len(closes) < 2:
        raise StudyError(f"{symbol} 的历史数据太少。")
    return dates, closes, result.get("meta") or {}


def daily_changes(closes):
    """Percent change of each day against the day before; the first day has none."""
    return [None] + [(closes[i] / closes[i - 1] - 1) * 100 for i in range(1, len(closes))]


def find_drops(dates, closes, threshold):
    """Every day that fell by threshold percent or more, with its forward returns."""
    changes = daily_changes(closes)
    events = []
    for i, change in enumerate(changes):
        # The tolerance keeps a drop of exactly the threshold from slipping
        # through on floating-point rounding.
        if change is None or change > -threshold + 1e-9:
            continue
        forward = {
            h: (closes[i + h] / closes[i] - 1) * 100 if i + h < len(closes) else None
            for h in HORIZONS
        }
        events.append({"index": i, "date": dates[i], "change": change, "forward": forward,
                       "low": further_low(closes, i)})
    return events


def further_low(closes, i):
    """How far below the close of day i the stock fell over the next LOW_WINDOW sessions.

    Zero if it never closed lower; None if the window is not complete yet.
    """
    if i + LOW_WINDOW >= len(closes):
        return None
    lowest = min(closes[i + 1:i + LOW_WINDOW + 1])
    return min(0.0, (lowest / closes[i] - 1) * 100)


def baseline(closes):
    """The same figures for buying at the close of every day, not just after a drop."""
    def returns(h):
        return [(closes[i + h] / closes[i] - 1) * 100 for i in range(len(closes) - h)]

    r60, r250 = returns(60), returns(250)
    lows = [further_low(closes, i) for i in range(len(closes) - LOW_WINDOW)]
    mean = lambda values: sum(values) / len(values) if values else None  # noqa: E731
    return {
        "win_rate": sum(1 for r in r60 if r > 0) / len(r60) * 100 if r60 else None,
        "average_60": mean(r60),
        "average_250": mean(r250),
        "average_low": mean(lows),
    }


def clustered(events):
    """Events that came within CLUSTER_DAYS sessions of the one before."""
    return [b for a, b in zip(events, events[1:]) if b["index"] - a["index"] <= CLUSTER_DAYS]


def choose_threshold(dates, closes, requested=None):
    """The requested threshold; otherwise 8%, lowered only if that finds almost nothing."""
    if requested is not None:
        return float(requested), find_drops(dates, closes, float(requested))

    threshold, events = DEFAULT_THRESHOLD, find_drops(dates, closes, DEFAULT_THRESHOLD)
    for lower in LOWER_THRESHOLDS:
        if len(events) >= MIN_EVENTS:
            break
        threshold, events = float(lower), find_drops(dates, closes, lower)
    return threshold, events


def fetch_calendars(days):
    """{day: companies reporting that day, or None if the calendar could not be read}."""
    def fetch(day):
        try:
            return day, nasdaq.fetch_earnings(day)
        except Exception as exc:
            print(f"Earnings calendar unavailable for {day}: {exc!r}")
            return day, None

    with ThreadPoolExecutor(max_workers=CALENDAR_WORKERS) as pool:
        return dict(pool.map(fetch, sorted(set(days))))


def reported_on(day, symbols, calendars):
    """Session of an earnings release by one of the symbols on that day: 'bmo', 'amc', '' or None."""
    rows = calendars.get(day)
    if rows is None:
        raise LookupError(day)
    for row in rows:
        if row["symbol"] in symbols:
            return row["hour"]
    return None


def classify(events, dates, symbol):
    """Mark each event as on an earnings release or not.

    A drop on day D is earnings-related when the company reported before the
    open on D, or after the close on the trading day before. A release with
    no stated time counts on either day. Drops before CALENDAR_START get
    None (unknown). Returns False if the calendar could not be read, leaving
    the events unclassified.
    """
    symbols = {symbol, symbol.replace("-", "/"), symbol.replace("-", ".")} | SYMBOL_ALIASES.get(symbol, set())
    covered = [e for e in events if dates[e["index"] - 1] >= CALENDAR_START]
    calendars = fetch_calendars(d for e in covered for d in (dates[e["index"]], dates[e["index"] - 1]))
    try:
        for event in events:
            i = event["index"]
            if dates[i - 1] < CALENDAR_START:
                event["earnings"] = None
                continue
            same_day = reported_on(dates[i], symbols, calendars)
            day_before = reported_on(dates[i - 1], symbols, calendars)
            event["earnings"] = same_day in ("bmo", "") or day_before in ("amc", "")
    except LookupError:
        for event in events:
            event.pop("earnings", None)
        return False
    return True


def summarise(events, dates, closes):
    """Headline numbers, the ordinary-day baseline and the notes under the tables."""
    finished = [e for e in events if e["forward"][60] is not None]
    winners = [e for e in finished if e["forward"][60] > 0]
    year = [e["forward"][250] for e in events if e["forward"][250] is not None]
    lows = [e["low"] for e in events if e["low"] is not None]
    summary = {
        "count": len(events),
        "finished": len(finished),
        "winners": len(winners),
        "win_rate": len(winners) / len(finished) * 100 if finished else None,
        "average_60": sum(e["forward"][60] for e in finished) / len(finished) if finished else None,
        "average_250": sum(year) / len(year) if year else None,
        "average_low": sum(lows) / len(lows) if lows else None,
        "losers": [e for e in finished if e["forward"][60] <= 0],
        "baseline": baseline(closes),
        "clustered": clustered(events),
    }

    # Rank of the latest event among every daily move since the IPO.
    latest = events[-1] if events else None
    summary["latest"] = latest
    if latest and len(closes) - 1 - latest["index"] <= 5:
        changes = sorted(c for c in daily_changes(closes) if c is not None)
        summary["latest_rank"] = changes.index(latest["change"]) + 1
    else:
        summary["latest_rank"] = None
    summary["worst"] = min(events, key=lambda e: e["change"]) if events else None
    return summary


# ============================================================
# Chart
# ============================================================
# Portrait card: the two tables are stacked at full width, so seven columns
# stay large and readable when Discord scales the image down.

CARD_WIDTH = 14.0
MARGIN = 0.48
STAT_TOP, STAT_HEIGHT = 2.05, 1.30
PANEL_GAP = 0.26
ROW_HEIGHT = 0.43
PANEL_HEAD = 1.02
FOOTER_HEIGHT = 0.95
# Column right edges, in inches from the panel's inner left edge; the date is left-aligned.
COLUMNS = [("DROP", 3.85), ("+1D", 5.35), ("+20D", 6.95), ("+60D", 8.55), ("+1Y", 10.15), ("LOW WITHIN 60D", 12.05)]


def signed(value, digits=1):
    return "—" if value is None else f"{value:+.{digits}f}%"


def value_color(value):
    if value is None:
        return MUTED
    return GREEN if value > 0 else RED


def plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else ("S" if word.isupper() else "s"))


def ordinal(n):
    return {1: "biggest", 2: "2nd biggest", 3: "3rd biggest"}.get(n, f"{n}th biggest")


def draw_stat(ax, font, x, width, value, caption, normal):
    box(ax, x, STAT_TOP, width, STAT_HEIGHT)
    label(ax, x + 0.22, STAT_TOP + 0.38, value, font(23, bold=True), AMBER)
    label(ax, x + 0.22, STAT_TOP + 0.78, caption, font(10.5), TEXT, max_width=width - 0.36)
    if normal:
        label(ax, x + 0.22, STAT_TOP + 1.06, normal, font(10), MUTED, max_width=width - 0.36)


def panel_rows(events):
    hidden = 1 if len(events) > MAX_ROWS_PER_PANEL else 0
    return max(1, min(len(events), MAX_ROWS_PER_PANEL) + hidden)


def panel_height(events):
    return PANEL_HEAD + panel_rows(events) * ROW_HEIGHT + 0.22


def draw_panel(ax, font, top, title, events, latest_index):
    x, width = MARGIN, CARD_WIDTH - 2 * MARGIN
    height = panel_height(events)
    box(ax, x, top, width, height)
    left = x + 0.32
    right = left + COLUMNS[-1][1]
    label(ax, left, top + 0.40, title, font(15, bold=True), AMBER)

    header_y = top + 0.80
    label(ax, left, header_y, "DATE", font(10.5, bold=True), MUTED)
    for name, offset in COLUMNS:
        label(ax, left + offset, header_y, name, font(10.5, bold=True), MUTED, ha="right")
    ax.plot([left, right], [header_y + 0.17] * 2, color=AMBER, linewidth=1, alpha=0.6)

    shown = events[-MAX_ROWS_PER_PANEL:]
    y = top + PANEL_HEAD + ROW_HEIGHT / 2
    if len(events) > len(shown):
        label(ax, left, y - 0.02, f"Most recent {len(shown)} shown · {plural(len(events) - len(shown), 'earlier event')} "
              "counted in the figures above", font(10), MUTED, max_width=right - left)
        y += ROW_HEIGHT
    if not events:
        label(ax, x + width / 2, y, "None", font(12), MUTED, ha="center")

    for event in shown:
        is_latest = event["index"] == latest_index
        if is_latest:
            ax.add_patch(mpatches.FancyBboxPatch(
                (left - 0.16, y - ROW_HEIGHT / 2 + 0.03), right - left + 0.32, ROW_HEIGHT - 0.06,
                boxstyle="round,pad=0,rounding_size=0.06", facecolor="#3a3220", edgecolor=AMBER, linewidth=1))

        # An asterisk marks a drop from before the earnings calendar begins.
        date_font = font(13, bold=True)
        date_text = event["date"].isoformat() + ("*" if event.get("earnings", False) is None else "")
        label(ax, left, y, date_text, date_font, AMBER if is_latest else TEXT)
        if is_latest:
            label(ax, left + text_width(date_text, date_font) + 0.14, y, "LATEST", font(9, bold=True), AMBER)

        values = [(f"{event['change']:.2f}%", RED, True)]
        for horizon in HORIZONS:
            value = event["forward"][horizon]
            values.append((signed(value), value_color(value), horizon == 60))
        low = event["low"]
        low_color = MUTED if low is None or low > -0.05 else RED
        low_text = "—" if low is None else ("0.0%" if low > -0.05 else f"{low:.1f}%")
        values.append((low_text, low_color, False))
        for (name, offset), (text, color, bold) in zip(COLUMNS, values):
            label(ax, left + offset, y, text, font(13, bold=bold), color, ha="right")
        y += ROW_HEIGHT
    return height


def build_notes(summary, symbol, events=()):
    notes = []
    losers = summary["losers"]
    if summary["finished"]:
        if losers:
            # The most recent ones, like the tables.
            dates = ("…, " if len(losers) > 6 else "") + ", ".join(e["date"].isoformat() for e in losers[-6:])
            notes.append((f"{len(losers)} of {summary['finished']} were lower 60 trading days later", dates))
        else:
            notes.append((f"All {summary['finished']} were higher 60 trading days later", ""))
    latest, rank = summary["latest"], summary["latest_rank"]
    if latest and rank:
        notes.append((f"The latest drop, {latest['change']:.2f}% on {latest['date'].isoformat()}, "
                      f"is the {ordinal(rank)} one-day drop in {symbol}'s history", ""))
    elif summary["worst"]:
        worst = summary["worst"]
        notes.append((f"Biggest drop: {worst['change']:.2f}% on {worst['date'].isoformat()}", ""))
    if summary["clustered"]:
        n = len(summary["clustered"])
        notes.append(("", f"{plural(n, 'drop')} came within {CLUSTER_DAYS} trading days of an earlier one, so their "
                          "outcomes overlap and the events are not fully independent."))
    if any(e.get("earnings", False) is None for e in events):
        notes.append(("", f"* Before {CALENDAR_START.year}, no earnings calendar is available, "
                          "so these drops are not classified."))
    return notes


def notes_height(notes):
    return sum(0.62 if h and d else 0.40 for h, d in notes) + 0.25 if notes else 0


def draw_card(symbol, meta, dates, threshold, events, classified, summary, out_path=OUTPUT_FILE):
    font = Fonts()
    if classified:
        others = [e for e in events if not e["earnings"]]
        unknown = any(e["earnings"] is None for e in others)
        panels = [("EARNINGS-RELATED", [e for e in events if e["earnings"]]),
                  ("OTHER" if unknown else "NON-EARNINGS", others)]
    else:
        panels = [("ALL EVENTS", events)]
    notes = build_notes(summary, symbol, events if classified else ())
    tables_top = STAT_TOP + STAT_HEIGHT + 0.30
    tables_height = sum(panel_height(p) for _, p in panels) + PANEL_GAP * (len(panels) - 1)
    height = tables_top + tables_height + 0.30 + notes_height(notes) + FOOTER_HEIGHT

    fig, ax = canvas(CARD_WIDTH, height)
    name = meta.get("longName") or meta.get("shortName") or symbol
    exchange = meta.get("fullExchangeName") or meta.get("exchangeName") or ""

    label(ax, MARGIN, 0.42, "MARKET PULSE  ·  DROP STUDY", font(11, bold=True), AMBER)
    label(ax, CARD_WIDTH - MARGIN, 0.42, f"{symbol}  |  {name}" + (f"  ·  {exchange}" if exchange else ""),
          font(11, bold=True), MUTED, ha="right", max_width=7.5)
    label(ax, MARGIN, 1.02, f"BUY THE DIP IN {symbol}?", font(38, bold=True), TEXT)
    since = dates[0].strftime("%Y")
    label(ax, MARGIN, 1.62,
          f"Every single-day drop of {threshold:g}% or more since {since}, and what the stock did next.",
          font(15), MUTED)
    ax.plot([MARGIN, CARD_WIDTH - MARGIN], [1.86, 1.86], color=AMBER, linewidth=1.2, alpha=0.7)

    # Headline figures, each set against buying at the close of any day.
    normal = summary["baseline"]
    def any_day(value, text):
        return None if value is None else f"Any day: {text}"
    stats = [
        (str(summary["count"]), f"{threshold:g}%+ drops since {since}", None),
        ("—" if summary["win_rate"] is None else f"{summary['win_rate']:.1f}%",
         f"Higher 60 days later ({summary['winners']}/{summary['finished']})",
         any_day(normal["win_rate"], f"{normal['win_rate']:.1f}%" if normal["win_rate"] is not None else "")),
        (signed(summary["average_60"]), "Average return 60 days later",
         any_day(normal["average_60"], signed(normal["average_60"]))),
        (signed(summary["average_250"]), "Average return 1 year later",
         any_day(normal["average_250"], signed(normal["average_250"]))),
        ("—" if summary["average_low"] is None else f"{summary['average_low']:.1f}%",
         "Avg. further fall in 60 days",
         any_day(normal["average_low"], f"{normal['average_low']:.1f}%" if normal["average_low"] is not None else "")),
    ]
    gap = 0.18
    stat_width = (CARD_WIDTH - 2 * MARGIN - gap * (len(stats) - 1)) / len(stats)
    for i, (value, caption, normal_text) in enumerate(stats):
        draw_stat(ax, font, MARGIN + i * (stat_width + gap), stat_width, value, caption, normal_text)

    latest_index = summary["latest"]["index"] if summary["latest"] else None
    y = tables_top
    for title, panel_events in panels:
        y += draw_panel(ax, font, y, f"{title}  ·  {plural(len(panel_events), 'EVENT')}",
                        panel_events, latest_index) + PANEL_GAP

    y += 0.30 - PANEL_GAP + 0.20
    for headline, detail in notes:
        if headline:
            label(ax, MARGIN, y, headline, font(12.5, bold=True), AMBER, max_width=CARD_WIDTH - 2 * MARGIN)
        if detail:
            label(ax, MARGIN, y + (0.30 if headline else 0), detail, font(11), MUTED,
                  max_width=CARD_WIDTH - 2 * MARGIN)
        y += 0.62 if headline and detail else 0.40

    footer_y = height - FOOTER_HEIGHT + 0.36
    ax.plot([MARGIN, CARD_WIDTH - MARGIN], [footer_y - 0.24] * 2, color=RULE, linewidth=1)
    source = ("Yahoo Finance adjusted closes; earnings dates: Nasdaq. Returns from the close of the drop day, "
              "in trading days (1Y = 250). Any day = buying at every close since the first.")
    if not classified:
        source = ("Yahoo Finance adjusted closes (earnings calendar unavailable). Returns from the close of the "
                  "drop day, in trading days (1Y = 250).")
    label(ax, MARGIN, footer_y, source, font(9.5), MUTED, max_width=CARD_WIDTH - 2 * MARGIN)
    label(ax, MARGIN, footer_y + 0.30, "Past results do not guarantee future returns.", font(9.5, bold=True), RED)
    return save(fig, out_path)


# ============================================================
# Entry point
# ============================================================

def run(symbol, threshold=None):
    """Build the study image. Returns its path."""
    symbol = normalise_symbol(symbol)
    dates, closes, meta = fetch_history(symbol)
    threshold, events = choose_threshold(dates, closes, threshold)
    if not events:
        raise StudyError(f"{symbol} 自 {dates[0].year} 年以来没有单日跌幅达到 {threshold:g}% 的交易日。")
    classified = classify(events, dates, symbol)
    summary = summarise(events, dates, closes)
    print(f"{symbol}: {len(events)} drops of {threshold:g}%+ since {dates[0]}, classified={classified}")
    return draw_card(symbol, meta, dates, threshold, events, classified, summary)


def parse_threshold(raw):
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except ValueError:
        raise StudyError(f"“{raw}” 不是有效的跌幅阈值。")
    if not 1 <= value <= 50:
        raise StudyError("跌幅阈值需要在 1 到 50 之间。")
    return value


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    token = os.environ.get(INTERACTION_TOKEN_ENV, "")
    symbol = os.environ.get("SYMBOL") or (args[0] if args else "")
    raw_threshold = os.environ.get("THRESHOLD") or (args[1] if len(args) > 1 else "")

    def reply(payload, file=None):
        if token and not DRY_RUN:
            edit_interaction_response(os.environ[APPLICATION_ID_ENV], token, payload, file=file)

    try:
        chart = run(symbol, parse_threshold(raw_threshold))
    except StudyError as exc:
        print(f"Not posted: {exc}")
        reply({"content": str(exc)})
        return

    if DRY_RUN or not token:
        print(f"[dry run] rendered {chart}")
        return
    reply({"embeds": [{"description": f"Buy the dip? · {normalise_symbol(symbol)}", "color": 15105570,
                       "image": {"url": f"attachment://{chart.name}"}}],
           "allowed_mentions": {"parse": []}},
          file=(chart.name, chart.read_bytes()))
    chart.unlink(missing_ok=True)


if __name__ == "__main__":
    main()

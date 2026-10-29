"""Buy-the-dip study for one stock: every large single-day drop since the IPO and
what the stock did 1, 20 and 60 trading days later, split into drops on an
earnings release and the rest.

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
HORIZONS = (1, 20, 60)
DEFAULT_THRESHOLD = 8.0
# Without an explicit threshold, the default is moved until the study has a
# readable number of events: volatile stocks fall 8% far too often, steady
# ones almost never.
AUTO_THRESHOLDS = (5, 6, 7, 8, 10, 12, 15, 20, 25, 30)
MIN_EVENTS, MAX_EVENTS = 4, 24
MAX_ROWS_PER_PANEL = 12

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
        events.append({"index": i, "date": dates[i], "change": change, "forward": forward})
    return events


def choose_threshold(dates, closes, requested=None):
    """The requested threshold, or the default moved until the event count is readable."""
    if requested is not None:
        return float(requested), find_drops(dates, closes, float(requested))

    events = find_drops(dates, closes, DEFAULT_THRESHOLD)
    if MIN_EVENTS <= len(events) <= MAX_EVENTS:
        return DEFAULT_THRESHOLD, events
    if len(events) > MAX_EVENTS:
        candidates = [t for t in AUTO_THRESHOLDS if t > DEFAULT_THRESHOLD]
    else:
        candidates = [t for t in reversed(AUTO_THRESHOLDS) if t < DEFAULT_THRESHOLD]
    for threshold in candidates:
        events = find_drops(dates, closes, threshold)
        if MIN_EVENTS <= len(events) <= MAX_EVENTS:
            return float(threshold), events
    # Nothing lands in range: take the last candidate tried (the closest to range).
    return float(candidates[-1]) if candidates else DEFAULT_THRESHOLD, events


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
    """Headline numbers and the two notes under the tables."""
    finished = [e for e in events if e["forward"][60] is not None]
    winners = [e for e in finished if e["forward"][60] > 0]
    summary = {
        "count": len(events),
        "finished": len(finished),
        "winners": len(winners),
        "win_rate": len(winners) / len(finished) * 100 if finished else None,
        "average_60": sum(e["forward"][60] for e in finished) / len(finished) if finished else None,
        "losers": [e for e in finished if e["forward"][60] <= 0],
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

CARD_WIDTH = 14.0
MARGIN = 0.48
STAT_TOP, STAT_HEIGHT = 2.05, 1.02
PANEL_TOP = 3.35
ROW_HEIGHT = 0.47
PANEL_HEAD = 1.05
FOOTER_HEIGHT = 0.62


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


def draw_stat(ax, font, x, width, value, caption):
    box(ax, x, STAT_TOP, width, STAT_HEIGHT)
    label(ax, x + 0.24, STAT_TOP + 0.38, value, font(24, bold=True), AMBER)
    label(ax, x + 0.24, STAT_TOP + 0.78, caption, font(11), MUTED, max_width=width - 0.4)


def draw_panel(ax, font, x, width, height, title, events, latest_index):
    box(ax, x, PANEL_TOP, width, height)
    left, right = x + 0.30, x + width - 0.30
    label(ax, left, PANEL_TOP + 0.40, title, font(15, bold=True), AMBER)

    columns = [("DROP", right - 3.15), ("+1D", right - 2.15), ("+20D", right - 1.10), ("+60D", right)]
    header_y = PANEL_TOP + 0.82
    label(ax, left, header_y, "DATE", font(10.5, bold=True), MUTED)
    for name, cx in columns:
        label(ax, cx, header_y, name, font(10.5, bold=True), MUTED, ha="right")
    ax.plot([left, right], [header_y + 0.17] * 2, color=AMBER, linewidth=1, alpha=0.6)

    shown = events[-MAX_ROWS_PER_PANEL:]
    y = PANEL_TOP + PANEL_HEAD + ROW_HEIGHT / 2
    if len(events) > len(shown):
        label(ax, left, y - 0.02, f"{plural(len(events) - len(shown), 'earlier event')} not shown", font(10), MUTED)
        y += ROW_HEIGHT
    if not events:
        label(ax, x + width / 2, y, "None", font(12), MUTED, ha="center")
    for event in shown:
        is_latest = event["index"] == latest_index
        if is_latest:
            ax.add_patch(mpatches.FancyBboxPatch(
                (left - 0.14, y - ROW_HEIGHT / 2 + 0.03), right - left + 0.28, ROW_HEIGHT - 0.06,
                boxstyle="round,pad=0,rounding_size=0.06", facecolor="#3a3220", edgecolor=AMBER, linewidth=1))
        date_font = font(13, bold=True)
        # An asterisk marks a drop from before the earnings calendar begins.
        date_text = event["date"].isoformat() + ("*" if event.get("earnings", False) is None else "")
        label(ax, left, y, date_text, date_font, AMBER if is_latest else TEXT)
        if is_latest:
            label(ax, left + text_width(event["date"].isoformat(), date_font) + 0.14, y, "LATEST",
                  font(9, bold=True), AMBER)
        label(ax, columns[0][1], y, f"{event['change']:.2f}%", font(13, bold=True), RED, ha="right")
        for (name, cx), horizon in zip(columns[1:], HORIZONS):
            value = event["forward"][horizon]
            bold = horizon == 60
            label(ax, cx, y, signed(value), font(13, bold=bold), value_color(value), ha="right")
        y += ROW_HEIGHT


def panel_rows(events):
    hidden = 1 if len(events) > MAX_ROWS_PER_PANEL else 0
    return max(1, min(len(events), MAX_ROWS_PER_PANEL) + hidden)


def build_notes(summary, symbol, events=()):
    notes = []
    losers = summary["losers"]
    if summary["finished"]:
        if losers:
            dates = ", ".join(e["date"].isoformat() for e in losers[:6]) + ("…" if len(losers) > 6 else "")
            notes.append((f"{len(losers)} of {summary['finished']} were lower 60 trading days later",
                          dates))
        else:
            notes.append((f"All {summary['finished']} were higher 60 trading days later", ""))
    latest, rank = summary["latest"], summary["latest_rank"]
    if latest and rank:
        notes.append((f"The latest drop, {latest['change']:.2f}% on {latest['date'].isoformat()}, "
                      f"is the {ordinal(rank)} one-day drop in {symbol}'s history", ""))
    elif summary["worst"]:
        worst = summary["worst"]
        notes.append((f"Biggest drop: {worst['change']:.2f}% on {worst['date'].isoformat()}", ""))
    if any(e.get("earnings", False) is None for e in events):
        notes.append(("", f"* Before {CALENDAR_START.year}, no earnings calendar is available, "
                          "so these drops are not classified."))
    return notes


def draw_card(symbol, meta, dates, threshold, events, classified, summary, out_path=OUTPUT_FILE):
    font = Fonts()
    if classified:
        others = [e for e in events if not e["earnings"]]
        unknown = any(e["earnings"] is None for e in others)
        panels = [("EARNINGS-RELATED", [e for e in events if e["earnings"]]),
                  ("OTHER" if unknown else "NON-EARNINGS", others)]
    else:
        panels = [("ALL EVENTS", events)]
    rows = max(panel_rows(p) for _, p in panels)
    panel_height = PANEL_HEAD + rows * ROW_HEIGHT + 0.25
    notes = build_notes(summary, symbol, events if classified else ())
    notes_height = sum(0.62 if h and d else 0.45 for h, d in notes) + 0.30 if notes else 0
    height = PANEL_TOP + panel_height + 0.25 + max(notes_height, 0) + FOOTER_HEIGHT

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

    gap = 0.22
    stat_width = (CARD_WIDTH - 2 * MARGIN - 3 * gap) / 4
    stats = [
        (str(summary["count"]), f"Drops of {threshold:g}%+ in a day since {since}"),
        (f"{summary['winners']} / {summary['finished']}", "Higher 60 trading days later"),
        ("—" if summary["win_rate"] is None else f"{summary['win_rate']:.1f}%", "Win rate at +60 trading days"),
        (signed(summary["average_60"]), "Average return 60 trading days later"),
    ]
    for i, (value, caption) in enumerate(stats):
        draw_stat(ax, font, MARGIN + i * (stat_width + gap), stat_width, value, caption)

    latest_index = summary["latest"]["index"] if summary["latest"] else None
    panel_gap = 0.28
    panel_width = (CARD_WIDTH - 2 * MARGIN - panel_gap * (len(panels) - 1)) / len(panels)
    for i, (title, panel_events) in enumerate(panels):
        draw_panel(ax, font, MARGIN + i * (panel_width + panel_gap), panel_width, panel_height,
                   f"{title}  ·  {plural(len(panel_events), 'EVENT')}", panel_events, latest_index)

    y = PANEL_TOP + panel_height + 0.45
    for headline, detail in notes:
        if headline:
            label(ax, MARGIN, y, headline, font(12.5, bold=True), AMBER, max_width=CARD_WIDTH - 2 * MARGIN)
        if detail:
            label(ax, MARGIN, y + (0.30 if headline else 0), detail, font(11), MUTED,
                  max_width=CARD_WIDTH - 2 * MARGIN)
        y += 0.62 if headline and detail else 0.45

    footer_y = height - FOOTER_HEIGHT / 2
    ax.plot([MARGIN, CARD_WIDTH - MARGIN], [footer_y - 0.24] * 2, color=RULE, linewidth=1)
    source = "Source: Yahoo Finance adjusted daily closes; earnings dates: Nasdaq. From the close of the drop day; trading days."
    if not classified:
        source = "Source: Yahoo Finance adjusted daily closes (earnings calendar unavailable). From the close of the drop day."
    label(ax, MARGIN, footer_y, source, font(10), MUTED, max_width=CARD_WIDTH - 2 * MARGIN - 3.4)
    label(ax, CARD_WIDTH - MARGIN, footer_y, "Past results do not guarantee future returns.", font(10, bold=True),
          RED, ha="right")
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

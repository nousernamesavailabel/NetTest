#!/usr/bin/env python3
"""
tui.py
Terminal UI for the Network Test Controller.
Live-updating dashboard showing current path status, recent results,
and per-metric health at a glance.

Usage:
  python tui.py                     # Live dashboard (auto-refreshes)
  python tui.py --once              # Print snapshot and exit
  python tui.py --path branch_a_to_hub   # Focus on one path
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import List, Optional

from rich import box
from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.rule import Rule
from rich.align import Align
from rich.style import Style
from rich import print as rprint

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core.config_loader import load_config
from core.results import ResultStore

console = Console()

# ── Thresholds for health colouring ───────────────────────
THRESHOLDS = {
    "latency_warn_ms":      20,
    "latency_crit_ms":      80,
    "loss_warn_pct":         1,
    "loss_crit_pct":         5,
    "jitter_warn_ms":        5,
    "jitter_crit_ms":       20,
    "throughput_warn_mbps": 10,
    "throughput_crit_mbps":  2,
    "bufferbloat_warn_ms":  30,
    "bufferbloat_crit_ms": 100,
}


def health_colour(value: float, warn: float, crit: float,
                  invert: bool = False) -> str:
    """
    Returns a Rich colour name based on threshold comparison.
    invert=True means higher is worse (latency, loss, jitter).
    invert=False means lower is worse (throughput).
    """
    if invert:
        if value >= crit:   return "red"
        if value >= warn:   return "yellow"
        return "green"
    else:
        if value <= crit:   return "red"
        if value <= warn:   return "yellow"
        return "green"


def fmt_val(value, unit: str = "", precision: int = 1) -> str:
    if value is None:
        return "[dim]—[/dim]"
    return f"{value:.{precision}f}{unit}"


# ── Summary cards (top row) ────────────────────────────────

def make_summary_cards(records: List[dict]) -> Columns:
    if not records:
        return Columns([Panel("[dim]No results yet[/dim]", expand=True)])

    total      = len(records)
    ok         = sum(1 for r in records if r.get("success"))
    failed     = total - ok
    last_ts    = records[-1]["timestamp_utc"][:19].replace("T", " ") if records else "—"

    avg_latency = None
    avg_tput    = None
    lat_vals    = [r["latency"]["rtt_avg_ms"]
                   for r in records if r.get("latency")]
    tput_vals   = [r["throughput"]["tx_mbps"]
                   for r in records if r.get("throughput")]
    if lat_vals:
        avg_latency = sum(lat_vals) / len(lat_vals)
    if tput_vals:
        avg_tput = sum(tput_vals) / len(tput_vals)

    ok_colour     = "green" if failed == 0 else ("yellow" if failed <= 2 else "red")
    lat_colour    = health_colour(avg_latency or 0,
                                  THRESHOLDS["latency_warn_ms"],
                                  THRESHOLDS["latency_crit_ms"],
                                  invert=True) if avg_latency else "dim"
    tput_colour   = health_colour(avg_tput or 0,
                                  THRESHOLDS["throughput_warn_mbps"],
                                  THRESHOLDS["throughput_crit_mbps"],
                                  invert=False) if avg_tput else "dim"

    cards = [
        Panel(
            Align(Text(f"{ok}/{total}", style=f"bold {ok_colour}", justify="center"), align="center"),
            title="[dim]paths OK[/dim]", expand=True,
        ),
        Panel(
            Align(Text(f"{fmt_val(avg_latency, 'ms')}", style=f"bold {lat_colour}", justify="center"), align="center"),
            title="[dim]avg latency[/dim]", expand=True,
        ),
        Panel(
            Align(Text(f"{fmt_val(avg_tput, ' Mbps')}", style=f"bold {tput_colour}", justify="center"), align="center"),
            title="[dim]avg throughput[/dim]", expand=True,
        ),
        Panel(
            Align(Text(failed and f"[red]{failed} failed[/red]" or "[green]0 failed[/green]",
                       justify="center"), align="center"),
            title="[dim]failures[/dim]", expand=True,
        ),
        Panel(
            Align(Text(last_ts, style="dim", justify="center"), align="center"),
            title="[dim]last run[/dim]", expand=True,
        ),
    ]
    return Columns(cards, expand=True)


# ── Main results table ─────────────────────────────────────

def make_results_table(records: List[dict], path_filter: Optional[str] = None) -> Table:
    table = Table(
        box=box.SIMPLE_HEAD,
        show_footer=False,
        pad_edge=False,
        expand=True,
    )
    table.add_column("Path",          style="bold",    min_width=28)
    table.add_column("Time (UTC)",    style="dim",     min_width=17)
    table.add_column("Status",                         min_width=8,  justify="center")
    table.add_column("TX Mbps",                        min_width=9,  justify="right")
    table.add_column("RX Mbps",                        min_width=9,  justify="right")
    table.add_column("RTT avg",                        min_width=9,  justify="right")
    table.add_column("RTT max",                        min_width=9,  justify="right")
    table.add_column("Loss %",                         min_width=8,  justify="right")
    table.add_column("Jitter",                         min_width=8,  justify="right")
    table.add_column("Δ loaded",                       min_width=9,  justify="right")
    table.add_column("MTU",                            min_width=7,  justify="right")
    table.add_column("Retr",                           min_width=6,  justify="right")

    # Show most recent 25 records, newest first
    display = list(reversed(records[-50:]))
    if path_filter:
        display = [r for r in display if r.get("path_id") == path_filter]

    for r in display[:25]:
        status_icon = "[green]✓[/green]" if r.get("success") else "[red]✗[/red]"
        ts          = r["timestamp_utc"][11:19]    # HH:MM:SS only

        t  = r.get("throughput")
        l  = r.get("latency")
        lu = r.get("latency_under_load")
        j  = r.get("jitter")
        m  = r.get("mtu")

        tx_mbps  = f"[{health_colour(t['tx_mbps'], THRESHOLDS['throughput_warn_mbps'], THRESHOLDS['throughput_crit_mbps'], invert=False)}]{t['tx_mbps']:.1f}[/]"  if t else "[dim]—[/dim]"
        rx_mbps  = f"{t['rx_mbps']:.1f}"  if t else "[dim]—[/dim]"
        retr     = f"[{'red' if t and t['retransmits'] > 10 else 'default'}]{t['retransmits']}[/]" if t else "[dim]—[/dim]"

        rtt_avg  = f"[{health_colour(l['rtt_avg_ms'], THRESHOLDS['latency_warn_ms'], THRESHOLDS['latency_crit_ms'], invert=True)}]{l['rtt_avg_ms']:.1f}ms[/]" if l else "[dim]—[/dim]"
        rtt_max  = f"{l['rtt_max_ms']:.1f}ms" if l else "[dim]—[/dim]"
        loss_pct = f"[{health_colour(l['packet_loss_pct'], THRESHOLDS['loss_warn_pct'], THRESHOLDS['loss_crit_pct'], invert=True)}]{l['packet_loss_pct']:.1f}%[/]" if l else "[dim]—[/dim]"

        jitter   = f"[{health_colour(j['jitter_ms'], THRESHOLDS['jitter_warn_ms'], THRESHOLDS['jitter_crit_ms'], invert=True)}]{j['jitter_ms']:.2f}ms[/]" if j else "[dim]—[/dim]"

        delta    = f"[{health_colour(lu['delta_ms'], THRESHOLDS['bufferbloat_warn_ms'], THRESHOLDS['bufferbloat_crit_ms'], invert=True)}]+{lu['delta_ms']:.1f}ms[/]" if lu else "[dim]—[/dim]"

        mtu_val  = f"{'[yellow]' if m and m['fragmentation_detected'] else ''}{m['effective_mtu_bytes']}{'[/]' if m and m['fragmentation_detected'] else ''}" if m else "[dim]—[/dim]"

        table.add_row(
            r["path_label"], ts, status_icon,
            tx_mbps, rx_mbps, rtt_avg, rtt_max, loss_pct,
            jitter, delta, mtu_val, retr,
        )

    if not display:
        table.add_row(*["[dim]—[/dim]"] * 12)

    return table


# ── Per-hop MTR detail ─────────────────────────────────────

def make_hop_table(records: List[dict], path_filter: Optional[str]) -> Optional[Table]:
    """Show MTR hop breakdown for the most recent latency-under-load result."""
    relevant = [
        r for r in reversed(records)
        if r.get("latency_under_load")
        and r["latency_under_load"].get("mtr_hops")
        and (not path_filter or r.get("path_id") == path_filter)
    ]
    if not relevant:
        return None

    latest = relevant[0]
    hops   = latest["latency_under_load"]["mtr_hops"]
    ts     = latest["timestamp_utc"][:19].replace("T", " ")

    table = Table(
        title=f"[dim]MTR hops under load — {latest['path_label']} @ {ts}[/dim]",
        box=box.SIMPLE_HEAD,
        pad_edge=False,
        expand=True,
    )
    table.add_column("Hop",    justify="right",  min_width=4)
    table.add_column("Host",                     min_width=30)
    table.add_column("Loss %", justify="right",  min_width=8)
    table.add_column("Avg ms", justify="right",  min_width=8)
    table.add_column("Best",   justify="right",  min_width=8)
    table.add_column("Worst",  justify="right",  min_width=8)
    table.add_column("StDev",  justify="right",  min_width=8)

    for h in hops:
        loss_c = health_colour(h.get("loss_pct", 0),
                               THRESHOLDS["loss_warn_pct"],
                               THRESHOLDS["loss_crit_pct"], invert=True)
        avg_c  = health_colour(h.get("avg_ms", 0),
                               THRESHOLDS["latency_warn_ms"],
                               THRESHOLDS["latency_crit_ms"], invert=True)
        table.add_row(
            str(h.get("hop", "?")),
            h.get("host", "???"),
            f"[{loss_c}]{h.get('loss_pct', 0):.1f}%[/]",
            f"[{avg_c}]{h.get('avg_ms', 0):.2f}[/]",
            f"{h.get('best_ms', 0):.2f}",
            f"{h.get('worst_ms', 0):.2f}",
            f"{h.get('stddev_ms', 0):.2f}",
        )
    return table


# ── Legend ────────────────────────────────────────────────

LEGEND = (
    "[green]■[/green] OK  "
    "[yellow]■[/yellow] Warn  "
    "[red]■[/red] Critical  "
    "[dim]  Δ loaded = bufferbloat indicator  "
    "Retr = TCP retransmits[/dim]"
)


# ── Full dashboard render ─────────────────────────────────

def render_dashboard(records: List[dict],
                     path_filter: Optional[str] = None) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header",   size=3),
        Layout(name="cards",    size=5),
        Layout(name="table",    size=30),
        Layout(name="hops",     size=12),
        Layout(name="footer",   size=1),
    )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    header_text = Text(justify="center")
    header_text.append("⬡  NetTest Controller", style="bold")
    header_text.append(f"   {now}", style="dim")
    if path_filter:
        header_text.append(f"   filter: {path_filter}", style="italic yellow")
    layout["header"].update(Panel(Align(header_text, align="center"), box=box.MINIMAL))

    layout["cards"].update(make_summary_cards(records))
    layout["table"].update(
        Panel(make_results_table(records, path_filter),
              title="[dim]Recent results[/dim]",
              box=box.ROUNDED, padding=(0, 1))
    )

    hop_table = make_hop_table(records, path_filter)
    if hop_table:
        layout["hops"].update(Panel(hop_table, box=box.ROUNDED, padding=(0, 1)))
    else:
        layout["hops"].update(Panel("[dim]No MTR data yet — runs with latency_under_load tests[/dim]",
                                    box=box.ROUNDED))

    layout["footer"].update(Align(Text(LEGEND, justify="center"), align="center"))

    return layout


# ── Entry point ───────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="NetTest Terminal UI")
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--once",   action="store_true",
                        help="Print once and exit")
    parser.add_argument("--path",   metavar="PATH_ID",
                        help="Focus display on one path")
    parser.add_argument("--refresh", type=int, default=10,
                        help="Refresh interval in seconds (default: 10)")
    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except Exception as e:
        console.print(f"[red]Config error:[/red] {e}")
        sys.exit(1)

    store = ResultStore(config.results_dir)

    if args.once:
        records = store.load_today()
        console.print(render_dashboard(records, args.path))
        return

    # Live auto-refresh mode
    try:
        with Live(render_dashboard(store.load_today(), args.path),
                  refresh_per_second=1,
                  screen=True) as live:
            while True:
                time.sleep(args.refresh)
                records = store.load_today()
                live.update(render_dashboard(records, args.path))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

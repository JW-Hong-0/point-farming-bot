import os
import time
from datetime import datetime
from typing import Dict


class Dashboard:
    def __init__(self, bot):
        self.bot = bot
        self.last_render_ts = 0.0

    def _clear(self) -> None:
        if os.name == "nt":
            os.system("cls")
        else:
            os.system("clear")

    def render(self) -> None:
        now = time.time()
        refresh = max(float(getattr(self.bot.config, "DASHBOARD_REFRESH_S", 1.0)), 0.2)
        if (now - self.last_render_ts) < refresh:
            return
        self.last_render_ts = now

        self._clear()
        self._header(now)
        self._balances()
        self._assignments()

    def _header(self, now: float) -> None:
        next_ts = float(getattr(self.bot, "next_rotation_ts", 0.0) or 0.0)
        remain_s = max(0.0, next_ts - now) if next_ts else 0.0
        remain_h = remain_s / 3600.0

        print("================ POINT FARMING BOT ================")
        print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(
            f"Cycle: {self.bot.cycle_id} | Active Pairs: {len(self.bot.assignments)} | "
            f"Next Rotation In: {remain_h:.2f}h"
        )
        print("-" * 56)

    def _balances(self) -> None:
        print("[Balances]")
        print(f"{'Exchange':<8} {'Total':>14} {'Available':>14}")
        print("-" * 40)
        for venue in sorted(self.bot.exchanges.keys()):
            b = self.bot.balance_cache.get(venue, {})
            total = float(b.get("total", 0.0) or 0.0)
            avail = float(b.get("available", 0.0) or 0.0)
            print(f"{venue:<8} {total:>14.4f} {avail:>14.4f}")
        print("-" * 56)

    def _assignments(self) -> None:
        print("[Hedged Pairs]")
        if not self.bot.assignments:
            print("(none)")
            print("-" * 56)
            return

        print(f"{'Ticker':<8} {'Long':<6} {'Short':<6} {'Qty':>12} {'Lev':>6} {'Age(h)':>8}")
        print("-" * 56)
        now = time.time()
        for ticker in sorted(self.bot.assignments.keys()):
            a = self.bot.assignments[ticker]
            age_h = (now - a.opened_ts) / 3600.0
            print(
                f"{ticker:<8} {a.long_venue:<6} {a.short_venue:<6} "
                f"{a.qty:>12.6f} {a.leverage:>6} {age_h:>8.2f}"
            )
        print("-" * 56)

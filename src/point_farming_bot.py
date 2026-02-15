import asyncio
import logging
import math
import random
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Dict, List, Optional, Tuple

from shared_crypto_lib.factory import ExchangeFactory, ExchangeType
from shared_crypto_lib.models import Balance, MarketInfo, OrderSide, OrderType, Position

from src.config import Config
from src.dashboard import Dashboard
from src.session_logger import PointSessionLogger

logger = logging.getLogger("PointFarmingBot")

PAIR_CHOICES: Tuple[Tuple[str, str], ...] = (
    ("GRVT", "HYNA"),
    ("GRVT", "VAR"),
    ("HYNA", "VAR"),
)


@dataclass
class Assignment:
    ticker: str
    long_venue: str
    short_venue: str
    qty: float
    leverage: int
    opened_ts: float


class PointFarmingBot:
    def __init__(self, session_logger: Optional[PointSessionLogger] = None, dashboard=None):
        if Config.RANDOM_SEED:
            random.seed(Config.RANDOM_SEED)

        self.grvt = None
        self.hyena = None
        self.variational = None
        self.exchanges: Dict[str, object] = {}
        self.assignments: Dict[str, Assignment] = {}
        self.next_rotation_ts: float = 0.0
        self.paused = bool(Config.TRADING_START_PAUSED)

        self.session_logger = session_logger
        self.dashboard = dashboard
        self.config = Config
        self.cycle_id = 0
        self._active_cycle_id = 0
        self.balance_cache: Dict[str, Dict[str, float]] = {}
        self.next_hedge_audit_ts: float = 0.0

        # (ticker, pair_key) -> fixed long venue
        self.direction_lock: Dict[Tuple[str, str], str] = {}

    async def initialize(self) -> None:
        self.grvt = ExchangeFactory.create_exchange(
            ExchangeType.GRVT,
            config={
                "api_key": Config.GRVT_API_KEY,
                "private_key": Config.GRVT_PRIVATE_KEY,
                "subaccount_id": Config.GRVT_TRADING_ACCOUNT_ID,
                "env": Config.GRVT_ENV,
            },
        )
        self.hyena = ExchangeFactory.create_exchange(
            ExchangeType.HYENA,
            config={
                "private_key": Config.HYENA_PRIVATE_KEY,
                "wallet_address": Config.HYENA_WALLET_ADDRESS,
                "main_address": Config.HYENA_MAIN_ADDRESS,
                "dex_id": Config.HYENA_DEX_ID,
                "use_symbol_prefix": Config.HYENA_USE_SYMBOL_PREFIX,
                "builder_address": Config.HYENA_BUILDER_ADDRESS,
                "builder_fee": Config.HYENA_BUILDER_FEE,
                "approve_builder_fee": Config.HYENA_APPROVE_BUILDER_FEE,
                "builder_max_fee_rate": Config.HYENA_BUILDER_MAX_FEE_RATE,
                "slippage": Config.HYENA_SLIPPAGE,
                "min_notional": Config.HYENA_MIN_NOTIONAL,
            },
        )

        self.variational = None
        if Config.ENABLE_VAR and Config.VARIATIONAL_WALLET_ADDRESS:
            self.variational = ExchangeFactory.create_exchange(
                ExchangeType.VARIATIONAL,
                config={
                    "wallet_address": Config.VARIATIONAL_WALLET_ADDRESS,
                    "vr_token": Config.VARIATIONAL_VR_TOKEN,
                    "private_key": Config.VARIATIONAL_PRIVATE_KEY,
                    "slippage": Config.VARIATIONAL_SLIPPAGE,
                },
            )

        await self.grvt.initialize()
        await self.hyena.initialize()
        if self.variational:
            await self.variational.initialize()

        self.exchanges = {
            "GRVT": self.grvt,
            "HYNA": self.hyena,
        }
        if self.variational:
            self.exchanges["VAR"] = self.variational

        logger.info("initialized exchanges: %s", sorted(self.exchanges.keys()))

    async def run(self) -> None:
        if self.paused:
            logger.warning("trading paused at startup (TRADING_START_PAUSED=1)")

        while True:
            try:
                await self._refresh_balances_cache()
                if not self.paused and time.time() >= self.next_rotation_ts:
                    await self.rotate_cycle()
                if not self.paused and time.time() >= self.next_hedge_audit_ts:
                    await self._audit_hedge_integrity()
                    self.next_hedge_audit_ts = time.time() + max(
                        3.0,
                        float(getattr(Config, "HEDGE_AUDIT_INTERVAL_S", 15.0)),
                    )
                if self.dashboard and bool(getattr(Config, "DASHBOARD_ENABLED", True)):
                    self.dashboard.render()
            except Exception as e:
                logger.exception("loop error: %s", e)
            await asyncio.sleep(max(1.0, float(Config.LOOP_INTERVAL_S)))

    async def rotate_cycle(self) -> None:
        self.cycle_id += 1
        self._active_cycle_id = self.cycle_id
        logger.info("cycle started id=%s", self._active_cycle_id)
        await self._ensure_direction_lock_from_assignments()

        closed = 0
        kept = 0
        for ticker in list(self.assignments.keys()):
            assignment = self.assignments.get(ticker)
            if not assignment:
                continue

            age_h = (time.time() - assignment.opened_ts) / 3600.0
            force_close = age_h >= float(Config.MAX_HOLD_HOURS)
            retain = random.random() < float(Config.RETAIN_PROBABILITY)

            if force_close or not retain:
                reason = "max_hold" if force_close else "random_drop"
                ok = await self._close_assignment(assignment, reason=reason)
                if ok:
                    closed += 1
                    self.assignments.pop(ticker, None)
                continue

            kept += 1
            logger.info(
                "keep %s pair=%s/%s age=%.2fh qty=%.8f lev=x%s",
                assignment.ticker,
                assignment.long_venue,
                assignment.short_venue,
                age_h,
                assignment.qty,
                assignment.leverage,
            )
            self._log_cycle_pair(
                ticker=assignment.ticker,
                long_venue=assignment.long_venue,
                short_venue=assignment.short_venue,
                qty=assignment.qty,
                leverage=assignment.leverage,
                state="kept",
                reason="retained",
            )

        await self._open_new_assignments_to_target()
        self._log_cycle_exchange_summary()

        self.next_rotation_ts = time.time() + self._random_rotation_interval_s()
        logger.info(
            "cycle finished id=%s closed=%s kept=%s active=%s next=%.0f",
            self._active_cycle_id,
            closed,
            kept,
            len(self.assignments),
            self.next_rotation_ts,
        )

    async def _open_new_assignments_to_target(self) -> None:
        max_active = max(0, int(Config.TARGET_ACTIVE_TICKERS))
        if max_active <= 0:
            return

        candidates = [t for t in Config.POINT_SYMBOLS if t not in self.assignments]
        random.shuffle(candidates)

        attempts = 0
        while len(self.assignments) < max_active and candidates and attempts < 200:
            attempts += 1
            pair_deficits, leg_deficits = self._compute_deficits(max_active)
            best = self._pick_best_candidate(candidates, pair_deficits, leg_deficits)
            if not best:
                break

            ticker, v1, v2 = best
            opened = await self._open_assignment_for_pair(ticker, v1, v2)
            if opened:
                candidates = [c for c in candidates if c != ticker]
            else:
                candidates = [c for c in candidates if c != ticker]

        if len(self.assignments) < max_active:
            logger.warning(
                "could not fill target_active=%s, current=%s",
                max_active,
                len(self.assignments),
            )

    def _pick_best_candidate(
        self,
        candidates: List[str],
        pair_deficits: Dict[str, int],
        leg_deficits: Dict[str, int],
    ) -> Optional[Tuple[str, str, str]]:
        best_score = None
        best_item = None

        for ticker in candidates:
            for v1, v2 in self._available_pairs_for_ticker(ticker):
                pkey = self._pair_key(v1, v2)
                pair_count = self._pair_counts().get(pkey, 0)
                score = 0.0
                if pair_deficits.get(pkey, 0) > 0:
                    score += 1000.0
                score += 20.0 * leg_deficits.get(v1, 0)
                score += 20.0 * leg_deficits.get(v2, 0)
                score += max(0.0, 10.0 - float(pair_count))
                score += random.random()

                if best_score is None or score > best_score:
                    best_score = score
                    best_item = (ticker, v1, v2)

        return best_item

    async def _open_assignment_for_pair(self, ticker: str, v1: str, v2: str) -> bool:
        lock_long = self._get_locked_long_venue(ticker, v1, v2)
        if lock_long:
            long_venue = lock_long
            short_venue = v2 if long_venue == v1 else v1
        else:
            if random.random() < 0.5:
                long_venue, short_venue = v1, v2
            else:
                long_venue, short_venue = v2, v1

        lev = self._effective_leverage_for_pair(ticker, long_venue, short_venue)
        if lev < 1:
            logger.warning("%s skip %s/%s: leverage cap invalid", ticker, long_venue, short_venue)
            return False

        qty = await self._compute_common_qty(ticker, long_venue, short_venue, lev)
        if qty <= 0:
            return False

        ok = await self._open_pair(ticker, long_venue, short_venue, qty, lev)
        if not ok:
            return False

        self.assignments[ticker] = Assignment(
            ticker=ticker,
            long_venue=long_venue,
            short_venue=short_venue,
            qty=qty,
            leverage=lev,
            opened_ts=time.time(),
        )
        self._set_locked_long_venue(ticker, long_venue, short_venue, long_venue)
        await self._sync_assignment_qty_from_live(ticker)
        self._log_cycle_pair(
            ticker=ticker,
            long_venue=long_venue,
            short_venue=short_venue,
            qty=qty,
            leverage=lev,
            state="opened",
            reason="new_assignment",
        )
        return True

    def _compute_deficits(self, target_active: int) -> Tuple[Dict[str, int], Dict[str, int]]:
        pair_counts = self._pair_counts()
        leg_counts = self._exchange_leg_counts()

        pair_deficits: Dict[str, int] = {}
        required_pairs = self._required_pair_keys(target_active)
        for pkey in required_pairs:
            if pair_counts.get(pkey, 0) < 1:
                pair_deficits[pkey] = 1

        leg_deficits: Dict[str, int] = {}
        min_legs = max(0, int(getattr(Config, "EXCHANGE_MIN_LEGS", 0)))
        if min_legs > 0:
            for venue in sorted(self.exchanges.keys()):
                cur = leg_counts.get(venue, 0)
                if cur < min_legs:
                    leg_deficits[venue] = min_legs - cur

        return pair_deficits, leg_deficits

    def _required_pair_keys(self, target_active: int) -> List[str]:
        if not bool(getattr(Config, "ENFORCE_ALL_PAIR_TYPES", False)):
            return []

        if target_active < len(PAIR_CHOICES):
            return []

        required = []
        for v1, v2 in PAIR_CHOICES:
            if v1 in self.exchanges and v2 in self.exchanges:
                required.append(self._pair_key(v1, v2))
        return required

    def _pair_counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for a in self.assignments.values():
            k = self._pair_key(a.long_venue, a.short_venue)
            out[k] = out.get(k, 0) + 1
        return out

    def _exchange_leg_counts(self) -> Dict[str, int]:
        out = {k: 0 for k in self.exchanges.keys()}
        for a in self.assignments.values():
            out[a.long_venue] = out.get(a.long_venue, 0) + 1
            out[a.short_venue] = out.get(a.short_venue, 0) + 1
        return out

    async def _close_assignment(self, assignment: Assignment, reason: str) -> bool:
        ticker = assignment.ticker
        ok_long = await self._close_venue_position(ticker, assignment.long_venue, reason=reason)
        ok_short = await self._close_venue_position(ticker, assignment.short_venue, reason=reason)
        logger.info(
            "close assignment %s pair=%s/%s reason=%s result=%s/%s",
            ticker,
            assignment.long_venue,
            assignment.short_venue,
            reason,
            ok_long,
            ok_short,
        )
        if ok_long and ok_short:
            self._log_cycle_pair(
                ticker=ticker,
                long_venue=assignment.long_venue,
                short_venue=assignment.short_venue,
                qty=assignment.qty,
                leverage=assignment.leverage,
                state="closed",
                reason=reason,
            )
        return ok_long and ok_short

    async def _close_venue_position(self, ticker: str, venue: str, reason: str) -> bool:
        ex = self.exchanges.get(venue)
        if not ex:
            return False
        symbol = self._symbol_for_venue(ticker, venue)
        if not symbol:
            return False

        tol = max(0.0, float(getattr(Config, "HEDGE_QTY_TOLERANCE", 0.02)))
        max_retry = max(1, int(getattr(Config, "CLOSE_RETRY_MAX", 3)))
        for attempt in range(1, max_retry + 1):
            signed_qty = await self._fetch_signed_position_qty(venue, ticker)
            if abs(signed_qty) <= tol:
                return True

            side = OrderSide.SELL if signed_qty > 0 else OrderSide.BUY
            qty = await self._adjust_qty_to_rules(
                ticker,
                venue,
                abs(signed_qty),
                allow_below_min_qty=True,
            )
            if qty <= 0:
                logger.warning(
                    "close skip %s %s: qty below tradable threshold signed=%.8f",
                    ticker,
                    venue,
                    signed_qty,
                )
                return False

            if Config.DRY_RUN:
                logger.info("[DRY_RUN] close %s %s %s qty=%s", ticker, venue, side.value, qty)
                self._log_trade(
                    action="close",
                    ticker=ticker,
                    exchange=venue,
                    side=side.value,
                    qty=qty,
                    leverage="",
                    reason=f"{reason}:dry_run",
                    order_id="",
                    status="dry_run",
                    dry_run=True,
                )
                return True

            try:
                order = await ex.create_order(symbol, OrderType.MARKET, side, qty, params={"reduce_only": True})
                logger.info(
                    "closed %s %s %s qty=%s attempt=%s/%s",
                    ticker,
                    venue,
                    side.value,
                    qty,
                    attempt,
                    max_retry,
                )
                self._log_trade(
                    action="close",
                    ticker=ticker,
                    exchange=venue,
                    side=side.value,
                    qty=qty,
                    leverage="",
                    reason=reason,
                    order_id=getattr(order, "id", ""),
                    status=getattr(getattr(order, "status", None), "value", ""),
                    dry_run=False,
                )
            except Exception as e:
                logger.error("close failed %s %s attempt=%s/%s: %s", ticker, venue, attempt, max_retry, e)
                if attempt >= max_retry:
                    return False
            await asyncio.sleep(0.2)

        rem = await self._fetch_signed_position_qty(venue, ticker)
        return abs(rem) <= tol

    async def _open_pair(self, ticker: str, long_venue: str, short_venue: str, qty: float, lev: int) -> bool:
        long_symbol = self._symbol_for_venue(ticker, long_venue)
        short_symbol = self._symbol_for_venue(ticker, short_venue)
        if not long_symbol or not short_symbol:
            return False

        long_ex = self.exchanges[long_venue]
        short_ex = self.exchanges[short_venue]

        p_long = await self._fetch_last_price(long_venue, ticker)
        p_short = await self._fetch_last_price(short_venue, ticker)
        if p_long <= 0 or p_short <= 0:
            logger.warning("open skip %s: invalid price for margin check", ticker)
            return False
        if not await self._has_sufficient_margin(long_venue, short_venue, p_long, p_short, qty, lev):
            logger.warning(
                "open skip %s: insufficient available margin for %s/%s qty=%.6f lev=%s",
                ticker,
                long_venue,
                short_venue,
                qty,
                lev,
            )
            return False

        await self._set_leverage_safe(long_venue, long_symbol, lev, ticker=ticker)
        await self._set_leverage_safe(short_venue, short_symbol, lev, ticker=ticker)

        if Config.DRY_RUN:
            logger.info(
                "[DRY_RUN] open %s long=%s short=%s qty=%.8f lev=x%s",
                ticker,
                long_venue,
                short_venue,
                qty,
                lev,
            )
            self._log_trade(
                action="open",
                ticker=ticker,
                exchange=long_venue,
                side="buy",
                qty=qty,
                leverage=lev,
                reason="open_pair:dry_run",
                order_id="",
                status="dry_run",
                dry_run=True,
            )
            self._log_trade(
                action="open",
                ticker=ticker,
                exchange=short_venue,
                side="sell",
                qty=qty,
                leverage=lev,
                reason="open_pair:dry_run",
                order_id="",
                status="dry_run",
                dry_run=True,
            )
            return True

        long_order = None
        try:
            long_order = await long_ex.create_order(long_symbol, OrderType.MARKET, OrderSide.BUY, qty)
            short_order = await short_ex.create_order(short_symbol, OrderType.MARKET, OrderSide.SELL, qty)
            logger.info(
                "opened %s long=%s short=%s qty=%.8f lev=x%s",
                ticker,
                long_venue,
                short_venue,
                qty,
                lev,
            )
            self._log_trade(
                action="open",
                ticker=ticker,
                exchange=long_venue,
                side="buy",
                qty=qty,
                leverage=lev,
                reason="open_pair",
                order_id=getattr(long_order, "id", ""),
                status=getattr(getattr(long_order, "status", None), "value", ""),
                dry_run=False,
            )
            self._log_trade(
                action="open",
                ticker=ticker,
                exchange=short_venue,
                side="sell",
                qty=qty,
                leverage=lev,
                reason="open_pair",
                order_id=getattr(short_order, "id", ""),
                status=getattr(getattr(short_order, "status", None), "value", ""),
                dry_run=False,
            )
            # Verify leverage after fills. If mismatch persists, close both legs to cap risk.
            ok_long_lev = await self._verify_position_leverage(ticker, long_venue, lev)
            ok_short_lev = await self._verify_position_leverage(ticker, short_venue, lev)
            if bool(getattr(Config, "ENFORCE_LEVERAGE_MATCH", True)) and (not ok_long_lev or not ok_short_lev):
                logger.error(
                    "leverage guard failed %s (%s=%s, %s=%s), force-closing pair",
                    ticker,
                    long_venue,
                    ok_long_lev,
                    short_venue,
                    ok_short_lev,
                )
                await self._close_venue_position(ticker, long_venue, reason="leverage_guard")
                await self._close_venue_position(ticker, short_venue, reason="leverage_guard")
                return False
            await self._normalize_pair_after_open(ticker, long_venue, short_venue)
            return True
        except Exception as e:
            logger.error("open failed %s (%s/%s): %s", ticker, long_venue, short_venue, e)
            if long_order:
                try:
                    await long_ex.create_order(long_symbol, OrderType.MARKET, OrderSide.SELL, qty)
                    logger.warning("rollback executed on %s %s", ticker, long_venue)
                except Exception as rollback_error:
                    logger.error("rollback failed on %s: %s", ticker, rollback_error)
            return False

    async def _compute_common_qty(self, ticker: str, v1: str, v2: str, lev: int) -> float:
        p1 = await self._fetch_last_price(v1, ticker)
        p2 = await self._fetch_last_price(v2, ticker)
        if p1 <= 0 or p2 <= 0:
            return 0.0

        rules1 = await self._get_market_rules(ticker, v1)
        rules2 = await self._get_market_rules(ticker, v2)

        min_qty_1 = float(rules1.get("min_qty") or 0.0)
        min_qty_2 = float(rules2.get("min_qty") or 0.0)
        min_notional_1 = float(rules1.get("min_notional") or 0.0)
        min_notional_2 = float(rules2.get("min_notional") or 0.0)
        step_1 = float(rules1.get("step_size") or min_qty_1 or 0.0)
        step_2 = float(rules2.get("step_size") or min_qty_2 or 0.0)

        target_notional = max(0.0, float(getattr(Config, "TARGET_NOTIONAL_PER_LEG_USD", 40.0)))
        max_notional = max(0.0, float(getattr(Config, "MAX_NOTIONAL_PER_LEG_USD", 50.0)))
        if max_notional > 0:
            target_notional = min(target_notional, max_notional)

        qty_target_1 = target_notional / p1
        qty_target_2 = target_notional / p2
        qty_target = min(qty_target_1, qty_target_2)

        qty_by_notional_1 = min_notional_1 / p1 if p1 > 0 else 0.0
        qty_by_notional_2 = min_notional_2 / p2 if p2 > 0 else 0.0

        global_min = max(min_qty_1, min_qty_2, qty_by_notional_1, qty_by_notional_2)
        step_common = self._common_step(step_1, step_2)

        qty = max(qty_target, global_min)
        qty = self._quantize_down(qty, step_common)
        if qty < global_min:
            qty += step_common if step_common > 0 else 0.0

        max_qty_1 = rules1.get("max_qty")
        max_qty_2 = rules2.get("max_qty")
        cap = None
        for value in (max_qty_1, max_qty_2):
            if value is None:
                continue
            try:
                v = float(value)
            except (TypeError, ValueError):
                continue
            if v > 0:
                cap = v if cap is None else min(cap, v)
        if cap is not None:
            qty = min(qty, cap)
            qty = self._quantize_down(qty, step_common)

        max_qty_by_notional = None
        if max_notional > 0:
            max_qty_by_notional = min(max_notional / p1, max_notional / p2)
            qty = min(qty, max_qty_by_notional)
            qty = self._quantize_down(qty, step_common)

        qty = await self._adjust_qty_to_rules(ticker, v1, qty)
        qty = await self._adjust_qty_to_rules(ticker, v2, qty)
        if qty <= 0:
            return 0.0

        # Final notional checks
        if p1 * qty < min_notional_1 or p2 * qty < min_notional_2:
            return 0.0
        if max_notional > 0 and (p1 * qty > max_notional or p2 * qty > max_notional):
            return 0.0
        return qty

    async def _fetch_last_price(self, venue: str, ticker: str) -> float:
        symbol = self._symbol_for_venue(ticker, venue)
        if not symbol:
            return 0.0
        ex = self.exchanges.get(venue)
        if not ex:
            return 0.0
        try:
            t = await ex.fetch_ticker(symbol)
            return float(getattr(t, "last", 0.0) or 0.0)
        except Exception:
            return 0.0

    async def _fetch_signed_position_qty(self, venue: str, ticker: str) -> float:
        ex = self.exchanges.get(venue)
        if not ex:
            return 0.0

        try:
            positions: List[Position] = await ex.fetch_positions()
        except Exception:
            return 0.0

        total = 0.0
        for p in positions:
            if self._base_symbol_from_any(p.symbol) != ticker:
                continue
            amt = float(getattr(p, "amount", 0.0) or 0.0)
            if getattr(p, "side", None) == OrderSide.SELL:
                total -= abs(amt)
            else:
                total += abs(amt)
        return total

    async def _set_leverage_safe(self, venue: str, symbol: str, leverage: int, ticker: str = "") -> None:
        ex = self.exchanges.get(venue)
        if not ex:
            return
        try:
            ok = await ex.set_leverage(symbol, leverage)
            if venue == "HYNA":
                # Some Hyena SDK paths expect un-prefixed symbols for leverage updates.
                base_sym = ticker.upper() if ticker else self._base_symbol_from_any(symbol)
                if ":" in symbol and base_sym and base_sym != symbol:
                    ok2 = await ex.set_leverage(base_sym, leverage)
                    ok = bool(ok) or bool(ok2)
            if not ok:
                logger.warning("set_leverage returned False %s %s x%s", venue, symbol, leverage)
        except Exception as e:
            logger.warning("set_leverage failed %s %s x%s: %s", venue, symbol, leverage, e)

    async def _fetch_position_leverage(self, venue: str, ticker: str) -> Optional[float]:
        ex = self.exchanges.get(venue)
        if not ex:
            return None
        try:
            positions: List[Position] = await ex.fetch_positions()
        except Exception:
            return None
        for p in positions:
            if self._base_symbol_from_any(getattr(p, "symbol", "")) != ticker.upper():
                continue
            try:
                lev = float(getattr(p, "leverage", 0.0) or 0.0)
            except Exception:
                lev = 0.0
            if lev > 0:
                return lev
        return None

    async def _verify_position_leverage(self, ticker: str, venue: str, target_lev: int) -> bool:
        retries = max(1, int(getattr(Config, "LEVERAGE_VERIFY_RETRIES", 4)))
        wait_s = max(0.1, float(getattr(Config, "LEVERAGE_VERIFY_INTERVAL_S", 0.6)))
        tolerance = max(0.0, float(getattr(Config, "LEVERAGE_TOLERANCE", 0.2)))

        symbol = self._symbol_for_venue(ticker, venue)

        for i in range(retries):
            actual = await self._fetch_position_leverage(venue, ticker)
            if actual is not None and actual > 0:
                if actual <= float(target_lev) + tolerance:
                    logger.info(
                        "leverage verified %s %s target=%s actual=%.2f",
                        ticker,
                        venue,
                        target_lev,
                        actual,
                    )
                    return True

                logger.warning(
                    "leverage mismatch %s %s target=%s actual=%.2f attempt=%s/%s",
                    ticker,
                    venue,
                    target_lev,
                    actual,
                    i + 1,
                    retries,
                )
            else:
                logger.info(
                    "leverage pending %s %s attempt=%s/%s",
                    ticker,
                    venue,
                    i + 1,
                    retries,
                )

            if symbol:
                await self._set_leverage_safe(venue, symbol, int(target_lev), ticker=ticker)
            await asyncio.sleep(wait_s)

        final_actual = await self._fetch_position_leverage(venue, ticker)
        if final_actual is not None and final_actual <= float(target_lev) + tolerance:
            logger.info(
                "leverage verified-after-retry %s %s target=%s actual=%.2f",
                ticker,
                venue,
                target_lev,
                final_actual,
            )
            return True

        logger.error(
            "leverage verify failed %s %s target=%s actual=%s",
            ticker,
            venue,
            target_lev,
            "n/a" if final_actual is None else f"{final_actual:.2f}",
        )
        return False

    async def _refresh_balances_cache(self) -> None:
        for venue, ex in self.exchanges.items():
            try:
                raw = await ex.fetch_balance()
                total, available = self._extract_balance_total_available(raw)
                self.balance_cache[venue] = {
                    "total": total,
                    "available": available,
                }
            except Exception:
                # Keep last cache on failure.
                continue

    @staticmethod
    def _extract_balance_total_available(raw) -> Tuple[float, float]:
        if isinstance(raw, Balance):
            return float(raw.total or 0.0), float(raw.free or 0.0)
        if isinstance(raw, dict):
            if "USDT" in raw and isinstance(raw["USDT"], Balance):
                b = raw["USDT"]
                return float(b.total or 0.0), float(b.free or 0.0)
            if "USDC" in raw and isinstance(raw["USDC"], Balance):
                b = raw["USDC"]
                return float(b.total or 0.0), float(b.free or 0.0)
            if "USDe" in raw and isinstance(raw["USDe"], Balance):
                b = raw["USDe"]
                return float(b.total or 0.0), float(b.free or 0.0)
            # generic fallback: first Balance object
            for v in raw.values():
                if isinstance(v, Balance):
                    return float(v.total or 0.0), float(v.free or 0.0)
        return 0.0, 0.0

    async def _has_sufficient_margin(
        self,
        long_venue: str,
        short_venue: str,
        p_long: float,
        p_short: float,
        qty: float,
        lev: int,
    ) -> bool:
        await self._refresh_balances_cache()
        lev_f = max(float(lev), 1.0)
        req_long = (p_long * qty) / lev_f
        req_short = (p_short * qty) / lev_f

        avail_long = float(self.balance_cache.get(long_venue, {}).get("available", 0.0) or 0.0)
        avail_short = float(self.balance_cache.get(short_venue, {}).get("available", 0.0) or 0.0)
        return avail_long >= req_long and avail_short >= req_short

    def _effective_leverage_for_pair(self, ticker: str, v1: str, v2: str) -> int:
        target = max(1, int(getattr(Config, "TARGET_LEVERAGE", 1)))
        max1 = self._get_max_leverage(ticker, v1)
        max2 = self._get_max_leverage(ticker, v2)
        lev = min(target, int(math.floor(max1)), int(math.floor(max2)))
        return max(1, lev)

    def _get_max_leverage(self, ticker: str, venue: str) -> float:
        symbol = self._symbol_for_venue(ticker, venue)
        if not symbol:
            return float(getattr(Config, "TARGET_LEVERAGE", 1))

        ex = self.exchanges.get(venue)
        if not ex:
            return float(getattr(Config, "TARGET_LEVERAGE", 1))

        market = getattr(ex, "markets", {}).get(symbol)
        max_lev = getattr(market, "max_leverage", None) if market else None
        try:
            if max_lev and float(max_lev) > 0:
                return float(max_lev)
        except (TypeError, ValueError):
            pass

        return float(getattr(Config, "TARGET_LEVERAGE", 1))

    async def _get_market_rules(self, ticker: str, venue: str) -> Dict:
        symbol = self._symbol_for_venue(ticker, venue)
        ex = self.exchanges.get(venue)
        if not ex or not symbol:
            return {}

        market = getattr(ex, "markets", {}).get(symbol)
        min_qty = float(getattr(market, "min_qty", 0.0) or 0.0) if market else 0.0
        step_size = float(getattr(market, "qty_step", 0.0) or 0.0) if market else 0.0
        min_notional = float(getattr(market, "min_notional", 0.0) or 0.0) if market else 0.0
        max_qty = getattr(market, "max_qty", None) if market else None

        if (not market or min_qty <= 0 or step_size <= 0) and hasattr(ex, "refresh_market_info"):
            try:
                refreshed = await ex.refresh_market_info(ticker)
                if refreshed:
                    min_qty = float(getattr(refreshed, "min_qty", min_qty) or min_qty)
                    step_size = float(getattr(refreshed, "qty_step", step_size) or step_size)
                    min_notional = float(getattr(refreshed, "min_notional", min_notional) or min_notional)
                    max_qty = getattr(refreshed, "max_qty", max_qty)
            except Exception:
                pass

        return {
            "min_qty": min_qty,
            "step_size": step_size,
            "min_notional": min_notional,
            "max_qty": max_qty,
        }

    async def _adjust_qty_to_rules(
        self,
        ticker: str,
        venue: str,
        qty: float,
        allow_below_min_qty: bool = False,
    ) -> float:
        if qty <= 0:
            return 0.0

        rules = await self._get_market_rules(ticker, venue)
        min_qty = float(rules.get("min_qty") or 0.0)
        step = float(rules.get("step_size") or 0.0)
        max_qty = rules.get("max_qty")

        if allow_below_min_qty:
            qty = self._quantize_up(qty, step)
        else:
            qty = self._quantize_down(qty, step)

        if max_qty is not None and not allow_below_min_qty:
            try:
                mq = float(max_qty)
                if mq > 0:
                    qty = min(qty, mq)
                    qty = self._quantize_down(qty, step)
            except (TypeError, ValueError):
                pass

        if not allow_below_min_qty and min_qty > 0 and qty < min_qty:
            return 0.0

        return max(0.0, qty)

    def _available_pairs_for_ticker(self, ticker: str) -> List[Tuple[str, str]]:
        result: List[Tuple[str, str]] = []
        for v1, v2 in PAIR_CHOICES:
            if v1 not in self.exchanges or v2 not in self.exchanges:
                continue
            if self._symbol_for_venue(ticker, v1) and self._symbol_for_venue(ticker, v2):
                result.append((v1, v2))
        return result

    def _symbol_for_venue(self, ticker: str, venue: str) -> str:
        t = ticker.upper()
        if venue == "GRVT":
            sym = f"{t}_USDT_Perp"
            return sym if sym in self.grvt.markets else ""
        if venue == "HYNA":
            pref = f"{Config.HYENA_DEX_ID}:{t}"
            if pref in self.hyena.markets:
                return pref
            if t in self.hyena.markets:
                return t
            return ""
        if venue == "VAR" and self.variational:
            return t if t in self.variational.markets else ""
        return ""

    @staticmethod
    def _base_symbol_from_any(symbol: str) -> str:
        s = str(symbol or "").upper()
        if ":" in s:
            s = s.split(":", 1)[1]
        for sep in ("_", "-", "/"):
            if sep in s:
                return s.split(sep, 1)[0]
        return s

    @staticmethod
    def _quantize_down(qty: float, step: float) -> float:
        if qty <= 0:
            return 0.0
        if step <= 0:
            return qty
        q = Decimal(str(qty))
        s = Decimal(str(step))
        units = (q / s).to_integral_value(rounding=ROUND_FLOOR)
        return float(units * s)

    @staticmethod
    def _quantize_up(qty: float, step: float) -> float:
        if qty <= 0:
            return 0.0
        if step <= 0:
            return qty
        q = Decimal(str(qty))
        s = Decimal(str(step))
        units = (q / s).to_integral_value(rounding=ROUND_CEILING)
        return float(units * s)

    @staticmethod
    def _common_step(step_1: float, step_2: float) -> float:
        s1 = float(step_1 or 0.0)
        s2 = float(step_2 or 0.0)
        if s1 <= 0 and s2 <= 0:
            return 0.0
        if s1 <= 0:
            return s2
        if s2 <= 0:
            return s1
        try:
            d1 = Decimal(str(s1)).normalize()
            d2 = Decimal(str(s2)).normalize()
            scale = max(-d1.as_tuple().exponent, -d2.as_tuple().exponent, 0)
            mul = Decimal(10) ** scale
            n1 = int((d1 * mul).to_integral_value())
            n2 = int((d2 * mul).to_integral_value())
            if n1 <= 0 or n2 <= 0:
                return max(s1, s2)
            lcm_units = math.lcm(abs(n1), abs(n2))
            return float(Decimal(lcm_units) / mul)
        except Exception:
            return max(s1, s2)

    async def _fetch_all_signed_positions(self) -> Dict[str, Dict[str, float]]:
        out: Dict[str, Dict[str, float]] = {v: {} for v in self.exchanges.keys()}
        for venue, ex in self.exchanges.items():
            try:
                positions: List[Position] = await ex.fetch_positions()
            except Exception as e:
                logger.warning("hedge_audit fetch_positions failed %s: %s", venue, e)
                continue
            for p in positions:
                ticker = self._base_symbol_from_any(getattr(p, "symbol", ""))
                if not ticker:
                    continue
                amt = float(getattr(p, "amount", 0.0) or 0.0)
                if amt <= 0:
                    continue
                signed = -abs(amt) if getattr(p, "side", None) == OrderSide.SELL else abs(amt)
                out[venue][ticker] = float(out[venue].get(ticker, 0.0) or 0.0) + signed
        return out

    async def _audit_hedge_integrity(self) -> None:
        if not self.exchanges:
            return

        actual = await self._fetch_all_signed_positions()
        expected: Dict[str, Dict[str, float]] = {v: {} for v in self.exchanges.keys()}
        for a in self.assignments.values():
            expected[a.long_venue][a.ticker] = float(expected[a.long_venue].get(a.ticker, 0.0) or 0.0) + abs(a.qty)
            expected[a.short_venue][a.ticker] = float(expected[a.short_venue].get(a.ticker, 0.0) or 0.0) - abs(a.qty)

        tol = max(0.0, float(getattr(Config, "HEDGE_QTY_TOLERANCE", 0.02)))
        tracked_symbols = set([s.upper() for s in Config.POINT_SYMBOLS]) | set(self.assignments.keys())

        for venue in sorted(self.exchanges.keys()):
            tickers = set(expected.get(venue, {}).keys()) | set(actual.get(venue, {}).keys())
            for ticker in sorted(tickers):
                if ticker.upper() not in tracked_symbols:
                    continue
                exp_qty = float(expected.get(venue, {}).get(ticker, 0.0) or 0.0)
                act_qty = float(actual.get(venue, {}).get(ticker, 0.0) or 0.0)
                delta = act_qty - exp_qty
                recoverable_qty = await self._min_recoverable_qty(ticker, venue)
                effective_tol = max(tol, recoverable_qty)
                if abs(delta) <= effective_tol:
                    continue
                logger.warning(
                    "hedge_audit mismatch %s %s expected=%.8f actual=%.8f delta=%.8f",
                    ticker,
                    venue,
                    exp_qty,
                    act_qty,
                    delta,
                )
                ok = await self._recover_ticker_venue_mismatch(ticker, venue, exp_qty, act_qty)
                if ok and ticker in self.assignments:
                    await self._sync_assignment_qty_from_live(ticker)

    async def _recover_ticker_venue_mismatch(self, ticker: str, venue: str, expected_qty: float, actual_qty: float) -> bool:
        symbol = self._symbol_for_venue(ticker, venue)
        ex = self.exchanges.get(venue)
        if not ex or not symbol:
            return False

        needed = expected_qty - actual_qty
        tol = max(0.0, float(getattr(Config, "HEDGE_QTY_TOLERANCE", 0.02)))
        recoverable_qty = await self._min_recoverable_qty(ticker, venue)
        effective_tol = max(tol, recoverable_qty)
        if abs(needed) <= effective_tol:
            return True

        if Config.DRY_RUN:
            logger.info(
                "[DRY_RUN] hedge_audit recover %s %s needed=%.8f expected=%.8f actual=%.8f",
                ticker,
                venue,
                needed,
                expected_qty,
                actual_qty,
            )
            return True

        # If assignment expects flat on this venue, close residual as reduce-only.
        if abs(expected_qty) <= tol:
            return await self._close_venue_position(ticker, venue, reason="hedge_audit_orphan")

        side = OrderSide.BUY if needed > 0 else OrderSide.SELL
        qty = await self._adjust_qty_to_rules(
            ticker,
            venue,
            abs(needed),
            allow_below_min_qty=True,
        )
        if qty <= 0:
            logger.info(
                "hedge_audit recover non-actionable %s %s: needed=%.8f min_recoverable=%.8f",
                ticker,
                venue,
                needed,
                recoverable_qty,
            )
            return False

        try:
            assignment = self.assignments.get(ticker)
            lev = int(getattr(assignment, "leverage", int(getattr(Config, "TARGET_LEVERAGE", 1))))
            await self._set_leverage_safe(venue, symbol, lev, ticker=ticker)
            order = await ex.create_order(symbol, OrderType.MARKET, side, qty)
            logger.warning(
                "hedge_audit recover %s %s side=%s qty=%.8f expected=%.8f actual=%.8f",
                ticker,
                venue,
                side.value,
                qty,
                expected_qty,
                actual_qty,
            )
            self._log_trade(
                action="recover",
                ticker=ticker,
                exchange=venue,
                side=side.value,
                qty=qty,
                leverage=lev,
                reason="hedge_audit_recover",
                order_id=getattr(order, "id", ""),
                status=getattr(getattr(order, "status", None), "value", ""),
                dry_run=False,
            )
        except Exception as e:
            logger.error("hedge_audit recover failed %s %s: %s", ticker, venue, e)
            return False

        await asyncio.sleep(0.25)
        rem = await self._fetch_signed_position_qty(venue, ticker)
        return abs(rem - expected_qty) <= max(effective_tol, 0.05)

    async def _sync_assignment_qty_from_live(self, ticker: str) -> None:
        assignment = self.assignments.get(ticker)
        if not assignment:
            return
        q_long = abs(await self._fetch_signed_position_qty(assignment.long_venue, ticker))
        q_short = abs(await self._fetch_signed_position_qty(assignment.short_venue, ticker))
        live = min(q_long, q_short)
        if live > 0:
            assignment.qty = live

    async def _normalize_pair_after_open(self, ticker: str, long_venue: str, short_venue: str) -> None:
        tol = max(0.0, float(getattr(Config, "HEDGE_QTY_TOLERANCE", 0.02)))
        q_long_signed = await self._fetch_signed_position_qty(long_venue, ticker)
        q_short_signed = await self._fetch_signed_position_qty(short_venue, ticker)
        q_long = abs(q_long_signed)
        q_short = abs(q_short_signed)
        if q_long <= 0 or q_short <= 0:
            return
        delta_abs = abs(q_long - q_short)
        if delta_abs <= tol:
            return

        common = min(q_long, q_short)
        heavy_venue = long_venue if q_long > q_short else short_venue
        min_recoverable = await self._min_recoverable_qty(ticker, heavy_venue)
        if delta_abs < min_recoverable:
            logger.info(
                "open_fill_reconcile non-actionable %s delta=%.8f heavy=%s min_recoverable=%.8f",
                ticker,
                delta_abs,
                heavy_venue,
                min_recoverable,
            )
            return

        ok = await self._trim_venue_to_target_abs(ticker, heavy_venue, common, reason="open_fill_reconcile")
        logger.warning(
            "open_fill_reconcile %s long=%s(%.8f) short=%s(%.8f) target=%.8f heavy=%s ok=%s",
            ticker,
            long_venue,
            q_long,
            short_venue,
            q_short,
            common,
            heavy_venue,
            ok,
        )

    async def _trim_venue_to_target_abs(self, ticker: str, venue: str, target_abs: float, reason: str) -> bool:
        ex = self.exchanges.get(venue)
        symbol = self._symbol_for_venue(ticker, venue)
        if not ex or not symbol:
            return False

        signed = await self._fetch_signed_position_qty(venue, ticker)
        cur_abs = abs(signed)
        delta = cur_abs - max(target_abs, 0.0)
        tol = max(0.0, float(getattr(Config, "HEDGE_QTY_TOLERANCE", 0.02)))
        if delta <= tol:
            return True

        rules = await self._get_market_rules(ticker, venue)
        step = float(rules.get("step_size") or 0.0)
        side = OrderSide.SELL if signed > 0 else OrderSide.BUY
        qty = await self._adjust_qty_to_rules(ticker, venue, delta, allow_below_min_qty=True)
        # Never over-trim past the target if venue step forces coarse quantization.
        if qty > delta + tol:
            qty = self._quantize_down(delta, step)
        if qty <= 0:
            logger.info(
                "trim non-actionable %s %s delta=%.8f step=%.8f",
                ticker,
                venue,
                delta,
                step,
            )
            return False

        if Config.DRY_RUN:
            logger.info("[DRY_RUN] trim %s %s side=%s qty=%.8f", ticker, venue, side.value, qty)
            return True

        try:
            order = await ex.create_order(symbol, OrderType.MARKET, side, qty, params={"reduce_only": True})
            self._log_trade(
                action="trim",
                ticker=ticker,
                exchange=venue,
                side=side.value,
                qty=qty,
                leverage="",
                reason=reason,
                order_id=getattr(order, "id", ""),
                status=getattr(getattr(order, "status", None), "value", ""),
                dry_run=False,
            )
            await asyncio.sleep(0.2)
            new_abs = abs(await self._fetch_signed_position_qty(venue, ticker))
            return new_abs <= target_abs + max(tol, 0.05)
        except Exception as e:
            logger.error("trim failed %s %s: %s", ticker, venue, e)
            return False

    async def _min_recoverable_qty(self, ticker: str, venue: str) -> float:
        rules = await self._get_market_rules(ticker, venue)
        min_qty = float(rules.get("min_qty") or 0.0)
        step = float(rules.get("step_size") or 0.0)
        min_notional = float(rules.get("min_notional") or 0.0)
        price = await self._fetch_last_price(venue, ticker)
        min_by_notional = (min_notional / price) if (min_notional > 0 and price > 0) else 0.0
        return max(min_qty, step, min_by_notional, 0.0)

    @staticmethod
    def _random_rotation_interval_s() -> float:
        min_h = float(Config.ROTATION_MIN_HOURS)
        max_h = float(Config.ROTATION_MAX_HOURS)
        if max_h < min_h:
            min_h, max_h = max_h, min_h
        return random.uniform(min_h, max_h) * 3600.0

    @staticmethod
    def _pair_key(v1: str, v2: str) -> str:
        return "|".join(sorted([v1, v2]))

    def _get_locked_long_venue(self, ticker: str, v1: str, v2: str) -> Optional[str]:
        return self.direction_lock.get((ticker, self._pair_key(v1, v2)))

    def _set_locked_long_venue(self, ticker: str, v1: str, v2: str, long_venue: str) -> None:
        self.direction_lock[(ticker, self._pair_key(v1, v2))] = long_venue

    async def _ensure_direction_lock_from_assignments(self) -> None:
        for assignment in self.assignments.values():
            self._set_locked_long_venue(
                assignment.ticker,
                assignment.long_venue,
                assignment.short_venue,
                assignment.long_venue,
            )

    def _log_trade(
        self,
        action: str,
        ticker: str,
        exchange: str,
        side: str,
        qty: float,
        leverage,
        reason: str,
        order_id: str,
        status: str,
        dry_run: bool,
    ) -> None:
        if not self.session_logger:
            return
        self.session_logger.log_trade(
            {
                "cycle_id": self._active_cycle_id,
                "action": action,
                "ticker": ticker,
                "exchange": exchange,
                "side": side,
                "qty": qty,
                "price": "",
                "notional": "",
                "leverage": leverage,
                "reason": reason,
                "order_id": order_id,
                "status": status,
                "dry_run": int(bool(dry_run)),
            }
        )

    def _log_cycle_pair(
        self,
        ticker: str,
        long_venue: str,
        short_venue: str,
        qty: float,
        leverage: int,
        state: str,
        reason: str,
    ) -> None:
        if not self.session_logger:
            return
        self.session_logger.log_cycle_pair(
            {
                "cycle_id": self._active_cycle_id,
                "ticker": ticker,
                "long_venue": long_venue,
                "short_venue": short_venue,
                "qty": qty,
                "leverage": leverage,
                "state": state,
                "reason": reason,
            }
        )

    def _log_cycle_exchange_summary(self) -> None:
        if not self.session_logger:
            return
        leg_counts = self._exchange_leg_counts()
        for venue in sorted(self.exchanges.keys()):
            pair_count = 0
            for a in self.assignments.values():
                if venue in (a.long_venue, a.short_venue):
                    pair_count += 1
            self.session_logger.log_cycle_exchange(
                {
                    "cycle_id": self._active_cycle_id,
                    "exchange": venue,
                    "pair_count": pair_count,
                    "leg_count": leg_counts.get(venue, 0),
                }
            )


async def build_and_run(session_logger: Optional[PointSessionLogger] = None, dashboard_enabled: bool = False) -> None:
    bot = PointFarmingBot(session_logger=session_logger, dashboard=None)
    if dashboard_enabled:
        bot.dashboard = Dashboard(bot)
    await bot.initialize()
    if not bot.paused:
        bot.next_rotation_ts = time.time()
    await bot.run()

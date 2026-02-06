import asyncio
import logging
import math
import random
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from shared_crypto_lib.factory import ExchangeFactory, ExchangeType
from shared_crypto_lib.models import MarketInfo, OrderSide, OrderType, Position

from src.config import Config

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
    def __init__(self):
        if Config.RANDOM_SEED:
            random.seed(Config.RANDOM_SEED)

        self.grvt = None
        self.hyena = None
        self.variational = None
        self.exchanges: Dict[str, object] = {}
        self.assignments: Dict[str, Assignment] = {}
        self.next_rotation_ts: float = 0.0
        self.paused = bool(Config.TRADING_START_PAUSED)

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
                if not self.paused and time.time() >= self.next_rotation_ts:
                    await self.rotate_cycle()
            except Exception as e:
                logger.exception("loop error: %s", e)
            await asyncio.sleep(max(1.0, float(Config.LOOP_INTERVAL_S)))

    async def rotate_cycle(self) -> None:
        logger.info("cycle started")
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

        await self._open_new_assignments_to_target()

        self.next_rotation_ts = time.time() + self._random_rotation_interval_s()
        logger.info(
            "cycle finished closed=%s kept=%s active=%s next=%.0f",
            closed,
            kept,
            len(self.assignments),
            self.next_rotation_ts,
        )

    async def _open_new_assignments_to_target(self) -> None:
        max_active = max(0, int(Config.TARGET_ACTIVE_TICKERS))
        if max_active <= 0:
            return

        active_tickers = set(self.assignments.keys())
        candidates = [t for t in Config.POINT_SYMBOLS if t not in active_tickers]
        random.shuffle(candidates)

        for ticker in candidates:
            if len(self.assignments) >= max_active:
                break
            opened = await self._open_assignment_for_ticker(ticker)
            if opened:
                active_tickers.add(ticker)

    async def _open_assignment_for_ticker(self, ticker: str) -> bool:
        choices = self._available_pairs_for_ticker(ticker)
        random.shuffle(choices)

        for v1, v2 in choices:
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
                continue

            qty = await self._compute_common_qty(ticker, long_venue, short_venue, lev)
            if qty <= 0:
                continue

            ok = await self._open_pair(ticker, long_venue, short_venue, qty, lev)
            if not ok:
                continue

            self.assignments[ticker] = Assignment(
                ticker=ticker,
                long_venue=long_venue,
                short_venue=short_venue,
                qty=qty,
                leverage=lev,
                opened_ts=time.time(),
            )
            self._set_locked_long_venue(ticker, long_venue, short_venue, long_venue)
            return True

        logger.warning("%s skipped: no valid pair/qty available", ticker)
        return False

    async def _close_assignment(self, assignment: Assignment, reason: str) -> bool:
        ticker = assignment.ticker
        ok_long = await self._close_venue_position(ticker, assignment.long_venue)
        ok_short = await self._close_venue_position(ticker, assignment.short_venue)
        logger.info(
            "close assignment %s pair=%s/%s reason=%s result=%s/%s",
            ticker,
            assignment.long_venue,
            assignment.short_venue,
            reason,
            ok_long,
            ok_short,
        )
        return ok_long and ok_short

    async def _close_venue_position(self, ticker: str, venue: str) -> bool:
        signed_qty = await self._fetch_signed_position_qty(venue, ticker)
        if abs(signed_qty) <= 0:
            return True

        side = OrderSide.SELL if signed_qty > 0 else OrderSide.BUY
        symbol = self._symbol_for_venue(ticker, venue)
        if not symbol:
            return False

        qty = await self._adjust_qty_to_rules(ticker, venue, abs(signed_qty), allow_below_min_qty=True)
        if qty <= 0:
            return False

        if Config.DRY_RUN:
            logger.info("[DRY_RUN] close %s %s %s qty=%s", ticker, venue, side.value, qty)
            return True

        ex = self.exchanges.get(venue)
        if not ex:
            return False

        try:
            await ex.create_order(symbol, OrderType.MARKET, side, qty, params={"reduce_only": True})
            logger.info("closed %s %s %s qty=%s", ticker, venue, side.value, qty)
            return True
        except Exception as e:
            logger.error("close failed %s %s: %s", ticker, venue, e)
            return False

    async def _open_pair(self, ticker: str, long_venue: str, short_venue: str, qty: float, lev: int) -> bool:
        long_symbol = self._symbol_for_venue(ticker, long_venue)
        short_symbol = self._symbol_for_venue(ticker, short_venue)
        if not long_symbol or not short_symbol:
            return False

        long_ex = self.exchanges[long_venue]
        short_ex = self.exchanges[short_venue]

        await self._set_leverage_safe(long_venue, long_symbol, lev)
        await self._set_leverage_safe(short_venue, short_symbol, lev)

        if Config.DRY_RUN:
            logger.info(
                "[DRY_RUN] open %s long=%s short=%s qty=%.8f lev=x%s",
                ticker,
                long_venue,
                short_venue,
                qty,
                lev,
            )
            return True

        long_order = None
        try:
            long_order = await long_ex.create_order(long_symbol, OrderType.MARKET, OrderSide.BUY, qty)
            await short_ex.create_order(short_symbol, OrderType.MARKET, OrderSide.SELL, qty)
            logger.info(
                "opened %s long=%s short=%s qty=%.8f lev=x%s",
                ticker,
                long_venue,
                short_venue,
                qty,
                lev,
            )
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

        margin = max(0.0, float(Config.MARGIN_PER_LEG_USD))
        notional_per_leg = margin * max(1, int(lev))

        qty_target_1 = notional_per_leg / p1
        qty_target_2 = notional_per_leg / p2
        qty_target = min(qty_target_1, qty_target_2)

        qty_by_notional_1 = min_notional_1 / p1 if p1 > 0 else 0.0
        qty_by_notional_2 = min_notional_2 / p2 if p2 > 0 else 0.0

        global_min = max(min_qty_1, min_qty_2, qty_by_notional_1, qty_by_notional_2)
        step_common = max(step_1, step_2, 0.0)

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

        qty = await self._adjust_qty_to_rules(ticker, v1, qty)
        qty = await self._adjust_qty_to_rules(ticker, v2, qty)
        if qty <= 0:
            return 0.0

        # Final notional check
        if p1 * qty < min_notional_1 or p2 * qty < min_notional_2:
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

    async def _set_leverage_safe(self, venue: str, symbol: str, leverage: int) -> None:
        ex = self.exchanges.get(venue)
        if not ex:
            return
        try:
            await ex.set_leverage(symbol, leverage)
        except Exception as e:
            logger.warning("set_leverage failed %s %s x%s: %s", venue, symbol, leverage, e)

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

        qty = self._quantize_down(qty, step)

        if max_qty is not None:
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
        inv = 1.0 / step
        return math.floor(qty * inv) / inv

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


async def build_and_run() -> None:
    bot = PointFarmingBot()
    await bot.initialize()
    if not bot.paused:
        bot.next_rotation_ts = time.time()
    await bot.run()

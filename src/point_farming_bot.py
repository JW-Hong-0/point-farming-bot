import asyncio
import logging
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
        # pair_key example: "GRVT|HYNA"
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
                "keep %s pair=%s/%s age=%.2fh",
                assignment.ticker,
                assignment.long_venue,
                assignment.short_venue,
                age_h,
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

            qty = await self._compute_common_qty(ticker, long_venue, short_venue)
            if qty <= 0:
                continue

            ok = await self._open_pair(ticker, long_venue, short_venue, qty)
            if not ok:
                continue

            self.assignments[ticker] = Assignment(
                ticker=ticker,
                long_venue=long_venue,
                short_venue=short_venue,
                qty=qty,
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

        qty = await self._adjust_qty_to_rules(ticker, venue, abs(signed_qty))
        if qty <= 0:
            return False

        if Config.DRY_RUN:
            logger.info("[DRY_RUN] close %s %s %s qty=%s", ticker, venue, side.value, qty)
            return True

        ex = self.exchanges.get(venue)
        if not ex:
            return False

        try:
            await ex.create_order(symbol, OrderType.MARKET, side, qty)
            logger.info("closed %s %s %s qty=%s", ticker, venue, side.value, qty)
            return True
        except Exception as e:
            logger.error("close failed %s %s: %s", ticker, venue, e)
            return False

    async def _open_pair(self, ticker: str, long_venue: str, short_venue: str, qty: float) -> bool:
        long_symbol = self._symbol_for_venue(ticker, long_venue)
        short_symbol = self._symbol_for_venue(ticker, short_venue)
        if not long_symbol or not short_symbol:
            return False

        long_ex = self.exchanges[long_venue]
        short_ex = self.exchanges[short_venue]

        await self._set_leverage_safe(long_venue, long_symbol, int(Config.TARGET_LEVERAGE))
        await self._set_leverage_safe(short_venue, short_symbol, int(Config.TARGET_LEVERAGE))

        if Config.DRY_RUN:
            logger.info(
                "[DRY_RUN] open %s long=%s short=%s qty=%.8f",
                ticker,
                long_venue,
                short_venue,
                qty,
            )
            return True

        long_order = None
        try:
            long_order = await long_ex.create_order(long_symbol, OrderType.MARKET, OrderSide.BUY, qty)
            await short_ex.create_order(short_symbol, OrderType.MARKET, OrderSide.SELL, qty)
            logger.info("opened %s long=%s short=%s qty=%.8f", ticker, long_venue, short_venue, qty)
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

    async def _compute_common_qty(self, ticker: str, v1: str, v2: str) -> float:
        p1 = await self._fetch_last_price(v1, ticker)
        p2 = await self._fetch_last_price(v2, ticker)
        if p1 <= 0 or p2 <= 0:
            return 0.0

        notional_per_leg = float(Config.MARGIN_PER_LEG_USD) * max(1.0, float(Config.TARGET_LEVERAGE))
        raw_qty = min(notional_per_leg / p1, notional_per_leg / p2)

        q1 = await self._adjust_qty_to_rules(ticker, v1, raw_qty)
        q2 = await self._adjust_qty_to_rules(ticker, v2, raw_qty)
        qty = min(q1, q2)

        if qty <= 0:
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
        symbol = self._symbol_for_venue(ticker, venue)
        if not symbol:
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

    async def _adjust_qty_to_rules(self, ticker: str, venue: str, qty: float) -> float:
        if qty <= 0:
            return 0.0

        symbol = self._symbol_for_venue(ticker, venue)
        if not symbol:
            return 0.0

        market: Optional[MarketInfo] = self.exchanges[venue].markets.get(symbol)
        if not market:
            return 0.0

        step = float(getattr(market, "qty_step", 0.0) or 0.0)
        min_qty = float(getattr(market, "min_qty", 0.0) or 0.0)

        if step > 0:
            inv = 1.0 / step
            qty = int(qty * inv) / inv

        if min_qty > 0 and qty < min_qty:
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

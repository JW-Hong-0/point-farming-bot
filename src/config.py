import os
from pathlib import Path


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ[key] = val


def _to_bool(v: str, default: bool = False) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _to_float(v: str, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _to_int(v: str, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


env_override = os.getenv("BOT_ENV_PATH")
if env_override:
    _load_env_file(Path(env_override))
else:
    here = Path(__file__).resolve()
    bot_root = here.parent.parent
    perp_root = bot_root.parent.parent
    candidates = [
        bot_root / "secret" / "Funding_Arbitrage.env",
        perp_root / "private" / "Funding_Arbitrage.env",
        bot_root / "private" / "Funding_Arbitrage.env",
    ]
    for candidate in candidates:
        if candidate.exists():
            _load_env_file(candidate)
            os.environ.setdefault("BOT_ENV_PATH", str(candidate))
            break


class Config:
    GRVT_ENV = os.getenv("GRVT_ENV", "PROD")
    HYENA_DEX_ID = os.getenv("HYENA_DEX_ID", "hyna")

    if GRVT_ENV == "TESTNET":
        GRVT_API_KEY = os.getenv("GRVT_TESTNET_API_KEY")
        GRVT_PRIVATE_KEY = os.getenv("GRVT_TESTNET_SECRET_KEY")
        GRVT_TRADING_ACCOUNT_ID = os.getenv("GRVT_TESTNET_TRADING_ACCOUNT_ID")
    else:
        GRVT_API_KEY = os.getenv("GRVT_MAINNET_API_KEY")
        GRVT_PRIVATE_KEY = os.getenv("GRVT_MAINNET_SECRET_KEY")
        GRVT_TRADING_ACCOUNT_ID = os.getenv("GRVT_MAINNET_TRADING_ACCOUNT_ID")

    HYENA_PRIVATE_KEY = os.getenv("HYENA_PRIVATE_KEY") or os.getenv("HYPERLIQUID_PRIVATE_KEY")
    HYENA_WALLET_ADDRESS = os.getenv("HYENA_WALLET_ADDRESS") or os.getenv("HYPERLIQUID_API_WALLET_ADDRESS")
    HYENA_MAIN_ADDRESS = os.getenv("HYENA_MAIN_ADDRESS") or os.getenv("HYPERLIQUID_MAIN_ADDRESS")
    HYENA_USE_SYMBOL_PREFIX = _to_bool(os.getenv("HYENA_USE_SYMBOL_PREFIX"), True)
    HYENA_BUILDER_ADDRESS = os.getenv(
        "HYENA_BUILDER_ADDRESS",
        "0x1924b8561eeF20e70Ede628A296175D358BE80e5",
    )
    HYENA_BUILDER_FEE = _to_int(os.getenv("HYENA_BUILDER_FEE"), 0)
    HYENA_APPROVE_BUILDER_FEE = _to_bool(os.getenv("HYENA_APPROVE_BUILDER_FEE"), False)
    HYENA_BUILDER_MAX_FEE_RATE = os.getenv("HYENA_BUILDER_MAX_FEE_RATE", "0")
    HYENA_SLIPPAGE = _to_float(os.getenv("HYENA_SLIPPAGE"), 0.05)
    HYENA_MIN_NOTIONAL = _to_float(os.getenv("HYENA_MIN_NOTIONAL"), 10.0)

    VARIATIONAL_WALLET_ADDRESS = os.getenv("VARIATIONAL_WALLET_ADDRESS")
    VARIATIONAL_VR_TOKEN = os.getenv("VARIATIONAL_JWT_TOKEN")
    VARIATIONAL_PRIVATE_KEY = os.getenv("VARIATIONAL_PRIVATE_KEY")
    VARIATIONAL_SLIPPAGE = _to_float(os.getenv("VARIATIONAL_SLIPPAGE"), 0.01)
    ENABLE_VAR = _to_bool(os.getenv("POINT_ENABLE_VAR"), True)

    POINT_SYMBOLS = [
        s.strip().upper()
        for s in os.getenv("POINT_SYMBOLS", "ENA,FARTCOIN,HYPE,LINK,LTC,SUI,XPL,ZEC,XMR,IP").split(",")
        if s.strip()
    ]
    ENFORCE_ALL_PAIR_TYPES = _to_bool(os.getenv("POINT_ENFORCE_ALL_PAIR_TYPES"), True)
    EXCHANGE_MIN_LEGS = _to_int(os.getenv("POINT_EXCHANGE_MIN_LEGS"), 2)
    TARGET_ACTIVE_TICKERS = _to_int(os.getenv("POINT_TARGET_ACTIVE_TICKERS"), 3)
    RETAIN_PROBABILITY = _to_float(os.getenv("POINT_RETAIN_PROBABILITY"), 0.55)
    MAX_HOLD_HOURS = _to_float(os.getenv("POINT_MAX_HOLD_HOURS"), 36.0)
    TARGET_LEVERAGE = _to_int(os.getenv("POINT_TARGET_LEVERAGE"), 5)
    ENFORCE_LEVERAGE_MATCH = _to_bool(os.getenv("POINT_ENFORCE_LEVERAGE_MATCH"), True)
    LEVERAGE_VERIFY_RETRIES = _to_int(os.getenv("POINT_LEVERAGE_VERIFY_RETRIES"), 4)
    LEVERAGE_VERIFY_INTERVAL_S = _to_float(os.getenv("POINT_LEVERAGE_VERIFY_INTERVAL_S"), 0.6)
    LEVERAGE_TOLERANCE = _to_float(os.getenv("POINT_LEVERAGE_TOLERANCE"), 0.2)
    TARGET_NOTIONAL_PER_LEG_USD = _to_float(os.getenv("POINT_TARGET_NOTIONAL_PER_LEG_USD"), 40.0)
    MAX_NOTIONAL_PER_LEG_USD = _to_float(os.getenv("POINT_MAX_NOTIONAL_PER_LEG_USD"), 50.0)
    MARGIN_PER_LEG_USD = _to_float(os.getenv("POINT_MARGIN_PER_LEG_USD"), 20.0)
    ROTATION_MIN_HOURS = _to_float(os.getenv("POINT_ROTATION_MIN_HOURS"), 1.0)
    ROTATION_MAX_HOURS = _to_float(os.getenv("POINT_ROTATION_MAX_HOURS"), 2.0)
    LOOP_INTERVAL_S = _to_float(os.getenv("POINT_LOOP_INTERVAL_S"), 3.0)
    HEDGE_AUDIT_INTERVAL_S = _to_float(os.getenv("POINT_HEDGE_AUDIT_INTERVAL_S"), 15.0)
    HEDGE_QTY_TOLERANCE = _to_float(os.getenv("POINT_HEDGE_QTY_TOLERANCE"), 0.02)
    CLOSE_RETRY_MAX = _to_int(os.getenv("POINT_CLOSE_RETRY_MAX"), 3)
    TRADING_START_PAUSED = _to_bool(os.getenv("TRADING_START_PAUSED"), True)
    DRY_RUN = _to_bool(os.getenv("POINT_DRY_RUN"), True)
    RANDOM_SEED = os.getenv("POINT_RANDOM_SEED")
    DASHBOARD_ENABLED = _to_bool(os.getenv("POINT_DASHBOARD_ENABLED"), True)
    DASHBOARD_REFRESH_S = _to_float(os.getenv("POINT_DASHBOARD_REFRESH_S"), 1.0)
    SESSION_LOG_DIR = os.getenv("POINT_SESSION_LOG_DIR", "")

    LOG_LEVEL = os.getenv("POINT_LOG_LEVEL", "INFO")

    if not GRVT_API_KEY:
        raise ValueError("GRVT API key missing. Set GRVT_MAINNET_API_KEY or GRVT_TESTNET_API_KEY.")
    if not GRVT_PRIVATE_KEY:
        raise ValueError("GRVT private key missing. Set GRVT_MAINNET_SECRET_KEY or GRVT_TESTNET_SECRET_KEY.")
    if not GRVT_TRADING_ACCOUNT_ID:
        raise ValueError("GRVT trading account ID missing.")
    if not HYENA_PRIVATE_KEY:
        raise ValueError("HYENA private key missing. Set HYENA_PRIVATE_KEY or HYPERLIQUID_PRIVATE_KEY.")

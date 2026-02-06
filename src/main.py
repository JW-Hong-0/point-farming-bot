import asyncio
import atexit
import logging
import os
import signal
import sys
import time
from pathlib import Path

current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
bots_root = project_root.parent
perp_root = bots_root.parent

# Prefer local submodule path (for standalone repo), fallback to monorepo shared path.
local_libs_dir = project_root / "libs"
shared_libs_dir = perp_root / "libs"
if local_libs_dir.exists() and str(local_libs_dir) not in sys.path:
    sys.path.append(str(local_libs_dir))
if shared_libs_dir.exists() and str(shared_libs_dir) not in sys.path:
    sys.path.append(str(shared_libs_dir))
if str(project_root) not in sys.path:
    sys.path.append(str(project_root))
if str(current_dir) not in sys.path:
    sys.path.append(str(current_dir))

from src.config import Config
from src.point_farming_bot import build_and_run


logging.basicConfig(
    level=getattr(logging, str(Config.LOG_LEVEL).upper(), logging.INFO),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler(project_root / f"point_farming_bot_{os.getpid()}.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("PointFarmingMain")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _acquire_lock(lock_path: Path) -> None:
    pid = os.getpid()
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(pid).encode("utf-8"))
        os.close(fd)
        logger.info("lock acquired: %s pid=%s", lock_path, pid)
        return
    except FileExistsError:
        existing_pid = None
        try:
            existing_pid = int(lock_path.read_text().strip())
        except Exception:
            pass

        if existing_pid and _pid_alive(existing_pid):
            raise RuntimeError(f"bot already running pid={existing_pid}")

        lock_path.unlink(missing_ok=True)
        _acquire_lock(lock_path)


def _release_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink(missing_ok=True)
    except Exception:
        pass


def _install_lock(lock_path: Path) -> None:
    _acquire_lock(lock_path)
    atexit.register(_release_lock, lock_path)

    def _handle(_sig, _frame):
        _release_lock(lock_path)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)


if __name__ == "__main__":
    try:
        lock_path = project_root / ".bot.lock"
        _install_lock(lock_path)
        logger.info("starting point farming bot")
        logger.info(
            "symbols=%s target_active=%s rotation=%.2f~%.2fh leverage=%s margin_per_leg=%s retain=%.2f max_hold_h=%.1f dry_run=%s paused=%s",
            Config.POINT_SYMBOLS,
            Config.TARGET_ACTIVE_TICKERS,
            Config.ROTATION_MIN_HOURS,
            Config.ROTATION_MAX_HOURS,
            Config.TARGET_LEVERAGE,
            Config.MARGIN_PER_LEG_USD,
            Config.RETAIN_PROBABILITY,
            Config.MAX_HOLD_HOURS,
            Config.DRY_RUN,
            Config.TRADING_START_PAUSED,
        )
        asyncio.run(build_and_run())
    except KeyboardInterrupt:
        logger.info("stopped by user")
    except Exception as e:
        logger.critical("fatal: %s", e)
        time.sleep(0.1)
        raise

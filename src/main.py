import asyncio
import atexit
import logging
import os
import signal
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
bots_root = project_root.parent
perp_root = bots_root.parent

local_libs_dir = project_root / "libs"
shared_libs_dir = perp_root / "libs"

def _has_hyena_factory(lib_root: Path) -> bool:
    factory_path = lib_root / "shared_crypto_lib" / "factory.py"
    if not factory_path.exists():
        return False
    try:
        text = factory_path.read_text(encoding="utf-8")
    except Exception:
        return False
    return "HYENA" in text

# Prefer a library root that has HYENA support.
preferred_libs = []
if shared_libs_dir.exists() and _has_hyena_factory(shared_libs_dir):
    preferred_libs.append(shared_libs_dir)
if local_libs_dir.exists() and _has_hyena_factory(local_libs_dir):
    preferred_libs.append(local_libs_dir)

# Fallback order if HYENA detection failed.
if not preferred_libs:
    if shared_libs_dir.exists():
        preferred_libs.append(shared_libs_dir)
    if local_libs_dir.exists():
        preferred_libs.append(local_libs_dir)

for lib_dir in preferred_libs:
    if str(lib_dir) not in sys.path:
        sys.path.append(str(lib_dir))
if str(project_root) not in sys.path:
    sys.path.append(str(project_root))
if str(current_dir) not in sys.path:
    sys.path.append(str(current_dir))

from src.config import Config
from src.point_farming_bot import build_and_run
from src.session_logger import PointSessionLogger


session_root = Path(getattr(Config, "SESSION_LOG_DIR", "") or (project_root / "logs"))
session_root.mkdir(parents=True, exist_ok=True)
session_ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
session_dir = session_root / f"session_{session_ts}"
session_dir.mkdir(parents=True, exist_ok=True)

run_log_path = session_dir / f"point_farming_bot_{os.getpid()}.log"
error_log_path = session_dir / "errors.log"

root_logger = logging.getLogger()
root_logger.setLevel(getattr(logging, str(Config.LOG_LEVEL).upper(), logging.INFO))
fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")

file_handler = logging.FileHandler(run_log_path, encoding="utf-8")
file_handler.setFormatter(fmt)
file_handler.setLevel(getattr(logging, str(Config.LOG_LEVEL).upper(), logging.INFO))
root_logger.addHandler(file_handler)

error_handler = logging.FileHandler(error_log_path, encoding="utf-8")
error_handler.setFormatter(fmt)
error_handler.setLevel(logging.ERROR)
root_logger.addHandler(error_handler)

stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(fmt)
stream_handler.setLevel(getattr(logging, str(Config.LOG_LEVEL).upper(), logging.INFO))
root_logger.addHandler(stream_handler)
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


def _terminate_pid(pid: int, timeout_s: float = 8.0) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except Exception:
        return False

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.2)

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except Exception:
        return False

    time.sleep(0.2)
    return not _pid_alive(pid)


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
            force_kill = str(os.getenv("POINT_FORCE_KILL_EXISTING", "0")).strip().lower() in (
                "1",
                "true",
                "yes",
                "on",
            )
            if not force_kill:
                raise RuntimeError(f"bot already running pid={existing_pid}")
            logger.warning("existing bot detected pid=%s, terminating due to POINT_FORCE_KILL_EXISTING=1", existing_pid)
            if not _terminate_pid(existing_pid):
                raise RuntimeError(f"failed to terminate existing bot pid={existing_pid}")

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
    session_logger = PointSessionLogger(base_dir=str(session_dir))
    try:
        lock_path = project_root / ".bot.lock"
        _install_lock(lock_path)
        logger.info("starting point farming bot")
        logger.info(
            "symbols=%s target_active=%s rotation=%.2f~%.2fh leverage=%s target_notional=%s max_notional=%s retain=%.2f max_hold_h=%.1f min_legs=%s enforce_all_pairs=%s dry_run=%s paused=%s session_dir=%s",
            Config.POINT_SYMBOLS,
            Config.TARGET_ACTIVE_TICKERS,
            Config.ROTATION_MIN_HOURS,
            Config.ROTATION_MAX_HOURS,
            Config.TARGET_LEVERAGE,
            Config.TARGET_NOTIONAL_PER_LEG_USD,
            Config.MAX_NOTIONAL_PER_LEG_USD,
            Config.RETAIN_PROBABILITY,
            Config.MAX_HOLD_HOURS,
            Config.EXCHANGE_MIN_LEGS,
            Config.ENFORCE_ALL_PAIR_TYPES,
            Config.DRY_RUN,
            Config.TRADING_START_PAUSED,
            session_logger.base_dir,
        )
        asyncio.run(
            build_and_run(
                session_logger=session_logger,
                dashboard_enabled=bool(Config.DASHBOARD_ENABLED),
            )
        )
    except KeyboardInterrupt:
        logger.info("stopped by user")
    except Exception as e:
        logger.critical("fatal: %s", e)
        time.sleep(0.1)
        raise
    finally:
        session_logger.finalize_to_xlsx()

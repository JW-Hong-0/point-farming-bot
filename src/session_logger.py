import csv
import logging
import os
from datetime import datetime
from typing import Dict, Optional

logger = logging.getLogger("PointSessionLogger")


class PointSessionLogger:
    def __init__(self, base_dir: Optional[str] = None):
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        root = base_dir or os.path.join(os.getcwd(), "logs")
        if os.path.basename(root).startswith("session_"):
            self.base_dir = root
        else:
            self.base_dir = os.path.join(root, f"session_{ts}")
        os.makedirs(self.base_dir, exist_ok=True)

        self.paths = {
            "trades": os.path.join(self.base_dir, "trades.csv"),
            "cycle_pairs": os.path.join(self.base_dir, "cycle_pairs.csv"),
            "cycle_exchange": os.path.join(self.base_dir, "cycle_exchange.csv"),
        }

        self.headers = {
            "trades": [
                "ts_utc",
                "cycle_id",
                "action",
                "ticker",
                "exchange",
                "side",
                "qty",
                "price",
                "notional",
                "leverage",
                "reason",
                "order_id",
                "status",
                "dry_run",
            ],
            "cycle_pairs": [
                "ts_utc",
                "cycle_id",
                "ticker",
                "long_venue",
                "short_venue",
                "qty",
                "leverage",
                "state",
                "reason",
            ],
            "cycle_exchange": [
                "ts_utc",
                "cycle_id",
                "exchange",
                "pair_count",
                "leg_count",
            ],
        }

    @staticmethod
    def _utc_now() -> str:
        return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    def _append(self, key: str, row: Dict) -> None:
        path = self.paths.get(key)
        headers = self.headers.get(key)
        if not path or not headers:
            return

        exists = os.path.exists(path)
        try:
            with open(path, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=headers)
                if not exists:
                    writer.writeheader()
                writer.writerow({h: row.get(h, "") for h in headers})
        except Exception as e:
            logger.warning("failed to append %s: %s", key, e)

    def log_trade(self, row: Dict) -> None:
        r = dict(row)
        r.setdefault("ts_utc", self._utc_now())
        self._append("trades", r)

    def log_cycle_pair(self, row: Dict) -> None:
        r = dict(row)
        r.setdefault("ts_utc", self._utc_now())
        self._append("cycle_pairs", r)

    def log_cycle_exchange(self, row: Dict) -> None:
        r = dict(row)
        r.setdefault("ts_utc", self._utc_now())
        self._append("cycle_exchange", r)

    def finalize_to_xlsx(self) -> Optional[str]:
        try:
            import pandas as pd
        except Exception:
            logger.warning("pandas unavailable, skip xlsx export")
            return None

        out = os.path.join(self.base_dir, "session.xlsx")
        try:
            with pd.ExcelWriter(out) as writer:
                for key, path in self.paths.items():
                    if not os.path.exists(path):
                        continue
                    try:
                        df = pd.read_csv(path)
                    except Exception:
                        continue
                    df.to_excel(writer, sheet_name=key[:31], index=False)
            logger.info("xlsx exported: %s", out)
            return out
        except Exception as e:
            logger.warning("xlsx export failed: %s", e)
            return None

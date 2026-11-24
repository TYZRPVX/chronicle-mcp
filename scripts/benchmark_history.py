"""Benchmark synthetic history only: python scripts/benchmark_history.py.

No browser discovery, real history access, network, or OS cache eviction.
Each mode gets one first sample followed by 30 alternating warm samples.
"""

import json
import math
import shutil
import sqlite3
import statistics

# Allow running directly from a checkout without an editable installation.
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chronicle_mcp.connection import open_readonly_database
from chronicle_mcp.database import query_history


def main():
    with tempfile.TemporaryDirectory(prefix="chronicle-benchmark-") as directory:
        source = Path(directory) / "History"
        target = Path(directory) / "copy.db"
        with sqlite3.connect(source) as db:
            db.execute(
                "CREATE TABLE urls (id INTEGER PRIMARY KEY, title TEXT, url TEXT, last_visit_time INTEGER, visit_count INTEGER)"
            )
            count = 0
            while source.stat().st_size < 131 * 1024 * 1024:
                db.executemany(
                    "INSERT INTO urls VALUES (?, ?, ?, ?, ?)",
                    (
                        (
                            i,
                            ("live config " if i % 1000 == 0 else "documentation ") + "x" * 300,
                            f"https://example.test/docs/{i}/" + "y" * 200,
                            13300000000000000 + i,
                            1,
                        )
                        for i in range(count, count + 10000)
                    ),
                )
                count += 10000
                db.commit()

        def measure(mode):
            start = time.perf_counter()
            if mode == "original_copy_phrase":
                shutil.copy2(source, target)
                db = sqlite3.connect(target)
            else:
                db = open_readonly_database(str(source))
            try:
                if mode == "readonly_terms":
                    rows = query_history(db, "live config", 5)
                else:
                    rows = db.execute(
                        "SELECT title, url, last_visit_time FROM urls WHERE title LIKE ? OR url LIKE ? ORDER BY last_visit_time DESC LIMIT ?",
                        ("%live config%", "%live config%", 5),
                    ).fetchall()
                assert len(rows) == 5
            finally:
                db.close()
                target.unlink(missing_ok=True)
            return (time.perf_counter() - start) * 1000

        modes = ["original_copy_phrase", "readonly_phrase", "readonly_terms"]
        first = {mode: measure(mode) for mode in modes}
        samples = {mode: [] for mode in modes}
        for run in range(30):
            for mode in modes[run % 3 :] + modes[: run % 3]:
                samples[mode].append(measure(mode))
        print(
            json.dumps(
                {
                    "database_bytes": source.stat().st_size,
                    "rows": count,
                    "samples_per_mode": 30,
                    "note": "First means first invocation, not cold OS cache; data was just generated. No model latency measured.",
                    "milliseconds": {
                        mode: {
                            "first": round(first[mode], 2),
                            "median": round(statistics.median(values), 2),
                            "p95": round(sorted(values)[math.ceil(len(values) * 0.95) - 1], 2),
                        }
                        for mode, values in samples.items()
                    },
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()

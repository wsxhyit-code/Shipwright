import argparse
import sqlite3
from contextlib import closing
from pathlib import Path


def initialize(runtime):
    runtime = Path(runtime)
    runtime.mkdir(parents=True, exist_ok=True)
    # ⚠️ `with sqlite3.connect(...)` **不关闭连接**（只 commit/rollback）。
    # 不显式关闭的话，Windows 上这个 db 文件在进程存活期间一直删不掉，
    # 固定验收里的 TemporaryDirectory 清理会 WinError 32 → 验收永远 FAIL。
    with closing(sqlite3.connect(runtime / "orders.sqlite3")) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, customer_id INTEGER, amount_cents INTEGER)")
        conn.execute("DELETE FROM orders")
        conn.executemany("INSERT INTO orders VALUES (?, ?, ?)", [(1, 1001, 1000), (2, 1001, 3000), (3, 1003, 1500)])
        conn.commit()
    for folder in ["alerts", "tasks", "reports", "delivery"]:
        (runtime / folder).mkdir(exist_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", default="runtime")
    args = parser.parse_args()
    initialize(args.runtime)
    print("Seeded customers: 1001=two orders; 1002=no orders; 1003=one order")


if __name__ == "__main__":
    main()

"""
check_detail_cache_production.py
====================
【一時的な診断専用スクリプト】

本番の GitHub Actions キャッシュ内にある実際の evaluations.db を対象に、
detail_cache テーブルの内容を読み取り専用で確認する。

このスクリプトは .github/workflows/inspect_detail_cache.yml から
Actions 上で実行される想定。ローカルの evaluations.db は
actions/cache で保持されている本番のDBとは別物のファイルのため、
ローカル実行では本番の状態を確認したことにならない
（.gitignore で *.db は追跡対象外のため）。

【やること】
    detail_cache テーブルから、対象URL群（本番 data.csv に現在ある
    既知11件。調布市の物件 nc_20988160 を含む）の
    total_units / repair_fund_monthly / fetched_at を読み取って表示する。

【やらないこと（副作用ゼロ）】
    - DBへの書き込みは一切しない（SELECTのみ）
    - SUUMOへのアクセスはしない
    - LINE通知はしない
    - data.csv・他ファイルの変更はしない
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "evaluations.db"

# 本番 data.csv に現在登録されている既知11件のURL
# （2026-07-04時点の data.csv より。調布物件を先頭に記載）
TARGET_URLS = [
    "https://suumo.jp/ms/chuko/tokyo/sc_chofu/nc_20988160/",  # 多摩川の自然に寄り添う（今回の調査対象）
    "https://suumo.jp/ms/chuko/tokyo/sc_fuchu/nc_20893454/",
    "https://suumo.jp/ms/chuko/tokyo/sc_fuchu/nc_21106723/",
    "https://suumo.jp/ms/chuko/tokyo/sc_fuchu/nc_21102869/",
    "https://suumo.jp/ms/chuko/tokyo/sc_fuchu/nc_21158356/",
    "https://suumo.jp/ms/chuko/tokyo/sc_fuchu/nc_21200581/",
    "https://suumo.jp/ms/chuko/tokyo/sc_fuchu/nc_20946582/",
    "https://suumo.jp/ms/chuko/tokyo/sc_fuchu/nc_21017717/",
    "https://suumo.jp/ms/chuko/tokyo/sc_fuchu/nc_20899303/",
    "https://suumo.jp/ms/chuko/tokyo/sc_fuchu/nc_78108434/",
    "https://suumo.jp/ms/chuko/tokyo/sc_inagi/nc_20985252/",
]


def main() -> None:
    print("=== detail_cache 本番確認（読み取り専用） ===")
    print(f"DBファイル: {DB_PATH}")
    print(f"存在: {DB_PATH.exists()}")
    if not DB_PATH.exists():
        print("[結果] evaluations.db が見つかりません"
              "（キャッシュ復元に失敗した可能性。restore-keysの一致を確認してください）")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    print(f"テーブル一覧: {tables}")
    print()

    if "detail_cache" not in tables:
        print("[結果] detail_cache テーブル自体が存在しません")
        conn.close()
        return

    total_in_table = conn.execute("SELECT COUNT(*) FROM detail_cache").fetchone()[0]
    print(f"detail_cache 全体の行数: {total_in_table}")
    print()

    print(f"{'URL':<62} {'total_units':>12} {'repair_fund':>14} {'fetched_at':>20}")
    print("-" * 115)
    found_count = 0
    for url in TARGET_URLS:
        row = conn.execute(
            "SELECT total_units, repair_fund_monthly, fetched_at "
            "FROM detail_cache WHERE listing_url = ?",
            (url,),
        ).fetchone()
        short_url = url.replace("https://suumo.jp/ms/chuko/tokyo/", "")
        if row is None:
            print(f"{short_url:<62} {'(行なし)':>12}")
        else:
            found_count += 1
            print(
                f"{short_url:<62} {str(row['total_units']):>12} "
                f"{str(row['repair_fund_monthly']):>14} {str(row['fetched_at']):>20}"
            )

    print("-" * 115)
    print(f"\n対象{len(TARGET_URLS)}件中、detail_cacheに行が存在するもの: {found_count} 件")

    conn.close()


if __name__ == "__main__":
    main()

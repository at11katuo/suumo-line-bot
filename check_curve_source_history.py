"""
check_curve_source_history.py
====================
【一時的な診断専用スクリプト】

本番の GitHub Actions キャッシュ内にある実際の evaluations.db を対象に、
紅葉丘2グループ（横断重複の実例）の各URLについて、日別の
curve_source（district/city のどちらのカーブを使ったか）・
current_fair_unit_price（適正㎡単価）・asking_vs_fair_pct（実勢比）の
推移を読み取り専用で確認する。

【背景】
    STEP2の通知サンプル検証で、同一物件（紅葉丘2・90.02㎡・3LDK・
    2003年7月築）のはずのURL間で実勢比が大きく食い違う事象が見つかった
    （nc_21269843: +10.2%割高 vs 別URL: -6〜-7.85%割安）。
    asking_vs_fair_pct はカーブ（curve.unit_price_for_age）由来の
    適正価格だけで決まり detail_cache は無関係なため（reinfolib_resale.py
    estimate_resale 参照）、原因は (a) curve_source が district/city で
    切り替わった、(b) 90日キャッシュの取引データ期間が実行日によって
    違った、のどちらかに絞られる。このスクリプトで実際のDB履歴を確認する。

このスクリプトは .github/workflows/check_curve_source_history.yml から
Actions 上で実行される想定。ローカルの evaluations.db は actions/cache で
保持されている本番のDBとは別物のファイルのため、ローカル実行では
本番の状態を確認したことにならない（.gitignore で *.db は追跡対象外）。

【やること】
    紅葉丘2グループの各URLについて、evaluations テーブルから
    evaluated_date・curve_source・current_fair_unit_price・
    asking_vs_fair_pct を日付昇順で表示する。

【やらないこと（副作用ゼロ）】
    - DBへの書き込みは一切しない（SELECTのみ）
    - SUUMOへのアクセスはしない
    - LINE通知はしない
    - data.csv・他ファイルの変更はしない
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "evaluations.db"

# 紅葉丘2グループ（90.02㎡・3LDK・2003年7月築）の全URL
# （2026-07-14時点の data.csv より。横断重複の実例として調査対象）
TARGET_URLS = [
    "https://suumo.jp/ms/chuko/tokyo/sc_fuchu/nc_21251938/",
    "https://suumo.jp/ms/chuko/tokyo/sc_fuchu/nc_21269843/",
    "https://suumo.jp/ms/chuko/tokyo/sc_fuchu/nc_21210987/",
    "https://suumo.jp/ms/chuko/tokyo/sc_fuchu/nc_20544700/",
    "https://suumo.jp/ms/chuko/tokyo/sc_fuchu/nc_20946581/",
    "https://suumo.jp/ms/chuko/tokyo/sc_fuchu/nc_21144972/",
    "https://suumo.jp/ms/chuko/tokyo/sc_fuchu/nc_21182213/",
    "https://suumo.jp/ms/chuko/tokyo/sc_fuchu/nc_21104482/",
    "https://suumo.jp/ms/chuko/tokyo/sc_fuchu/nc_20545192/",
    "https://suumo.jp/ms/chuko/tokyo/sc_fuchu/nc_20900524/",
    "https://suumo.jp/ms/chuko/tokyo/sc_fuchu/nc_21019122/",
    "https://suumo.jp/ms/chuko/tokyo/sc_fuchu/nc_21051135/",
]


def main() -> None:
    print("=== curve_source 履歴確認（読み取り専用） ===")
    print(f"DBファイル: {DB_PATH}")
    print(f"存在: {DB_PATH.exists()}")
    if not DB_PATH.exists():
        print("[結果] evaluations.db が見つかりません"
              "（キャッシュ復元に失敗した可能性。restore-keysの一致を確認してください）")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    columns = {row[1] for row in conn.execute("PRAGMA table_info(evaluations)").fetchall()}
    print(f"evaluationsテーブルのカラム: {sorted(columns)}")
    print()
    if "curve_source" not in columns:
        print("[結果] curve_source カラムが存在しません（マイグレーション未実行の可能性）")
        conn.close()
        return

    for url in TARGET_URLS:
        rows = conn.execute(
            """
            SELECT evaluated_date, curve_source, current_fair_unit_price, asking_vs_fair_pct, asking_price
            FROM evaluations
            WHERE listing_url = ?
            ORDER BY evaluated_date
            """,
            (url,),
        ).fetchall()
        short_url = url.replace("https://suumo.jp/ms/chuko/tokyo/sc_fuchu/", "")
        print(f"--- {short_url} ({len(rows)}行) ---")
        for r in rows:
            print(
                f"  {r['evaluated_date']}  curve_source={r['curve_source']!r:30}  "
                f"適正㎡単価={r['current_fair_unit_price']}  "
                f"実勢比={r['asking_vs_fair_pct']}  "
                f"売出={r['asking_price']}"
            )
        if not rows:
            print("  (該当行なし)")
        print()

    conn.close()


if __name__ == "__main__":
    main()

"""
check_district_sample_distribution.py
====================
【診断専用スクリプト】

対象3市（府中市・調布市・稲城市）の実際の取引データを国交省APIから
取得し、地区別・築年数バケット別のサンプル数分布を表示する。
地区単位カーブ導入（フォールバック閾値の決定）の判断材料とする。

【やること】
    1. 各市について fetch_trades_multi_year で取引データを取得
       （build_curves.py と同じ年範囲・同じ関数をそのまま再利用。
       新しいAPI呼び出しパターンは作らない）
    2. normalize() で正規化（build_curves.py と同じ関数）
    3. 地区(district)・築年数バケットごとにグルーピングし、
       サンプル数を集計して表示
    4. 閾値の候補（3/5/8/10/15/20件）ごとに、何箇所の
       (地区, 築年数バケット) がその閾値を満たすかを表示する

【やらないこと（副作用ゼロ）】
    - キャッシュファイル（cache/*.json）への保存はしない
    - DBへの書き込みはしない
    - build_curves.py の既存キャッシュには一切触れない
    - LINE通知・data.csvの変更はしない
"""

import os
from collections import defaultdict

from build_curves import FETCH_END_YEAR, FETCH_START_YEAR, TARGET_AREAS
from reinfolib_resale import AGE_BUCKETS, ReinfolibClient, _bucket_of, normalize


def main() -> None:
    api_key = os.environ.get("REINFOLIB_API_KEY", "").strip()
    if not api_key:
        print("[エラー] REINFOLIB_API_KEY が未設定です。")
        return

    client = ReinfolibClient(api_key=api_key)

    for city_name, city_code in TARGET_AREAS.items():
        print(f"\n{'=' * 100}")
        print(f"=== {city_name}（{city_code}） ===")
        print(f"{'=' * 100}")

        raw_rows = client.fetch_trades_multi_year(
            city_code=city_code,
            start_year=FETCH_START_YEAR,
            end_year=FETCH_END_YEAR,
        )
        trades = normalize(raw_rows)
        print(f"取引データ総数（正規化後・中古マンションのみ）: {len(trades)} 件")

        # 地区ごとの総サンプル数（築年数バケット問わず）
        district_totals: dict[str, int] = defaultdict(int)
        # 地区×築年数バケットごとのサンプル数
        district_bucket_counts: dict[str, dict[tuple, int]] = defaultdict(lambda: defaultdict(int))

        no_district_count = 0
        for t in trades:
            district = t.district or "(地区名なし)"
            if not t.district:
                no_district_count += 1
            district_totals[district] += 1
            if t.age_at_trade is not None and t.age_at_trade >= 0:
                bucket = _bucket_of(t.age_at_trade)
                district_bucket_counts[district][bucket] += 1

        print(f"地区名が取得できない取引: {no_district_count} 件")
        print(f"地区の種類数: {len(district_totals)}")
        print()

        # 総サンプル数の多い順に表示
        sorted_districts = sorted(district_totals.items(), key=lambda kv: -kv[1])
        print(f"{'地区名':<20} {'総数':>6}  築年数バケット別内訳（サンプル数1件以上のみ表示）")
        print("-" * 100)
        for district, total in sorted_districts:
            bucket_str = " / ".join(
                f"{lo}-{hi}年:{district_bucket_counts[district].get((lo, hi), 0)}"
                for lo, hi in AGE_BUCKETS
                if district_bucket_counts[district].get((lo, hi), 0) > 0
            )
            print(f"{district:<20} {total:>6}  {bucket_str}")

        # 閾値候補ごとに「何箇所の(地区,築年数バケット)が十分なサンプルを持つか」を集計
        print()
        print("【閾値候補ごとの充足状況】(地区, 築年数バケット)の組み合わせのうち、")
        print("その件数以上のサンプルを持つものがいくつあるか:")
        for threshold in [3, 5, 8, 10, 15, 20]:
            qualifying = sum(
                1
                for buckets in district_bucket_counts.values()
                for count in buckets.values()
                if count >= threshold
            )
            print(f"  閾値{threshold:>2}件以上: {qualifying} 組み合わせ")


if __name__ == "__main__":
    main()

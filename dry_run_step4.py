"""
dry_run_step4.py
====================
【一時的な診断専用スクリプト】STEP 4 dry_run。

本番の data.csv（このチェックアウト時点の最新版）と本番の evaluations.db・
cache/（actions/cache から読み取り専用で復元。書き込みしても永続化されない）
を土台に、main() の主要4経路（フル新着・別業者掲載検知・再掲載・値下げ
グループ集約）を通しで動かし、LINE送信の代わりに本文を print する。

【背景（なぜこの構成にしたか）】
    過去のレビューで、ローカルの USE_MOCK_REINFOLIB 汚染キャッシュ（地区名
    "テスト町" のダミーデータ）を「実測reinfolib値」と誤認して報告する事故が
    起きた。この dry_run はその再発防止のため、
      1. ローカルキャッシュ（reinfolibモック・古い90日キャッシュ）を一切
         経由しない設計にする（本番の cache/・evaluations.db をそのまま使う）
      2. 冒頭で環境健全性チェック（nc_21269843のcurve_source・適正㎡単価を
         独立に再計算し、DBに既に記録されている値と一致するか）を行い、
         不一致なら dry_run全体を中断する
    の2点を必須要件として組み込んでいる。

【やること】
    1. 環境健全性チェック（health_check）
    2. main() の scrape() を、実データ(data.csv)＋4経路を確実に踏む注入
       データに差し替えて実行
    3. LINE送信（requests.post）を print に差し替えて実行し、送信されるはず
       だった本文をそのまま出力する（実送信は発生しない）
    4. Gemini呼び出し（evaluate_listing）・SUUMO詳細ページ取得
       （detail_fetcher.fetch_detail）は固定値にモックする（外部アクセス
       ゼロ・経路確認を決定的にするため。reinfolib評価・DB読み書きは本物）

【やらないこと（副作用最小限）】
    - evaluations.db・cache/ の永続化。GitHub Actions上は actions/cache/restore
      のみを使い save を行わないため自動的に破棄されるが、【2026-07-17 追記】
      それはランナーが使い捨てだから成立する保護であり、ローカル実行には
      効かない（実際にローカル実行で本番と無関係な evaluations.db に
      DRY_RUN注入行が永続化される事故が発生した）。そのため main() 内で
      evaluations.db 自体も data.csv と同様スクラッチコピーに差し替え、
      evaluator.DB_PATH（本番DBパス）には一切書き込ませない設計にしている。
      実行後、本番DBのmtimeが変化していないかも二重チェックする。
    - 実際の data.csv への保存（コピーに対して save_listings させる）
    - LINE通知の実送信
    - SUUMOへの実アクセス（scrape・detail_fetch とも差し替え）
"""

from __future__ import annotations

import csv
import shutil
import sqlite3
import sys
import tempfile
import time
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import scraper
from scraper import Listing

DB_PATH = Path(__file__).parent / "evaluations.db"
DATA_CSV_PATH = Path(__file__).parent / "data.csv"

HEALTH_CHECK_URL = "nc_21269843"


# ---------------------------------------------------------------------------
# 1. 環境健全性チェック（汚染事故の再発防止ゲート）
# ---------------------------------------------------------------------------

def _independent_estimate(url_fragment: str) -> dict | None:
    """DBに書き込まず、data.csv記載の物件について current_fair_unit_price・
    curve_source・asking_vs_fair_pct を独立に再計算する。"""
    from build_curves import get_curve_bundle
    from evaluator import _city_name, resolve_city_code, DEFAULT_HOLD_YEARS
    from reinfolib_resale import estimate_resale, select_curve
    from suumo_adapter import suumo_to_candidate

    with open(DATA_CSV_PATH, encoding="utf-8", newline="") as f:
        rows = [Listing(**row) for row in csv.DictReader(f)]
    listing = next((l for l in rows if url_fragment in l.url), None)
    if listing is None:
        print(f"[健全性チェック] {url_fragment} が data.csv に見つかりません。判定不能としてスキップします。")
        return None

    city_code = resolve_city_code(listing.location)
    if city_code is None:
        print(f"[健全性チェック] {listing.location} の市コードが判定できません。")
        return None
    city_name = _city_name(city_code)

    bundle = get_curve_bundle(city_name=city_name, city_code=city_code)
    if bundle is None:
        print(f"[健全性チェック] {city_name} のカーブが取得できませんでした（キャッシュ・APIキー未設定の可能性）。")
        return None

    candidate = suumo_to_candidate(listing, detail=None, city_name=city_name)
    if candidate is None:
        print(f"[健全性チェック] {url_fragment} の候補変換に失敗しました。")
        return None

    current_year = date.today().year
    current_age = current_year - candidate.building_year
    curve, curve_source = select_curve(
        candidate.district, bundle.city_curve, bundle.district_curves,
        current_age, city_name=city_name,
    )
    est = estimate_resale(candidate, curve, current_year, DEFAULT_HOLD_YEARS, curve_source=curve_source)
    return {
        "curve_source": curve_source,
        "current_fair_unit_price": est.current_fair_unit_price,
        "asking_vs_fair_pct": est.asking_vs_fair_pct,
    }


def health_check(url_fragment: str = HEALTH_CHECK_URL) -> bool:
    """
    独立に再計算した値と、本番DBに既に記録されている最新行を突き合わせる。
    一致しなければ False を返す（呼び出し側で dry_run 全体を中断する）。
    比較不能（DB履歴なし等）の場合は判定不能として True を返す（中断しない）。
    """
    print("=" * 70)
    print("[健全性チェック] 環境汚染ゲート（前回のモックキャッシュ汚染事故の再発防止）")
    print("=" * 70)

    fresh = _independent_estimate(url_fragment)
    if fresh is None:
        print("[健全性チェック] 再計算できなかったため判定不能。dry_runは継続します（結果の数値は参考程度に）。")
        return True

    if not DB_PATH.exists():
        print("[健全性チェック][警告] evaluations.db が見つかりません（キャッシュ復元失敗の可能性）。判定不能。")
        return True

    from evaluator import _init_db  # curve_source列のマイグレーションを確実に通すため

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        _init_db(conn)
        row = conn.execute(
            "SELECT evaluated_date, curve_source, current_fair_unit_price, asking_vs_fair_pct "
            "FROM evaluations WHERE listing_url LIKE ? ORDER BY evaluated_date DESC LIMIT 1",
            (f"%{url_fragment}%",),
        ).fetchone()
    except sqlite3.OperationalError as e:
        print(f"[健全性チェック][警告] DB照会に失敗しました（{e}）。判定不能。")
        conn.close()
        return True
    conn.close()

    if row is None:
        print(f"[健全性チェック][警告] {url_fragment} のDB履歴が見つかりません。判定不能。")
        return True

    print(f"対象URL: {url_fragment}")
    print(
        f"  DB最新行({row['evaluated_date']}): curve_source={row['curve_source']!r} "
        f"適正㎡単価={row['current_fair_unit_price']} 実勢比={row['asking_vs_fair_pct']}"
    )
    print(
        f"  独立再計算          : curve_source={fresh['curve_source']!r} "
        f"適正㎡単価={fresh['current_fair_unit_price']} 実勢比={fresh['asking_vs_fair_pct']}"
    )

    ok = (
        row["curve_source"] == fresh["curve_source"]
        and row["current_fair_unit_price"] is not None
        and fresh["current_fair_unit_price"] is not None
        and abs(row["current_fair_unit_price"] - fresh["current_fair_unit_price"]) < 1.0
    )
    print(f"判定: {'OK（環境は健全）' if ok else 'NG（環境汚染の疑いあり。dry_runを中断します）'}")
    print("=" * 70)
    return ok


# ---------------------------------------------------------------------------
# 2. 4経路を確実に踏むための注入データ
# ---------------------------------------------------------------------------

def build_injected_current() -> list[Listing]:
    """data.csv の実データをベースに、4経路（フル新着・別業者掲載検知・
    再掲載・値下げグループ集約）を確実に踏む注入データを混ぜて返す。"""
    with open(DATA_CSV_PATH, encoding="utf-8", newline="") as f:
        base = [Listing(**row) for row in csv.DictReader(f)]

    injected: list[Listing] = []

    # (A) 全部新規の横断重複グループ → フル新着 + dual_note
    # 実在の既知グループ（紅葉丘2等）と同じ属性を使うと「既知×新規混在」
    # （別業者掲載検知）に吸収されてしまうため、data.csvのどの既知物件とも
    # 一致しない架空の属性を使う。
    injected.append(Listing(
        name="[DRY_RUN注入A] 新規業者・架空物件（全部新規グループ）",
        price="4990万円", location="東京都府中市dryrun架空町１",
        url="https://suumo.jp/dry-run/new-group-a/",
        floor_plan="3LDK", area="77.77m2（壁芯）", age="2015年1月",
    ))
    injected.append(Listing(
        name="[DRY_RUN注入A] 新規業者・架空物件（全部新規グループ・もう1件）",
        price="4980万円", location="東京都府中市dryrun架空町１",
        url="https://suumo.jp/dry-run/new-group-b/",
        floor_plan="3LDK", area="77.77m2（壁芯）", age="2015年1月",
    ))

    # (B) 既知×新規混在 → 別業者掲載検知
    #     本町1グループの実在既知物件と同一属性の新規URLを1件注入する
    honcho_known = next((l for l in base if l.location.startswith("東京都府中市本町")), None)
    if honcho_known is not None:
        injected.append(Listing(
            name="[DRY_RUN注入B] 新規業者・本町1相当（既知×新規混在）",
            price=honcho_known.price, location=honcho_known.location,
            url="https://suumo.jp/dry-run/dual-listing/",
            floor_plan=honcho_known.floor_plan, area=honcho_known.area, age=honcho_known.age,
        ))
    else:
        print("[dry_run][警告] 本町1の既知物件が data.csv に見つからず、別業者掲載検知経路をスキップします。")

    # (C) 再掲載検知 → 7/8以前の観測でdata.csvから消えた紅葉丘2のURLを再投入
    #     （STEP1報告時点の13件のうち、現データに残っていない3件の1つ）
    injected.append(Listing(
        name="[DRY_RUN注入C] 紅葉丘2・過去に消滅したURLの再出現",
        price="5290万円", location="東京都府中市紅葉丘２",
        url="https://suumo.jp/ms/chuko/tokyo/sc_fuchu/nc_21219793/",
        floor_plan="3LDK", area="90.02m2（壁芯）", age="2003年7月",
    ))

    # (D) 値下げグループ集約 → 紅葉丘2の既知メンバーのうち1件をさらに値下げ
    momiji_known = [l for l in base if l.location.startswith("東京都府中市紅葉丘")]
    result = [l for l in base if l not in momiji_known]
    if momiji_known:
        cheapest = min(momiji_known, key=lambda l: l.url)  # 決定的に1件選ぶ
        result.extend(l for l in momiji_known if l.url != cheapest.url)
        result.append(replace(cheapest, price="4990万円"))
    else:
        print("[dry_run][警告] 紅葉丘2の既知物件が data.csv に見つからず、値下げグループ集約経路をスキップします。")

    return result + injected


# ---------------------------------------------------------------------------
# 3. LINE送信の差し替え（print化）
# ---------------------------------------------------------------------------

_sent_message_count = 0


def _fake_post(url, headers=None, json=None, timeout=None, **kwargs):
    global _sent_message_count
    resp = MagicMock(status_code=200, text="OK(dry_run)")
    messages = (json or {}).get("messages", [])
    for msg in messages:
        _sent_message_count += 1
        print("\n" + "─" * 70)
        print(f"[LINE送信されるはずだった本文 #{_sent_message_count}]（実送信なし）")
        print("─" * 70)
        print(msg.get("text", ""))
    return resp


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------

def main() -> None:
    print("=== STEP 4 dry_run 開始 ===")
    print(f"data.csv: {DATA_CSV_PATH}（存在: {DATA_CSV_PATH.exists()}）")
    print(f"evaluations.db: {DB_PATH}（存在: {DB_PATH.exists()}）")

    if not health_check():
        print("\n環境健全性チェックNG。dry_runを中断します。", file=sys.stderr)
        sys.exit(1)

    injected_current = build_injected_current()
    print(f"\n注入後の current 件数: {len(injected_current)} 件")

    # data.csv の実内容をスクラッチにコピーし、scraper.DATA_FILE をそちらに
    # 差し替える（known_urls は実データを反映しつつ、save_listings による
    # 上書きは本物の data.csv に影響させないため）。
    scratch_data_csv = Path(tempfile.mktemp(suffix="_data.csv"))
    shutil.copy(DATA_CSV_PATH, scratch_data_csv)

    # evaluations.db も同様にスクラッチコピーへ差し替える。
    # 【2026-07-17 追記】GitHub Actions上では「actions/cache/restore のみ・
    # save なし」により書き込みが自動的に破棄されるが、これはランナーが
    # 使い捨てだから成立する話であり、ローカル実行にはその保護がない。
    # 実際にローカル実行で本番と無関係な evaluations.db に [DRY_RUN注入...]
    # 行が永続化される事故が起きたため、evaluator.DB_PATH 自体をこの
    # スクラッチコピーに差し替え、本番DBには一切書き込ませない設計にする。
    scratch_db_path = Path(tempfile.mktemp(suffix="_evaluations.db"))
    if DB_PATH.exists():
        shutil.copy(DB_PATH, scratch_db_path)
    db_mtime_before = DB_PATH.stat().st_mtime if DB_PATH.exists() else None

    import evaluator
    import detail_fetcher
    import gemini_cache

    # 【2026-07-17 追記】detail_fetcher.py・gemini_cache.py は evaluations.db
    # と同じファイルに detail_cache / gemini_evaluations テーブルを持つが、
    # DB_PATH を evaluator.py から import せず、それぞれ独自に
    # Path(__file__).parent / "evaluations.db" を定義している。
    # evaluator.DB_PATH だけを差し替えても、この2モジュール経由の書き込み
    # （実際に Gemini キャッシュ保存で発生した）は本番DBに漏れるため、
    # 3箇所とも同じスクラッチコピーに差し替える。
    try:
        with patch.object(scraper, "scrape", lambda url: injected_current), \
             patch.object(scraper, "evaluate_listing", lambda listing: (5, (
                 "総合評価：★★★★★ (5/5)\n"
                 "ヤドカリ投資メリット：[DRY_RUN] 経路確認用の固定評価\n"
                 "懸念点：[DRY_RUN] 経路確認用の固定評価\n"
                 "数年後売却ポテンシャル：高い／[DRY_RUN]\n"
                 "判定：即内覧推奨"
             ))), \
             patch("detail_fetcher.fetch_detail", lambda url, **kw: None), \
             patch.object(scraper.time, "sleep", lambda *a, **k: None), \
             patch.object(scraper, "requests") as mock_requests, \
             patch.object(scraper, "LINE_CHANNEL_ACCESS_TOKEN", "dry-run-dummy-token"), \
             patch.object(scraper, "LINE_USER_ID", "dry-run-dummy-user"), \
             patch.object(scraper, "DATA_FILE", str(scratch_data_csv)), \
             patch.object(scraper, "GEMINI_EVAL_LIMIT_PER_RUN", 999), \
             patch.object(evaluator, "DB_PATH", scratch_db_path), \
             patch.object(detail_fetcher, "DB_PATH", scratch_db_path), \
             patch.object(gemini_cache, "DB_PATH", scratch_db_path):
        # GEMINI_EVAL_LIMIT_PER_RUN を引き上げる理由: evaluate_listing は
        # 固定値にモック済みでAPIコストは発生しないため、本番のGemini
        # キャッシュ充足状況（未評価の既知物件バックログ）に左右されず、
        # 注入した新着物件が確実に評価対象に入るようにする
        # （バックログに埋もれてフル新着経路が確認できない事故を防ぐ）。

            mock_requests.post.side_effect = _fake_post

            scraper.main()
    finally:
        scratch_data_csv.unlink(missing_ok=True)
        scratch_db_path.unlink(missing_ok=True)

    # 防御的二重チェック: 本番DBのmtimeが実行中に変化していないか
    # （evaluator.DB_PATH の差し替えが何らかの理由で効かなかった場合の検知用）。
    db_mtime_after = DB_PATH.stat().st_mtime if DB_PATH.exists() else None
    if db_mtime_before != db_mtime_after:
        print(
            f"\n[警告][事故] 本番DB（{DB_PATH}）が実行中に変更された形跡があります。"
            "スクラッチ差し替えが効いていない可能性があるため、内容を確認してください。",
            file=sys.stderr,
        )
        sys.exit(1)

    print("\n" + "=" * 70)
    print(f"=== dry_run 完了: LINE送信されるはずだった通知は {_sent_message_count} 件（実送信は発生していません） ===")
    print("=" * 70)


if __name__ == "__main__":
    main()

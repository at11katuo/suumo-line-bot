"""
backfill_gemini_evaluations.py
====================
既知物件のうち gemini_evaluations に未保存のものへ、
一度だけ Gemini評価を実行して保存する「バックフィル」専用スクリプト。

【背景】
    参考枠（scraper.notify_line_reference）は、既知物件について
    gemini_evaluations に保存済みの Gemini 評価を使って判定する設計。
    しかし gemini_cache.py 導入前から「既知」だった物件は、一度も
    new_listings として扱われたことがなく、Gemini評価が保存される
    機会が構造的になかった（実際に調布市の物件で確認された）。
    このスクリプトで一度だけ埋め合わせる。

    本番の main() と同一の evaluate_listing（同じモデル・同じ
    プロンプト）をそのまま使う。簡略化した別実装は評価軸の不整合を
    招くリスクがあるため使わない。

【冪等性】
    data.csv の全物件のうち、gemini_evaluations に「まだ保存されて
    いない」ものだけを対象にする（find_backfill_targets）。2回実行
    しても、1回目で保存されたものは2回目で対象から外れるため、
    Gemini APIが重複して呼ばれることはない。

【dry_run（安全策）】
    環境変数 DRY_RUN が "false" のときだけ実際にバックフィルする。
    それ以外（未設定・"true"・その他）は安全側としてドライラン扱いにし、
    対象物件の一覧を表示するだけで、Gemini API呼び出し・DB保存は
    一切行わない。

【やらないこと（副作用最小限）】
    - LINE通知は送らない
    - evaluations・detail_cache・sashine_notifications・
      reference_notifications への書き込みはしない
      （gemini_evaluations のみに書き込む。dry_run時はそれもしない）
    - SUUMOへのアクセスはしない（詳細ページ取得は行わない）
"""

from __future__ import annotations

import csv
import os
import time
from pathlib import Path
from typing import Optional

from gemini_cache import load_gemini_evaluations, save_gemini_evaluation
from scraper import DATA_FILE, Listing, evaluate_listing

# Gemini API TPM制限対策。main() と同じ間隔（無料枠: 429回避）
GEMINI_SLEEP_SEC = 15


def _load_all_known_listings(data_file: Optional[str] = None) -> list[Listing]:
    """
    data.csv から全物件を読み込む（スクレイプしない。読み取りのみ）。

    data_file: None なら呼び出し時点の scraper.DATA_FILE を使う
               （関数定義時に固定値へ束縛されるのを避けるため、本体側で
               解決する。db_path と同じ設計パターン）。
    """
    if data_file is None:
        data_file = DATA_FILE
    path = Path(data_file)
    if not path.exists():
        print(f"[エラー] {data_file} が見つかりません。")
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return [Listing(**row) for row in csv.DictReader(f)]


def find_backfill_targets(
    listings: list[Listing],
    db_path: Optional[Path] = None,
) -> list[Listing]:
    """
    gemini_evaluations に未保存の物件だけを返す（冪等性の核心）。

    load_gemini_evaluations で「保存済みURL集合」を一括取得し、そこに
    含まれない物件だけを対象とする。これを使う限り、既に保存済みの
    物件へ Gemini API が重複して呼ばれることはない。

    db_path: None なら呼び出し時点の gemini_cache.DB_PATH を使う
             （テストで一時DBに差し替えるための引数）。
    """
    all_urls = [l.url for l in listings]
    already_saved = load_gemini_evaluations(all_urls, db_path=db_path)
    return [l for l in listings if l.url not in already_saved]


def run_backfill(
    targets: list[Listing],
    dry_run: bool,
    db_path: Optional[Path] = None,
    sleep_sec: Optional[float] = None,
) -> int:
    """
    バックフィル本体。

    dry_run=True のときは対象一覧を表示するだけで、Gemini API呼び出し・
    DB保存は一切行わない（戻り値も常に0）。
    dry_run=False のときだけ実際に evaluate_listing → save_gemini_evaluation
    を行う。

    sleep_sec: None なら呼び出し時点の GEMINI_SLEEP_SEC を使う
               （関数定義時に固定値へ束縛されるのを避けるため、本体側で
               解決する。db_path と同じ設計パターン。テストでこれを
               モックせずに 15 秒の実待機が走ってしまう事故を防ぐ）。

    戻り値: 実際に評価・保存した件数（dry_run=True のときは常に0）。
    """
    if sleep_sec is None:
        sleep_sec = GEMINI_SLEEP_SEC
    print(f"[バックフィル対象] {len(targets)} 件")
    for i, listing in enumerate(targets, start=1):
        print(f"  [{i}/{len(targets)}] {listing.name[:30]} ({listing.url})")

    if dry_run:
        print("\n[DRY RUN] Gemini API・DB保存は行いません。上記が対象一覧です。")
        print("実際にバックフィルするには dry_run=false で再実行してください。")
        return 0

    if not targets:
        print("\nバックフィル対象はありませんでした（全件すでに保存済み）。")
        return 0

    saved_count = 0
    for i, listing in enumerate(targets, start=1):
        print(f"\n[{i}/{len(targets)}] {listing.name[:30]} を評価中...", flush=True)
        score, eval_text = evaluate_listing(listing)
        save_gemini_evaluation(listing.url, score, eval_text, db_path=db_path)
        print(f"    → {score}★ で保存しました", flush=True)
        saved_count += 1
        if i < len(targets):
            time.sleep(sleep_sec)  # Gemini API TPM制限対策（main()と同じ間隔）

    print(f"\n完了: {saved_count} 件のGemini評価をバックフィルしました。")
    return saved_count


def main() -> None:
    # 安全側デフォルト: DRY_RUN が明示的に "false" のときだけ本実行する。
    # 未設定・"true"・その他の値はすべてドライラン扱いにする。
    dry_run = os.environ.get("DRY_RUN", "true").strip().lower() != "false"

    print("=== Gemini評価バックフィル ===")
    print(f"モード: {'DRY RUN（表示のみ）' if dry_run else '本実行（Gemini API呼び出し・DB保存あり）'}")
    print()

    listings = _load_all_known_listings()
    print(f"data.csv から読み込み: {len(listings)} 件")

    targets = find_backfill_targets(listings)
    already_saved_count = len(listings) - len(targets)
    print(
        f"保存済み（スキップ対象）: {already_saved_count} 件 / "
        f"未保存（今回の対象）: {len(targets)} 件\n"
    )

    run_backfill(targets, dry_run=dry_run)


if __name__ == "__main__":
    main()

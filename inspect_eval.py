"""
inspect_eval.py
====================
現在 SUUMO に出ている物件の評価内訳をターミナルに表示する、読み取り専用の確認スクリプト。

【使い方】
    # 通常: SUUMO をライブスクレイプして評価（詳細取得なし）
    python inspect_eval.py

    # data.csv の物件だけ使う（スクレイプしない）
    python inspect_eval.py --from-csv

    # 詳細ページから総戸数・修繕積立金を取得してスコア比較を表示
    python inspect_eval.py --from-csv --with-detail

    # キャッシュを無視して国交省APIから最新カーブを取得
    python inspect_eval.py --force-refresh

    # 所在地文字列を表示（市コード判定不可の原因調査用）
    python inspect_eval.py --debug

【やること】
    1. SUUMO から物件を収集・フィルタリング（scrape + apply_filters）
       ※ --from-csv のときは data.csv から読み込む
    2. 各物件を所在市のカーブで estimate_resale 評価
    3. スコア / 乖離率 / 有望判定(○×) / ×の理由 を1行ずつ表示
    4. --with-detail のとき: 詳細ページから総戸数・修繕積立金を取得し
       スコアの「詳細なし→詳細あり」の変化を比較表示する

【やらないこと（副作用ゼロ）】
    - LINE通知は送らない
    - evaluations.db の evaluations テーブルには書き込まない
      ※ detail_cache テーブルへの保存は許可（次回の再アクセスを避けるため）
    - data.csv を変更しない

【APIキーの設定方法】
    プロジェクトルートに .env ファイルを作って書いておくと、このスクリプト起動時に
    自動で読み込まれる（.env は gitignore 済みなので誤コミットの心配なし）:

        REINFOLIB_API_KEY=あなたのキー

    .env.example を参考にしてください。
"""

import argparse
import csv
import datetime
import os
from pathlib import Path
from typing import Optional

# ---- .env ファイルの自動読み込み ----
# プロジェクトルートに .env があれば環境変数に取り込む。
# これにより REINFOLIB_API_KEY をコマンドラインや画面に晒さずに済む。
# (外部ライブラリ不要。python-dotenv がなくても動く)
_ENV_FILE = Path(__file__).parent / ".env"
if _ENV_FILE.exists():
    with open(_ENV_FILE, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _, _val = _line.partition("=")
                # os.environ.setdefault で「既に設定済みの変数は上書きしない」を保証する
                os.environ.setdefault(_key.strip(), _val.strip())

from build_curves import TARGET_AREAS, get_curve
from detail_fetcher import (
    fetch_detail,
    get_uncached_urls,
    load_detail_cache,
    save_detail_cache,
)
from evaluator import resolve_city_code
from reinfolib_resale import estimate_resale
from scraper import (
    DATA_FILE,
    PROMISING_SCORE_THRESHOLD,
    PROMISING_VS_FAIR_MAX_PCT,
    TARGET_URL,
    Listing,
    apply_filters,
    scrape,
)
from suumo_adapter import suumo_to_candidate

# 市区町村コード → 市名の逆引き（get_curve の city_name 引数に使う）
_CODE_TO_NAME: dict[str, str] = {code: name for name, code in TARGET_AREAS.items()}

# 評価に使う基準年（今年）と想定保有年数
CURRENT_YEAR = datetime.date.today().year
HOLD_YEARS   = 10

# 表示列幅（両モード共通）
_NAME_W = 22
_CITY_W = 5


def _fail_reason(score: Optional[int], vs_fair: Optional[float]) -> str:
    """有望でない理由を文字列で返す。スコア不足 / 乖離率超過 / 両方。"""
    parts = []
    if score is None or score < PROMISING_SCORE_THRESHOLD:
        parts.append(f"スコア不足({score}/100 < {PROMISING_SCORE_THRESHOLD})")
    if vs_fair is not None and vs_fair > PROMISING_VS_FAIR_MAX_PCT:
        parts.append(f"乖離率超過({vs_fair:+.1f}% > +{PROMISING_VS_FAIR_MAX_PCT:.1f}%)")
    return " / ".join(parts)


def _is_promising(score: Optional[int], vs_fair: Optional[float]) -> bool:
    """有望判定。スコアが閾値以上かつ乖離率が上限以内。"""
    if score is None or score < PROMISING_SCORE_THRESHOLD:
        return False
    if vs_fair is not None and vs_fair > PROMISING_VS_FAIR_MAX_PCT:
        return False
    return True


def _load_from_csv() -> list[Listing]:
    """data.csv から収集済み物件を読み込む（スクレイプしない）。"""
    path = Path(DATA_FILE)
    if not path.exists():
        print(f"[エラー] {DATA_FILE} が見つかりません。先に scraper.py を実行してください。")
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return [Listing(**row) for row in csv.DictReader(f)]


# ---------------------------------------------------------------------------
# 通常モード（詳細取得なし）の表示ループ
# ---------------------------------------------------------------------------

def _run_normal(
    listings: list[Listing],
    curve_cache: dict,
    debug: bool,
) -> None:
    """詳細取得なしで各物件を評価して表示する（--with-detail なし時の動作）。"""
    sep    = "-" * 90
    header = (
        f"  {'物件名':<{_NAME_W}} {'市':<{_CITY_W}} {'スコア':>7}  {'乖離率':>7}  {'判定'}  理由"
    )
    print(header)
    print(sep)

    promising_count = 0
    score_fail_only = 0
    rate_fail_only  = 0
    both_fail       = 0
    skip_count      = 0

    for listing in listings:
        name_short = listing.name[:20]
        city_code  = resolve_city_code(listing.location)

        if city_code is None:
            loc_hint = f" 所在地: {listing.location}" if debug else ""
            print(f"  {name_short:<{_NAME_W}} {'???':<{_CITY_W}} {'':>7}  {'':>7}  ?   (市コード判定不可){loc_hint}")
            skip_count += 1
            continue

        city_name = _CODE_TO_NAME.get(city_code, city_code)
        curve     = curve_cache.get(city_code)

        if curve is None:
            print(f"  {name_short:<{_NAME_W}} {city_name:<{_CITY_W}} {'':>7}  {'':>7}  ?   (カーブなし)")
            skip_count += 1
            continue

        candidate = suumo_to_candidate(listing)
        if candidate is None:
            print(f"  {name_short:<{_NAME_W}} {city_name:<{_CITY_W}} {'':>7}  {'':>7}  ?   (フィールド変換失敗)")
            skip_count += 1
            continue

        est     = estimate_resale(candidate, curve, CURRENT_YEAR, HOLD_YEARS)
        score   = est.resale_score
        vs_fair = est.asking_vs_fair_pct

        score_ok  = score >= PROMISING_SCORE_THRESHOLD
        rate_ok   = (vs_fair is None) or (vs_fair <= PROMISING_VS_FAIR_MAX_PCT)
        promising = score_ok and rate_ok

        if promising:
            mark   = "○"
            reason = ""
            promising_count += 1
        else:
            mark   = "×"
            reason = _fail_reason(score, vs_fair)
            if not score_ok and rate_ok:
                score_fail_only += 1
            elif score_ok and not rate_ok:
                rate_fail_only += 1
            else:
                both_fail += 1

        score_str = f"{score}/100"
        vs_str    = f"{vs_fair:+.1f}%" if vs_fair is not None else "N/A"
        loc_hint  = f"\n    所在地: {listing.location}" if debug else ""
        print(
            f"  {name_short:<{_NAME_W}} {city_name:<{_CITY_W}} {score_str:>7}  {vs_str:>7}  {mark}   {reason}{loc_hint}"
        )

    print(sep)
    total     = len(listings)
    evaluated = total - skip_count
    print(f"\n【集計】全{total}件（評価済{evaluated}件 / スキップ{skip_count}件）")
    print(f"  ○ 有望              : {promising_count} 件")
    print(f"  × スコア不足のみ    : {score_fail_only} 件   （スコア<{PROMISING_SCORE_THRESHOLD}）")
    print(f"  × 乖離率超過のみ    : {rate_fail_only} 件   （乖離率>+{PROMISING_VS_FAIR_MAX_PCT:.1f}%）")
    print(f"  × スコア＋乖離率両方: {both_fail} 件")
    print(f"\n有望判定の条件: スコア>={PROMISING_SCORE_THRESHOLD} かつ 乖離率<={PROMISING_VS_FAIR_MAX_PCT:+.1f}%")


# ---------------------------------------------------------------------------
# --with-detail 比較モードの表示ループ
# ---------------------------------------------------------------------------

def _run_with_detail(
    listings: list[Listing],
    curve_cache: dict,
    debug: bool,
) -> None:
    """
    詳細ページから総戸数・修繕積立金を取得し、スコアの変化を比較表示する。

    表示列:
        物件名 / 市 / 総戸数 / 修繕積立金(円/㎡) / スコア(なし→あり) / 乖離率 / 判定 / 理由

    アクセス数:
        detail_cache テーブルに「一度も登録されていない」物件数だけアクセスする。
        登録済み（取得失敗で NULL の場合も含む）は再取得しない。
    """

    # ---- 詳細取得フェーズ ----
    all_urls     = [l.url for l in listings]
    uncached_set = set(get_uncached_urls(all_urls))

    print(
        f"詳細ページ取得: 対象 {len(listings)}件 / "
        f"キャッシュ未登録 {len(uncached_set)}件 をフェッチします\n"
    )

    for listing in listings:
        if listing.url not in uncached_set:
            continue  # 登録済みはスキップ（重複 fetch 防止）
        data = fetch_detail(listing.url)  # 内部で4秒待機・timeout=15秒
        # 失敗（data=None）のときも「試み済み」として NULL 行を保存し次回の重複 fetch を防ぐ
        save_detail_cache(
            listing.url,
            data or {"total_units": None, "repair_fund_monthly": None},
        )

    # current 全件のキャッシュを DB から一括読み込み（新着は今保存、既知は以前保存）
    detail_cache_map = load_detail_cache(all_urls)
    print()

    # ---- 比較表示ヘッダー ----
    sep     = "-" * 110
    UNITS_W = 7   # 総戸数列幅   "32戸" / "不明"
    FUND_W  = 10  # 修繕積立金列 "341円/㎡" / "不明"
    SCORE_W = 16  # スコア変化列 "67→75(+8)" / "67→67"
    VS_W    = 8   # 乖離率列

    header = (
        f"  {'物件名':<{_NAME_W}} {'市':<{_CITY_W}}"
        f" {'総戸数':>{UNITS_W}}"
        f" {'修繕積立金':>{FUND_W}}"
        f" {'スコア変化':>{SCORE_W}}"
        f" {'乖離率':>{VS_W}}  判定  理由"
    )
    print(header)
    print(sep)

    detail_fetched  = 0  # 詳細データが取れた件数（NULL でない値が1つ以上ある）
    score_changed   = 0  # スコアが変化した件数
    newly_promising = 0  # 詳細なし× → 詳細あり○ になった件数
    skip_count      = 0

    for listing in listings:
        name_short = listing.name[:20]
        city_code  = resolve_city_code(listing.location)

        if city_code is None:
            loc_hint = f" 所在地: {listing.location}" if debug else ""
            print(
                f"  {name_short:<{_NAME_W}} {'???':<{_CITY_W}}"
                f" {'':>{UNITS_W}} {'':>{FUND_W}} {'':>{SCORE_W}} {'':>{VS_W}}"
                f"  ?   (市コード判定不可){loc_hint}"
            )
            skip_count += 1
            continue

        city_name = _CODE_TO_NAME.get(city_code, city_code)
        curve     = curve_cache.get(city_code)

        if curve is None:
            print(
                f"  {name_short:<{_NAME_W}} {city_name:<{_CITY_W}}"
                f" {'':>{UNITS_W}} {'':>{FUND_W}} {'':>{SCORE_W}} {'':>{VS_W}}"
                f"  ?   (カーブなし)"
            )
            skip_count += 1
            continue

        # ---- 詳細なし評価（ベースライン） ----
        cand_base = suumo_to_candidate(listing, detail=None)
        if cand_base is None:
            print(
                f"  {name_short:<{_NAME_W}} {city_name:<{_CITY_W}}"
                f" {'':>{UNITS_W}} {'':>{FUND_W}} {'':>{SCORE_W}} {'':>{VS_W}}"
                f"  ?   (フィールド変換失敗)"
            )
            skip_count += 1
            continue

        est_base   = estimate_resale(cand_base, curve, CURRENT_YEAR, HOLD_YEARS)
        score_base = est_base.resale_score
        # 乖離率は価格と市場カーブから算出するもので、総戸数・修繕積立金とは無関係
        vs_fair    = est_base.asking_vs_fair_pct

        # ---- 詳細あり評価 ----
        detail = detail_cache_map.get(listing.url)

        # NULL でない値が1件でもあれば「詳細データあり」とカウント
        has_data = detail is not None and (
            detail.get("total_units") is not None
            or detail.get("repair_fund_monthly") is not None
        )
        if has_data:
            detail_fetched += 1

        cand_detail  = suumo_to_candidate(listing, detail=detail)
        est_detail   = estimate_resale(cand_detail, curve, CURRENT_YEAR, HOLD_YEARS)
        score_detail = est_detail.resale_score

        # ---- 各列の文字列を組み立て ----
        if detail is not None:
            total_units    = detail.get("total_units")
            repair_monthly = detail.get("repair_fund_monthly")
            area_sqm       = cand_detail.area_sqm
            repair_per_sqm = (
                repair_monthly / area_sqm
                if (repair_monthly is not None and area_sqm)
                else None
            )
            units_str = f"{total_units}戸"          if total_units    is not None else "不明"
            fund_str  = f"{repair_per_sqm:.0f}円/㎡" if repair_per_sqm is not None else "不明"
        else:
            units_str = "未取得"
            fund_str  = "未取得"

        diff      = score_detail - score_base
        if diff != 0:
            score_changed += 1
        score_str = (
            f"{score_base}→{score_detail}({diff:+d})"
            if diff != 0
            else f"{score_base}→{score_detail}"
        )

        vs_str = f"{vs_fair:+.1f}%" if vs_fair is not None else "N/A"

        was_promising = _is_promising(score_base,   vs_fair)
        is_promis     = _is_promising(score_detail, vs_fair)

        if is_promis and not was_promising:
            newly_promising += 1
            mark = "○★"  # 詳細なし× → 詳細あり○（新たに有望判定）
        elif is_promis:
            mark = "○"
        else:
            mark = "×"

        reason   = "" if is_promis else _fail_reason(score_detail, vs_fair)
        loc_hint = f"\n    所在地: {listing.location}" if debug else ""

        print(
            f"  {name_short:<{_NAME_W}} {city_name:<{_CITY_W}}"
            f" {units_str:>{UNITS_W}}"
            f" {fund_str:>{FUND_W}}"
            f" {score_str:>{SCORE_W}}"
            f" {vs_str:>{VS_W}}  {mark}   {reason}{loc_hint}"
        )

    print(sep)

    total     = len(listings)
    evaluated = total - skip_count
    print(f"\n【詳細あり評価 集計】全{total}件（評価済{evaluated}件 / スキップ{skip_count}件）")
    print(f"  詳細データが取れた件数     : {detail_fetched}件")
    print(f"  スコアが変化した件数       : {score_changed}件")
    print(f"  新たに有望○★ になった件数 : {newly_promising}件")
    print(f"\n有望判定の条件: スコア>={PROMISING_SCORE_THRESHOLD} かつ 乖離率<={PROMISING_VS_FAIR_MAX_PCT:+.1f}%")
    if newly_promising > 0:
        print("  ★ = 詳細なし評価では × だったが、詳細ありで ○ になった物件")


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------

def main(
    force_refresh: bool = False,
    from_csv:      bool = False,
    debug:         bool = False,
    with_detail:   bool = False,
) -> None:
    print(f"=== 評価内訳確認 ({datetime.date.today()}) ===\n")

    # ---- 物件収集 ----
    if from_csv:
        listings = _load_from_csv()
        print(f"data.csv から読み込み: {len(listings)} 件\n")
    else:
        print("SUUMO から物件を収集中...", flush=True)
        raw      = scrape(TARGET_URL)
        listings = apply_filters(raw)
        print(f"\nフィルタ後: {len(listings)} 件\n")

    if not listings:
        print("物件が取得できませんでした。セレクタを確認してください。")
        return

    # ---- エリアごとにカーブを1回だけ取得（物件数に比例しない）----
    print("減価カーブを読み込み中...")
    curve_cache: dict[str, object] = {}
    for city_name, city_code in TARGET_AREAS.items():
        curve  = get_curve(city_name=city_name, city_code=city_code, force_refresh=force_refresh)
        curve_cache[city_code] = curve
        status = "OK（キャッシュ or API取得）" if curve else "取得失敗（評価スキップ）"
        print(f"  [{city_name} ({city_code})]: {status}")
    print()

    # ---- モード分岐 ----
    if with_detail:
        _run_with_detail(listings, curve_cache, debug)
    else:
        _run_normal(listings, curve_cache, debug)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="SUUMO現在物件の評価内訳を表示する（読み取り専用）"
    )
    parser.add_argument(
        "--from-csv",
        action="store_true",
        help="data.csv の既知物件を使う（スクレイプしない。すぐ結果が出る）",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="キャッシュを無視して国交省APIからカーブを再取得する",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="所在地文字列を表示する（市コード判定不可の原因調査用）",
    )
    parser.add_argument(
        "--with-detail",
        action="store_true",
        help=(
            "詳細ページから総戸数・修繕積立金を取得してスコア変化を比較表示する。"
            "アクセス数は detail_cache 未登録の物件数のみ。"
        ),
    )
    args = parser.parse_args()
    main(
        force_refresh=args.force_refresh,
        from_csv=args.from_csv,
        debug=args.debug,
        with_detail=args.with_detail,
    )

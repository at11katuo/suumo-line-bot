"""
build_curves.py
====================
検討エリアの中古マンション「減価カーブ」を国交省XIT001 APIから取得し、
JSON ファイルにキャッシュする。

【手動実行の使い方】
    # 通常実行（キャッシュが90日以内なら再取得しない）
    python build_curves.py

    # キャッシュを無視して強制再取得
    python build_curves.py --force-refresh

    # APIキー不要のモックモード（開発・テスト用）
    USE_MOCK_REINFOLIB=1 python build_curves.py

【必要な環境変数】
    REINFOLIB_API_KEY   — 国交省「不動産情報ライブラリ」のAPIキー
                          USE_MOCK_REINFOLIB=1 のときは不要

【市区町村コードの確認方法】
    XIT002 API で東京都(13)のコード一覧を取得して確認できる。
    curl "https://www.reinfolib.mlit.go.jp/ex-api/external/XIT002?area=13" \
         -H "Ocp-Apim-Subscription-Key: $REINFOLIB_API_KEY"
"""

from __future__ import annotations

import json
import logging
import os
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from reinfolib_resale import (
    AGE_BUCKETS,
    DepreciationCurve,
    ReinfolibClient,
    Trade,
    build_depreciation_curve,
    normalize,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

# 検討対象エリアと市区町村コード（全国地方公共団体コードの上5桁）
# ※まず調布市1件で動作確認し、後で府中・稲城・多摩を追加する
TARGET_AREAS: dict[str, str] = {
    "調布市": "13208",
    "府中市": "13206",
    "稲城市": "13225",
    # "多摩市": "13224",  # 後で追加
}

# 取得対象年（直近4年）。コロナ禍（2020〜2021）の市況異常期を避けた鮮度と
# サンプル数のバランス。
FETCH_START_YEAR = 2022
FETCH_END_YEAR   = 2025

# キャッシュ有効期限（国交省データは四半期更新 ≒ 90日）
CACHE_TTL_DAYS = 90

# キャッシュの保存先ディレクトリ（.gitignore で追跡対象外）
CACHE_DIR = Path(__file__).parent / "cache"

# 地区単位カーブのサンプル数閾値。build_depreciation_curve の
# デフォルト min_samples=3 では中央値が外れ値1件で大きく動きうるため、
# より高めの値にする。実データ（3市合計）で閾値ごとの充足状況を確認した結果:
#   3件以上=294 / 5件以上=234 / 8件以上=167 / 10件以上=146 / 15件以上=98 組み合わせ
# 統計的な安定性とカバレッジのバランスから8件を採用する
# （check_district_sample_distribution.py で実データ確認済み）。
DISTRICT_MIN_SAMPLES = 8


# ---------------------------------------------------------------------------
# モックデータ生成（USE_MOCK_REINFOLIB=1 のとき API の代わりに使う）
# ---------------------------------------------------------------------------

def _make_mock_trades(city_code: str, start_year: int, end_year: int) -> list[dict]:
    """
    APIキー不要のダミー取引データを生成する。
    reinfolib_resale.py の __main__ ブロックと同じ減価ロジックを使用。
    seed を固定しているので何度実行しても同じデータが生成される。
    """
    random.seed(42)  # 再現性のため固定シード
    rows: list[dict] = []
    base_unit = 900_000  # 新築時の基準㎡単価（円）

    for year in range(start_year, end_year + 1):
        for _ in range(50):  # 年間50件 × 4年 = 200件
            b_year = random.randint(1990, year - 1)  # 建築年 < 取引年
            age = year - b_year
            # 築年数に応じて年1.5%の減価。±10%のばらつきを加える。
            unit = base_unit * (0.985 ** age) * random.uniform(0.9, 1.1)
            rows.append({
                "Type":          "中古マンション等",
                "TradePrice":    int(unit * 70),    # 総額（70㎡想定）
                "Area":          "70",
                "UnitPrice":     int(unit),
                "BuildingYear":  f"{b_year}年",
                "Period":        f"{year}年第2四半期",
                "FloorPlan":     "3LDK",
                "Municipality":  f"市区町村{city_code}",
                "DistrictName":  "テスト町",
            })
    return rows


# ---------------------------------------------------------------------------
# 地区単位カーブ
# ---------------------------------------------------------------------------

@dataclass
class CurveBundle:
    """
    市単位カーブと地区単位カーブ群をまとめて持つ。

    地区単位カーブは、市単位カーブの生成に使ったものと同じ取引データ
    （trades）を地区(district)ごとに再集計するだけで作る。地区単位カーブの
    ために国交省APIを追加で呼び出すことはない。
    """
    city_curve: DepreciationCurve
    district_curves: dict[str, DepreciationCurve] = field(default_factory=dict)


def _build_district_curves(
    trades: list[Trade], min_samples: int = DISTRICT_MIN_SAMPLES,
) -> dict[str, DepreciationCurve]:
    """
    取引データを地区(district)ごとにグルーピングし、それぞれについて
    build_depreciation_curve を実行する。

    地区名が空の取引（district=""）は除外する。生成したカーブが
    1バケットも埋まらなかった地区（サンプル数が全バケットで
    min_samples未満）は結果に含めない（district_curves に存在しない
    ＝常に市単位カーブへフォールバックする、という扱いになる）。
    """
    by_district: dict[str, list[Trade]] = {}
    for t in trades:
        if not t.district:
            continue
        by_district.setdefault(t.district, []).append(t)

    result: dict[str, DepreciationCurve] = {}
    for district, district_trades in by_district.items():
        curve = build_depreciation_curve(district_trades, min_samples=min_samples)
        if curve.median_unit_price:
            result[district] = curve
    return result


# ---------------------------------------------------------------------------
# JSON シリアライズ / デシリアライズ
# ---------------------------------------------------------------------------

def _curve_to_dict(curve: DepreciationCurve) -> dict:
    """
    DepreciationCurve を JSON に保存できる dict に変換する。
    tuple キー (0, 5) → 文字列 "0-5" に変換する。
    """
    return {
        "median_unit_price": {
            f"{lo}-{hi}": v
            for (lo, hi), v in curve.median_unit_price.items()
        },
        "sample_count": {
            f"{lo}-{hi}": n
            for (lo, hi), n in curve.sample_count.items()
        },
    }


def _dict_to_curve(d: dict) -> DepreciationCurve:
    """
    JSON から読み戻した dict を DepreciationCurve に復元する。
    文字列 "0-5" → tuple キー (0, 5) に戻す。
    """
    def parse_key(s: str) -> tuple[int, int]:
        # "0-5" → (0, 5) / "41-200" → (41, 200)
        lo, hi = s.split("-")
        return (int(lo), int(hi))

    return DepreciationCurve(
        median_unit_price={parse_key(k): float(v) for k, v in d["median_unit_price"].items()},
        sample_count={parse_key(k): int(n)         for k, n in d["sample_count"].items()},
    )


# ---------------------------------------------------------------------------
# キャッシュ操作
# ---------------------------------------------------------------------------

def _cache_path(city_code: str, start_year: int, end_year: int) -> Path:
    """キャッシュファイルのパスを返す。エリア・年範囲ごとに別ファイルになる。"""
    return CACHE_DIR / f"curve_{city_code}_{start_year}_{end_year}.json"


def _load_cache(path: Path) -> Optional[DepreciationCurve]:
    """
    キャッシュファイルを読み込む。
    - ファイルが存在しない → None
    - 有効期限（CACHE_TTL_DAYS）を超えている → None
    - 有効期限内 → DepreciationCurve を返す
    """
    if not path.exists():
        return None

    with path.open(encoding="utf-8") as f:
        data = json.load(f)

    fetched_at = datetime.fromisoformat(data["fetched_at"])
    age_days = (datetime.now() - fetched_at).days
    if age_days >= CACHE_TTL_DAYS:
        logger.info(
            "キャッシュ期限切れのため無視します（%d日経過 / 有効期限%d日）: %s",
            age_days, CACHE_TTL_DAYS, path.name,
        )
        return None

    logger.info(
        "キャッシュを使用します: %s（取得日: %s、%d日経過）",
        path.name, fetched_at.date(), age_days,
    )
    return _dict_to_curve(data["curve"])


def _save_cache(
    path: Path,
    city_code: str,
    city_name: str,
    start_year: int,
    end_year: int,
    trade_count: int,
    curve: DepreciationCurve,
) -> None:
    """減価カーブを JSON ファイルに保存する。"""
    # ディレクトリが無ければ作る（.gitignore に追加済みのため git 追跡されない）
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "fetched_at":  datetime.now().isoformat(timespec="seconds"),
        "city_code":   city_code,
        "city_name":   city_name,
        "start_year":  start_year,
        "end_year":    end_year,
        "trade_count": trade_count,
        "curve":       _curve_to_dict(curve),
    }
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    logger.info("キャッシュ保存: %s（取引数: %d件）", path.name, trade_count)


# ---------------------------------------------------------------------------
# 地区単位カーブ込みキャッシュ（CurveBundle）
# ---------------------------------------------------------------------------
# 市単位カーブ専用の _cache_path/_load_cache/_save_cache とは別に、
# 市単位＋地区単位をまとめて1ファイルにキャッシュする。
# 1回の get_curve_bundle 呼び出しで作られるキャッシュファイルが1つになる
# ようにするため（市単位・地区単位を別ファイルにすると、キャッシュの
# 有効期限が別々にずれてtradesの再取得タイミングが噛み合わなくなる）。

def _bundle_cache_path(city_code: str, start_year: int, end_year: int) -> Path:
    """地区単位カーブ込みキャッシュファイルのパスを返す。"""
    return CACHE_DIR / f"curve_bundle_{city_code}_{start_year}_{end_year}.json"


def _curve_bundle_to_dict(bundle: CurveBundle) -> dict:
    """CurveBundle を JSON に保存できる dict に変換する（既存の _curve_to_dict を再利用）。"""
    return {
        "city_curve": _curve_to_dict(bundle.city_curve),
        "district_curves": {
            district: _curve_to_dict(curve)
            for district, curve in bundle.district_curves.items()
        },
    }


def _dict_to_curve_bundle(d: dict) -> CurveBundle:
    """JSON から読み戻した dict を CurveBundle に復元する（既存の _dict_to_curve を再利用）。"""
    return CurveBundle(
        city_curve=_dict_to_curve(d["city_curve"]),
        district_curves={
            district: _dict_to_curve(curve_d)
            for district, curve_d in d["district_curves"].items()
        },
    )


def _load_bundle_cache(path: Path) -> Optional[CurveBundle]:
    """
    キャッシュファイルを読み込む。ロジックは _load_cache と同じ
    （存在チェック・TTLチェック）。
    """
    if not path.exists():
        return None

    with path.open(encoding="utf-8") as f:
        data = json.load(f)

    fetched_at = datetime.fromisoformat(data["fetched_at"])
    age_days = (datetime.now() - fetched_at).days
    if age_days >= CACHE_TTL_DAYS:
        logger.info(
            "キャッシュ期限切れのため無視します（%d日経過 / 有効期限%d日）: %s",
            age_days, CACHE_TTL_DAYS, path.name,
        )
        return None

    logger.info(
        "キャッシュを使用します: %s（取得日: %s、%d日経過）",
        path.name, fetched_at.date(), age_days,
    )
    return _dict_to_curve_bundle(data["bundle"])


def _save_bundle_cache(
    path: Path,
    city_code: str,
    city_name: str,
    start_year: int,
    end_year: int,
    trade_count: int,
    bundle: CurveBundle,
) -> None:
    """市単位カーブ＋地区単位カーブ群を JSON ファイルに保存する。"""
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "fetched_at":  datetime.now().isoformat(timespec="seconds"),
        "city_code":   city_code,
        "city_name":   city_name,
        "start_year":  start_year,
        "end_year":    end_year,
        "trade_count": trade_count,
        "bundle":      _curve_bundle_to_dict(bundle),
    }
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    logger.info(
        "キャッシュ保存: %s（取引数: %d件、地区カーブ%d件）",
        path.name, trade_count, len(bundle.district_curves),
    )


# ---------------------------------------------------------------------------
# メイン関数（ライブラリとしても使える）
# ---------------------------------------------------------------------------

def get_curve_bundle(
    city_name: str,
    city_code: str,
    start_year: int = FETCH_START_YEAR,
    end_year: int   = FETCH_END_YEAR,
    force_refresh: bool = False,
) -> Optional[CurveBundle]:
    """
    指定エリアの市単位カーブ＋地区単位カーブ群をまとめて返す。

    - キャッシュが有効期限（90日）以内なら API を叩かずキャッシュを返す
    - force_refresh=True のときはキャッシュを無視して再取得する
    - 環境変数 USE_MOCK_REINFOLIB=1 のとき、API を叩かずモックデータを使う
    - 地区単位カーブは、市単位カーブと同じ取引データ（trades）を地区ごとに
      再集計するだけで作る。地区単位カーブのために API を追加で呼ぶことはない。

    引数:
        city_name     : 市区町村名（ログ用）
        city_code     : 全国地方公共団体コード上5桁（例: "13208"）
        start_year    : 取引データの取得開始年
        end_year      : 取引データの取得終了年
        force_refresh : True のとき有効期限内のキャッシュも無視する

    戻り値:
        CurveBundle（カーブ生成に失敗した場合は None）
    """
    path = _bundle_cache_path(city_code, start_year, end_year)

    # --- キャッシュチェック ---
    if not force_refresh:
        cached = _load_bundle_cache(path)
        if cached is not None:
            return cached
    else:
        logger.info("--force-refresh: キャッシュを無視して再取得します。")

    # --- データ取得（モック or 実API）---
    use_mock = os.environ.get("USE_MOCK_REINFOLIB", "").strip() == "1"

    if use_mock:
        logger.info("[モック] %s（%s）のダミーデータを生成中...", city_name, city_code)
        raw_rows = _make_mock_trades(city_code, start_year, end_year)
    else:
        api_key = os.environ.get("REINFOLIB_API_KEY", "").strip()
        if not api_key:
            logger.error(
                "REINFOLIB_API_KEY が未設定です。"
                "実APIを使う場合は環境変数を設定してください。"
                "開発中は USE_MOCK_REINFOLIB=1 も使えます。"
            )
            return None
        logger.info(
            "%s（%s）の取引データを国交省APIから取得中（%d〜%d年）...",
            city_name, city_code, start_year, end_year,
        )
        client = ReinfolibClient(api_key=api_key)
        raw_rows = client.fetch_trades_multi_year(
            city_code=city_code,
            start_year=start_year,
            end_year=end_year,
        )

    # --- 正規化 → 市単位カーブ生成 ---
    trades = normalize(raw_rows)
    if not trades:
        logger.warning("%s: 正規化後の取引データが0件でした。カーブを生成できません。", city_name)
        return None

    city_curve = build_depreciation_curve(trades)
    if not city_curve.median_unit_price:
        logger.warning(
            "%s: サンプル数不足でバケットが埋まりませんでした（取引数: %d件）。",
            city_name, len(trades),
        )
        return None

    # --- 地区単位カーブ生成（同じ trades から再集計。追加のAPI呼び出しなし）---
    district_curves = _build_district_curves(trades)
    logger.info(
        "%s: 地区単位カーブ %d 地区分を生成しました（サンプル数%d件以上の(地区,築年数バケット)のみ）。",
        city_name, len(district_curves), DISTRICT_MIN_SAMPLES,
    )

    bundle = CurveBundle(city_curve=city_curve, district_curves=district_curves)
    _save_bundle_cache(path, city_code, city_name, start_year, end_year, len(trades), bundle)
    return bundle


def get_curve(
    city_name: str,
    city_code: str,
    start_year: int = FETCH_START_YEAR,
    end_year: int   = FETCH_END_YEAR,
    force_refresh: bool = False,
) -> Optional[DepreciationCurve]:
    """
    指定エリアの市単位減価カーブを返す（従来からの呼び出し方をそのまま
    維持するための互換関数）。

    内部では get_curve_bundle を呼び、市単位カーブ部分だけを取り出して
    返す。地区単位カーブも同じ取引データから同時に生成・キャッシュされる
    が、この関数の戻り値には含まれない（地区単位カーブが必要な場合は
    get_curve_bundle を直接使うこと）。
    """
    bundle = get_curve_bundle(city_name, city_code, start_year, end_year, force_refresh)
    return bundle.city_curve if bundle else None


# ---------------------------------------------------------------------------
# エントリポイント（python build_curves.py で直接実行する場合）
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="検討エリアの中古マンション減価カーブを生成・キャッシュする"
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="キャッシュを無視して再取得する（APIキー確認時などに使う）",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    for city_name, city_code in TARGET_AREAS.items():
        print(f"\n>>> {city_name}（{city_code}）の減価カーブを生成中...")
        curve = get_curve(city_name, city_code, force_refresh=args.force_refresh)
        if curve is None:
            print(f"    [失敗] カーブを生成できませんでした。")
            continue

        print(f"=== {city_name} 築年数別 実勢㎡単価（中央値）===")
        for b in AGE_BUCKETS:
            if b in curve.median_unit_price:
                print(
                    f"  築{b[0]:>2}-{b[1]:>2}年: "
                    f"{curve.median_unit_price[b]:>10,.0f} 円/㎡"
                    f"  (n={curve.sample_count[b]})"
                )

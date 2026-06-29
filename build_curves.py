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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from reinfolib_resale import (
    AGE_BUCKETS,
    DepreciationCurve,
    ReinfolibClient,
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
# メイン関数（ライブラリとしても使える）
# ---------------------------------------------------------------------------

def get_curve(
    city_name: str,
    city_code: str,
    start_year: int = FETCH_START_YEAR,
    end_year: int   = FETCH_END_YEAR,
    force_refresh: bool = False,
) -> Optional[DepreciationCurve]:
    """
    指定エリアの減価カーブを返す。

    - キャッシュが有効期限（90日）以内なら API を叩かずキャッシュを返す
    - force_refresh=True のときはキャッシュを無視して再取得する
    - 環境変数 USE_MOCK_REINFOLIB=1 のとき、API を叩かずモックデータを使う

    引数:
        city_name     : 市区町村名（ログ用）
        city_code     : 全国地方公共団体コード上5桁（例: "13208"）
        start_year    : 取引データの取得開始年
        end_year      : 取引データの取得終了年
        force_refresh : True のとき有効期限内のキャッシュも無視する

    戻り値:
        DepreciationCurve（カーブ生成に失敗した場合は None）
    """
    path = _cache_path(city_code, start_year, end_year)

    # --- キャッシュチェック ---
    if not force_refresh:
        cached = _load_cache(path)
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

    # --- 正規化 → カーブ生成 ---
    trades = normalize(raw_rows)
    if not trades:
        logger.warning("%s: 正規化後の取引データが0件でした。カーブを生成できません。", city_name)
        return None

    curve = build_depreciation_curve(trades)
    if not curve.median_unit_price:
        logger.warning(
            "%s: サンプル数不足でバケットが埋まりませんでした（取引数: %d件）。",
            city_name, len(trades),
        )
        return None

    _save_cache(path, city_code, city_name, start_year, end_year, len(trades), curve)
    return curve


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

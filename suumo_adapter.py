"""
suumo_adapter.py
SUUMO スクレイピング結果（Listing）を reinfolib_resale.Candidate に変換するアダプタ。

使い方:
    from suumo_adapter import suumo_to_candidate
    candidate = suumo_to_candidate(listing)
    if candidate is None:
        # 必須フィールドが取れなかった物件はスキップ
        continue
"""

import logging
import re
from typing import Optional

from scraper import Listing
from reinfolib_resale import Candidate

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# パース関数（各フィールドの文字列 → Pythonの型）
# ---------------------------------------------------------------------------

def _parse_price(price_str: str) -> Optional[float]:
    """
    SUUMO の価格文字列を円(float)に変換する。

    対応パターン:
      "4,200万円"   → 42_000_000
      "5500万円"    → 55_000_000  （カンマなし）
      "1億2000万円" → 120_000_000 （億＋万）
      "1億円"       → 100_000_000 （億のみ）
    パース失敗（"価格未定" / 空文字など）は None を返す。
    """
    if not price_str:
        return None

    # カンマを除いてから数値を探す
    s = price_str.replace(",", "")

    oku = 0.0  # 億の部分（例: "1億" → 100_000_000）
    man = 0.0  # 万の部分（例: "4200万" → 42_000_000）

    m_oku = re.search(r'([\d.]+)億', s)
    if m_oku:
        oku = float(m_oku.group(1)) * 1_0000_0000

    m_man = re.search(r'([\d.]+)万', s)
    if m_man:
        man = float(m_man.group(1)) * 10_000

    # どちらも 0 なら数値が読み取れなかった
    if oku == 0.0 and man == 0.0:
        return None

    return oku + man


def _parse_area(area_str: str) -> Optional[float]:
    """
    SUUMO の専有面積文字列を ㎡(float) に変換する。

    対応パターン:
      "72.5m²"  → 72.5
      "72.5㎡"  → 72.5  （全角㎡）
      "65m²"    → 65.0
    パース失敗は None を返す。
    """
    if not area_str:
        return None
    # 半角 m・全角ｍ・㎡ の前にある数値を取り出す
    m = re.search(r'([\d.]+)\s*[mｍ㎡]', area_str)
    return float(m.group(1)) if m else None


def _parse_building_year(age_str: str) -> Optional[int]:
    """
    SUUMO の築年月文字列から建築年（西暦）を取り出す。

    対応パターン:
      "2018年3月" → 2018
      "2005年築"  → 2005
    パース失敗は None を返す。
    """
    if not age_str:
        return None
    # 4桁の年号を取り出す
    m = re.search(r'(\d{4})\s*年', age_str)
    return int(m.group(1)) if m else None


def _parse_walk_minutes(station_str: str) -> Optional[int]:
    """
    SUUMO の沿線・駅文字列から徒歩分数を取り出す。

    対応パターン:
      "京王線 調布駅 徒歩6分"   → 6
      "JR線 立川駅 徒歩 10 分" → 10  （スペースあり）
    パース失敗は None を返す（任意フィールドなので None のまま Candidate に渡せる）。
    """
    if not station_str:
        return None
    m = re.search(r'徒歩\s*(\d+)\s*分', station_str)
    return int(m.group(1)) if m else None


def _extract_district(location: str, city_name: str) -> Optional[str]:
    """
    住所文字列から市名より後ろの部分を取り出し、末尾の丁目・番地
    （数字・ハイフン）を除去して地区名候補を作る。

    対応パターン:
      "東京都府中市紅葉丘２"     + "府中市" → "紅葉丘"
      "東京都稲城市矢野口"       + "稲城市" → "矢野口"
      "東京都調布市多摩川１－２" + "調布市" → "多摩川"

    ここで作るのは「候補」に過ぎない。国交省の地区名（DistrictName）と
    実際に一致するかどうかは呼び出し側（select_curve）が district_curves
    の存在チェックで判定する。ここでは表記ゆれの正規化のみ行い、
    一致するかどうかの判断はしない。

    市名が住所に含まれない、または市名の後に何も残らない場合は None。
    """
    if not location or not city_name:
        return None
    idx = location.find(city_name)
    if idx == -1:
        return None
    rest = location[idx + len(city_name):]
    # 末尾の丁目・番地（半角/全角数字、ハイフン類）を除去する。
    # ハイフン類は「－」(U+FF0D 全角ハイフンマイナス)・「−」(U+2212 マイナス
    # 記号)・「ー」(U+30FC 長音記号、番地表記で使われることがある)・半角
    # ハイフンの4種を対象にする（実際の住所表記で確認済みの組み合わせ）。
    district = re.sub(r'[\d0-9０-９\-－−ー]+$', '', rest).strip()
    return district or None


# ---------------------------------------------------------------------------
# 変換関数（公開インターフェース）
# ---------------------------------------------------------------------------

def suumo_to_candidate(
    listing: Listing,
    detail: Optional[dict] = None,
    city_name: Optional[str] = None,
) -> Optional[Candidate]:
    """
    SUUMO の Listing を reinfolib_resale の Candidate に変換して返す。

    asking_price / area_sqm / building_year のいずれかが取得できない場合は
    None を返す（バッチ処理でこの物件をスキップする合図）。
    取得できなかったフィールド名は logging.warning に記録する。

    引数:
        listing: SUUMO スクレイピング結果
        detail : detail_fetcher.fetch_detail() の戻り値（または None）。
                 {"total_units": int|None, "repair_fund_monthly": float|None} 形式。
                 None のとき（詳細未取得 or 取得失敗）は total_units / repair_fund_per_sqm が
                 中立（None）のまま → スコアに影響しない（フォールバック動作）。
        city_name : 地区名抽出に使う市名（例: "府中市"）。呼び出し側が既に
                 resolve_city_code 等で判定済みの市名をそのまま渡す想定。
                 None のときは district=None のまま（地区単位カーブは
                 使われず、常に市単位カーブにフォールバックする）。

    修繕積立金の㎡換算:
        詳細ページの値は月額総額（例: 24,080円/月）。
        estimate_resale の repair_fund_per_sqm は「月額 ÷ 専有面積（円/㎡/月）」を期待する。
        例: 24,080円 ÷ 70.6㎡ ≈ 341円/㎡ → 200円以上なので減点なし（健全）。
        換算はここで行う（area_sqm が確定している場所が最適）。
    """
    asking_price  = _parse_price(listing.price)
    area_sqm      = _parse_area(listing.area)
    building_year = _parse_building_year(listing.age)

    # 必須フィールドのチェック：1つでも取れなければスキップ
    missing = []
    if asking_price is None:
        missing.append(f"asking_price(元値={listing.price!r})")
    if area_sqm is None:
        missing.append(f"area_sqm(元値={listing.area!r})")
    if building_year is None:
        missing.append(f"building_year(元値={listing.age!r})")

    if missing:
        logger.warning(
            "suumo_to_candidate スキップ [%s]: 取得不可フィールド = %s",
            listing.name,
            ", ".join(missing),
        )
        return None

    # ---- 詳細データ（任意）→ total_units と repair_fund_per_sqm の算出 ----
    total_units = None
    repair_fund_per_sqm = None
    if detail:
        total_units = detail.get("total_units")
        repair_fund_monthly = detail.get("repair_fund_monthly")
        # 修繕積立金月額 ÷ 専有面積 → ㎡単価（円/㎡/月）に換算する。
        # 0除算ガード付き（area_sqm は必須フィールドなのでここでは None にならない）。
        if repair_fund_monthly is not None and area_sqm:
            repair_fund_per_sqm = repair_fund_monthly / area_sqm

    district = _extract_district(listing.location, city_name) if city_name else None

    return Candidate(
        asking_price=asking_price,
        area_sqm=area_sqm,
        building_year=building_year,
        walk_minutes=_parse_walk_minutes(listing.station),
        total_units=total_units,
        repair_fund_per_sqm=repair_fund_per_sqm,
        district=district,
        floor_plan=listing.floor_plan or "",
    )

"""
reinfolib_resale.py
====================
国土交通省「不動産情報ライブラリ」XIT001 API を使って、
中古マンションの成約・取引価格から「ヤドカリ戦法」向けの出口価値を推定するモジュール。

想定ユース:
  SUUMO で拾った候補物件（エリア / 専有面積 / 築年 / 売出価格）を渡すと、
  - そのエリアの中古マンションの実勢㎡単価（築年数別）
  - N年保有後に売却したときの想定売却額
  - 売出価格に対する割安/割高
  - ヤドカリ向け「売りやすさスコア」
  を返す。

依存: requests のみ（pandas 不要）
APIキー: 環境変数 REINFOLIB_API_KEY に格納（.env 運用と同じ流儀）
"""

from __future__ import annotations

import os
import re
import time
import statistics
from dataclasses import dataclass, field
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# 1. API クライアント
# ---------------------------------------------------------------------------

ENDPOINT = "https://www.reinfolib.mlit.go.jp/ex-api/external/XIT001"

# priceClassification: 01=取引価格情報のみ / 02=成約価格情報のみ / 未指定=両方
# 出口価格の推定には成約ベースのほうが実態に近いが、件数が少ないので
# デフォルトは両方取得して後段でフィルタする。
DEFAULT_REQUEST_INTERVAL_SEC = 1.0  # API は連続実行を避けるよう案内されている


class ReinfolibClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        request_interval_sec: float = DEFAULT_REQUEST_INTERVAL_SEC,
    ):
        self.api_key = api_key or os.environ.get("REINFOLIB_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "APIキーがありません。環境変数 REINFOLIB_API_KEY を設定してください。"
            )
        self.interval = request_interval_sec
        self._last_call = 0.0

    def _throttle(self):
        elapsed = time.time() - self._last_call
        if elapsed < self.interval:
            time.sleep(self.interval - elapsed)
        self._last_call = time.time()

    def fetch_trades(
        self,
        year: int,
        city_code: str,
        quarter: Optional[int] = None,
        price_classification: Optional[str] = None,
        language: str = "ja",
    ) -> list[dict]:
        """XIT001 を叩いて取引/成約データの生レコードを返す。

        city_code: 全国地方公共団体コードの上5桁（例: 13208 = 立川市）
        """
        params = {"year": year, "city": city_code, "language": language}
        if quarter is not None:
            params["quarter"] = quarter
        if price_classification is not None:
            params["priceClassification"] = price_classification

        self._throttle()
        # requests は Accept-Encoding: gzip を自動付与し、自動で解凍する
        resp = requests.get(
            ENDPOINT,
            params=params,
            headers={"Ocp-Apim-Subscription-Key": self.api_key},
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        # XIT001 は {"status": "OK", "data": [...]} 形式
        return payload.get("data", [])

    def fetch_trades_multi_year(
        self,
        city_code: str,
        start_year: int,
        end_year: int,
        **kwargs,
    ) -> list[dict]:
        """複数年分まとめて取得。"""
        rows: list[dict] = []
        for y in range(start_year, end_year + 1):
            rows.extend(self.fetch_trades(year=y, city_code=city_code, **kwargs))
        return rows


# ---------------------------------------------------------------------------
# 2. レコードの正規化
# ---------------------------------------------------------------------------

# ヤドカリの出口は「住みたいファミリー」。中古マンションの実需取引だけを見る。
MANSION_TYPES = {"中古マンション等"}


@dataclass
class Trade:
    price: float          # 取引総額（円）
    area_sqm: float       # 専有面積（㎡）
    unit_price: float     # ㎡単価（円/㎡）
    building_year: Optional[int]   # 建築年（西暦）
    trade_year: Optional[int]      # 取引時点（西暦）
    age_at_trade: Optional[int]    # 取引時点の築年数
    floor_plan: str
    municipality: str
    district: str


_WAREKI = {"令和": 2018, "平成": 1988, "昭和": 1925}  # 元号元年-1


def _parse_year(s: str) -> Optional[int]:
    """'1972年' '令和3年' '2015年第2四半期' などから西暦を取り出す。"""
    if not s:
        return None
    for era, base in _WAREKI.items():
        m = re.search(era + r"(\d+)", s)
        if m:
            return base + int(m.group(1))
    m = re.search(r"(\d{4})", s)
    return int(m.group(1)) if m else None


def _to_float(s) -> Optional[float]:
    if s is None or s == "":
        return None
    try:
        return float(str(s).replace(",", ""))
    except ValueError:
        return None


def normalize(raw_rows: list[dict], only_mansion: bool = True) -> list[Trade]:
    out: list[Trade] = []
    for r in raw_rows:
        if only_mansion and r.get("Type") not in MANSION_TYPES:
            continue
        price = _to_float(r.get("TradePrice"))
        area = _to_float(r.get("Area"))
        if not price or not area:
            continue
        unit = _to_float(r.get("UnitPrice")) or (price / area)
        b_year = _parse_year(r.get("BuildingYear", ""))
        t_year = _parse_year(r.get("Period", ""))
        age = (t_year - b_year) if (b_year and t_year) else None
        out.append(
            Trade(
                price=price,
                area_sqm=area,
                unit_price=unit,
                building_year=b_year,
                trade_year=t_year,
                age_at_trade=age,
                floor_plan=r.get("FloorPlan", "") or "",
                municipality=r.get("Municipality", "") or "",
                district=r.get("DistrictName", "") or "",
            )
        )
    return out


# ---------------------------------------------------------------------------
# 3. 築年数別の減価カーブ
# ---------------------------------------------------------------------------

# 築年数を5年刻みのバケットに分け、各バケットの㎡単価中央値を出す。
# 回帰だとサンプルが少ないとブレるので、説明しやすい中央値ベースにする。
AGE_BUCKETS = [(0, 5), (6, 10), (11, 15), (16, 20), (21, 25), (26, 30), (31, 40), (41, 200)]


def _bucket_of(age: int) -> tuple[int, int]:
    for lo, hi in AGE_BUCKETS:
        if lo <= age <= hi:
            return (lo, hi)
    return AGE_BUCKETS[-1]


@dataclass
class DepreciationCurve:
    # バケット -> ㎡単価中央値
    median_unit_price: dict[tuple[int, int], float] = field(default_factory=dict)
    sample_count: dict[tuple[int, int], int] = field(default_factory=dict)

    def unit_price_for_age(self, age: int) -> Optional[float]:
        """指定築年数の想定㎡単価。バケットに実データがあればそれを、
        無ければ隣接バケットから補間する。"""
        b = _bucket_of(age)
        if b in self.median_unit_price:
            return self.median_unit_price[b]
        known = sorted(self.median_unit_price.items(), key=lambda kv: kv[0][0])
        if not known:
            return None
        if age <= known[0][0][0]:
            return known[0][1]
        if age >= known[-1][0][1]:
            return known[-1][1]
        return statistics.mean(v for _, v in known)


def build_depreciation_curve(
    trades: list[Trade], min_samples: int = 3
) -> DepreciationCurve:
    buckets: dict[tuple[int, int], list[float]] = {}
    for t in trades:
        if t.age_at_trade is None or t.age_at_trade < 0:
            continue
        b = _bucket_of(t.age_at_trade)
        buckets.setdefault(b, []).append(t.unit_price)

    curve = DepreciationCurve()
    for b, prices in buckets.items():
        if len(prices) >= min_samples:
            curve.median_unit_price[b] = statistics.median(prices)
            curve.sample_count[b] = len(prices)
    return curve


def select_curve(
    district: Optional[str],
    city_curve: DepreciationCurve,
    district_curves: dict[str, DepreciationCurve],
    current_age: int,
    city_name: str,
) -> tuple[DepreciationCurve, str]:
    """
    地区単位カーブに十分なサンプルがあればそれを、なければ市単位カーブに
    フォールバックして使う。どちらを使ったかを表す説明文字列も一緒に返す
    （評価結果・ログに残して透明性を確保するため）。

    判定は「物件の現在の築年数バケット」1つだけで行う（現在価値と将来価値の
    両方に同じカーブを使う。current_ageとfuture_ageで別々のカーブを混在させると
    説明が複雑になり、透明性という目的に反するため）。

    地区カーブが選ばれるのは、district_curves[district] が存在し、かつ
    そのカーブの median_unit_price に現在の築年数バケットが実在するとき
    （＝build_depreciation_curveの時点でサンプル数閾値を満たしたバケット）
    だけ。サンプル不足のバケットは build_depreciation_curve が最初から
    含めないため、ここで閾値を再チェックする必要はない。

    地区名が不明（None）・地区カーブが存在しない・該当バケットのサンプルが
    不足している、いずれの場合も例外を投げず市単位カーブへ安全に
    フォールバックする。
    """
    if district:
        district_curve = district_curves.get(district)
        if district_curve is not None:
            bucket = _bucket_of(current_age)
            if bucket in district_curve.median_unit_price:
                n = district_curve.sample_count.get(bucket, 0)
                return district_curve, f"district:{district}(n={n})"
    return city_curve, f"city:{city_name}"


# ---------------------------------------------------------------------------
# 4. 候補物件の評価（ヤドカリ向け）
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    asking_price: float    # 売出価格（円）
    area_sqm: float        # 専有面積（㎡）
    building_year: int     # 建築年（西暦）
    walk_minutes: Optional[int] = None       # 駅徒歩（分）
    total_units: Optional[int] = None         # 総戸数
    repair_fund_per_sqm: Optional[float] = None  # 修繕積立金 円/㎡・月
    floor_plan: str = ""
    district: Optional[str] = None  # 地区名（国交省DistrictNameとの完全一致用。不明ならNone）


@dataclass
class ResaleEstimate:
    current_fair_unit_price: Optional[float]
    current_fair_price: Optional[float]
    asking_vs_fair_pct: Optional[float]      # +なら割高、-なら割安
    future_unit_price: Optional[float]
    future_resale_price: Optional[float]
    net_after_tax_and_cost: Optional[float]  # 諸費用・譲渡税を引いた手取り見込み
    resale_score: int                        # 0-100 売りやすさ
    notes: list[str] = field(default_factory=list)
    # どのカーブを使って評価したか（例: "district:紅葉丘(n=19)" / "city:府中市"）。
    # 呼び出し側（select_curve）が決めた説明文字列をそのまま記録するだけで、
    # このモジュール自身は地区/市の判定ロジックを持たない。
    curve_source: str = ""
    # 評価基準年時点での築年数（current_year - building_year）。
    # 通知側で「現在築○年」を表示するために保持するだけで、スコア計算等の
    # 判定には使わない（既存のスコアリングは変更しない）。
    current_age: Optional[int] = None


# 譲渡所得税: 所有5年以下=短期39.63% / 5年超=長期20.315%
SHORT_TERM_TAX = 0.3963
LONG_TERM_TAX = 0.20315
# 売却時の仲介手数料(3%+6万+消費税)＋その他 ≒ 売却額の約3.5%で概算
SELL_COST_RATE = 0.035


def estimate_resale(
    cand: Candidate,
    curve: DepreciationCurve,
    current_year: int,
    hold_years: int,
    curve_source: str = "",
) -> ResaleEstimate:
    notes: list[str] = []
    current_age = current_year - cand.building_year
    future_age = current_age + hold_years

    fair_unit_now = curve.unit_price_for_age(current_age)
    fair_unit_future = curve.unit_price_for_age(future_age)

    fair_price_now = fair_unit_now * cand.area_sqm if fair_unit_now else None
    future_resale = fair_unit_future * cand.area_sqm if fair_unit_future else None

    asking_vs_fair = None
    if fair_price_now:
        asking_vs_fair = (cand.asking_price - fair_price_now) / fair_price_now * 100

    net = None
    if future_resale:
        sell_cost = future_resale * SELL_COST_RATE
        gain = future_resale - cand.asking_price  # 取得費を売出価格で近似
        tax_rate = LONG_TERM_TAX if hold_years > 5 else SHORT_TERM_TAX
        taxable_gain = max(0.0, gain - 30_000_000)  # 居住用3000万特別控除
        tax = taxable_gain * tax_rate
        net = future_resale - sell_cost - tax - cand.asking_price
        if hold_years <= 5:
            notes.append("所有5年以下は短期譲渡39.63%。売却益が出るなら5年超まで保有が有利。")
        if gain > 30_000_000:
            notes.append("売却益が3000万特別控除を超過。次物件の住宅ローン控除とは併用不可な点に注意。")

    score = _resale_score(cand, current_age, future_age, curve, notes, asking_vs_fair)

    return ResaleEstimate(
        current_fair_unit_price=fair_unit_now,
        current_fair_price=fair_price_now,
        asking_vs_fair_pct=asking_vs_fair,
        future_unit_price=fair_unit_future,
        future_resale_price=future_resale,
        net_after_tax_and_cost=net,
        resale_score=score,
        notes=notes,
        curve_source=curve_source,
        current_age=current_age,
    )


def _resale_score(
    cand: Candidate,
    current_age: int,
    future_age: int,
    curve: DepreciationCurve,
    notes: list[str],
    asking_vs_fair_pct: Optional[float] = None,
) -> int:
    """
    ヤドカリ出口（住みたいファミリーに売る）視点の流動性スコア 0-100。

    asking_vs_fair_pct（実勢比。+なら割高、-なら割安）を段階的に加減点する。
    このシステムの目的は「割安な物件を買う」ことであり、流動性が高くても
    割高な物件が高得点に見えるのは目的に反するため（docs/score-fairness-spec.md
    参照）。減点を加点より重くしているのは意図的な非対称設計で、割高を掴まない
    ことを割安を見つけることより優先する。
    """
    score = 50

    if cand.walk_minutes is not None:
        if cand.walk_minutes <= 7:
            score += 15
        elif cand.walk_minutes <= 10:
            score += 5
        else:
            score -= 10
            notes.append("駅徒歩10分超は買い手が絞られ、出口が重くなりやすい。")

    if 65 <= cand.area_sqm <= 80:
        score += 10
    elif cand.area_sqm < 50:
        score -= 10
        notes.append("50㎡未満はファミリー実需から外れ、出口が投資家中心になりがち。")

    if cand.total_units is not None:
        if cand.total_units >= 50:
            score += 8
        elif cand.total_units < 20:
            score -= 8
            notes.append("総戸数20戸未満は1戸あたり修繕負担が重く、将来の値上げ/一時金リスク。")

    if cand.repair_fund_per_sqm is not None and cand.repair_fund_per_sqm < 200:
        score -= 8
        notes.append("修繕積立金が㎡200円未満。将来の大幅値上げ・一時金徴収の可能性。")

    if future_age > 25:
        score -= 12
        notes.append(f"売却想定時に築{future_age}年。築25年超は買い手のローン審査が厳しくなる。")
    elif future_age <= 20:
        score += 7

    if asking_vs_fair_pct is not None:
        pct = asking_vs_fair_pct
        if pct >= 30:
            score -= 20
            notes.append(f"実勢比+{pct:.1f}%は大幅割高。出口での値下がり余地が大きい。")
        elif pct >= 15:
            score -= 12
            notes.append(f"実勢比+{pct:.1f}%は割高。指値交渉の前提で検討要。")
        elif pct >= 8:
            score -= 5
        elif pct <= -10:
            score += 8
            notes.append(f"実勢比{pct:.1f}%は割安圏。")

    return max(0, min(100, score))


# ---------------------------------------------------------------------------
# 5. 自己テスト（APIキー不要・ダミーデータでロジック確認）
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import random
    dummy = []
    random.seed(0)
    base_2024 = 900_000
    for _ in range(200):
        b_year = random.randint(1990, 2022)
        age = 2024 - b_year
        unit = base_2024 * (0.985 ** age) * random.uniform(0.9, 1.1)
        dummy.append(
            {
                "Type": "中古マンション等",
                "TradePrice": int(unit * 70),
                "Area": "70",
                "UnitPrice": int(unit),
                "BuildingYear": f"{b_year}年",
                "Period": "2024年第2四半期",
                "FloorPlan": "3LDK",
                "Municipality": "立川市",
                "DistrictName": "曙町",
            }
        )

    trades = normalize(dummy)
    curve = build_depreciation_curve(trades)
    print("=== 築年数別 実勢㎡単価（中央値） ===")
    for b in AGE_BUCKETS:
        if b in curve.median_unit_price:
            print(f"  築{b[0]:>2}-{b[1]:>2}年: {curve.median_unit_price[b]:>10,.0f} 円/㎡"
                  f"  (n={curve.sample_count[b]})")

    cand = Candidate(
        asking_price=42_000_000, area_sqm=72, building_year=2018,
        walk_minutes=6, total_units=80, repair_fund_per_sqm=230, floor_plan="3LDK",
    )
    est = estimate_resale(cand, curve, current_year=2026, hold_years=10)
    print("\n=== 候補物件の評価 ===")
    print(f"  現在の適正価格 : {est.current_fair_price:,.0f} 円"
          f" (売出 {cand.asking_price:,.0f} 円, 乖離 {est.asking_vs_fair_pct:+.1f}%)")
    print(f"  10年後想定売却 : {est.future_resale_price:,.0f} 円")
    print(f"  税・諸費用後手取り見込み: {est.net_after_tax_and_cost:,.0f} 円")
    print(f"  売りやすさスコア: {est.resale_score}/100")
    for n in est.notes:
        print(f"   - {n}")
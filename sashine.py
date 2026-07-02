"""
sashine.py
====================
売出価格から「指値（値引き交渉）」の目安金額3点セットを計算する。

【責務】
    - 売出価格 → 初回提示・落としどころ・引き際 の計算のみ
    - 強気度（aggressive/standard/mild）に応じた落としどころの算出

【やらないこと】
    - どの物件が指値候補かの判定（次STEP）
    - 通知・DB保存・既存フロー（scraper.py / evaluator.py）への組み込み
    - 副作用（DB・通信・print）は一切なし。純粋な計算関数のみ。

【単位について】
    入出力とも円（float）。Candidate.asking_price（reinfolib_resale.py）と
    同じ単位で揃えている。万円表示への変換は呼び出し側の責務とする
    （scraper.py の既存表示コードと同じ流儀）。
"""

from __future__ import annotations

import math
from typing import Optional

# ---------------------------------------------------------------------------
# 定数（調整したいときはここだけ見ればよい）
# ---------------------------------------------------------------------------

# 強気度ごとの「落としどころ」率
_AGGRESSIVE_RATE = 0.90   # 強気: 10%引き
_STANDARD_RATE   = 0.93   # 標準: 7%引き

# 控えめ(mild)は率ではなく「10万円単位への切り捨て」で落としどころを決める
_MILD_ROUND_UNIT = 100_000  # 10万円単位

# 落としどころから初回提示・引き際を求める率（共通）
_OPENING_OFFER_RATE = 0.95   # 初回提示 = 落としどころ × 0.95
_WALK_AWAY_RATE      = 1.03   # 引き際   = 落としどころ × 1.03

# 率計算（aggressive/standard/opening_offer/walk_away）の結果を丸める単位
# 買い手有利（安全側）に倒すため、すべて「切り捨て」で統一する。
_ROUND_UNIT = 10_000   # 万円未満切り捨て

_VALID_AGGRESSIVENESS = {"aggressive", "standard", "mild"}


def _floor_to_unit(value: float, unit: int) -> float:
    """
    value を unit 単位で切り捨てる（買い手有利＝常に安全側に丸める）。

    浮動小数点の演算誤差対策:
        例えば 42_000_000 * 0.9 は理論上 37_800_000 ちょうどだが、
        浮動小数点演算では 37_799_999.999999996 のような誤差が出ることがある。
        これを単純に math.floor すると 1万円単位で1つ下にずれてしまうため、
        先に「円単位で四捨五入」してノイズを消してから整数演算で切り捨てる。
    """
    yen = round(value)          # 円未満の浮動小数点ノイズを除去
    return float((yen // unit) * unit)


def calc_negotiation_targets(
    asking_price: float,
    aggressiveness: str = "standard",
) -> Optional[dict]:
    """
    売出価格から交渉目安金額3点セット（初回提示・落としどころ・引き際）を計算する。

    強気度3段階:
        aggressive: 落としどころ = 売出価格 × 0.90（10%引き）
        standard  : 落としどころ = 売出価格 × 0.93（7%引き）
        mild      : 落としどころ = 売出価格を10万円単位で切り捨て
                    ※ 売出価格がすでに10万円単位ちょうどの場合、
                      切り捨てても値引きゼロ（落としどころ＝売出価格）になる。
                      これは異常ではなく意図した自然な挙動。呼び出し側は
                      target_price == asking_price で判別できる
                      （戻り値に asking_price をそのままエコーしているため
                      追加の判定用フィールドは不要）。

    共通:
        初回提示 = 落としどころ × 0.95
        引き際   = 落としどころ × 1.03
        すべて万円未満は切り捨て（買い手有利・安全側に丸める）

    引数:
        asking_price   : 売出価格（円）。Candidate.asking_price と同じ単位。
        aggressiveness : "aggressive" | "standard" | "mild"（既定 "standard"）

    戻り値:
        {
            "asking_price":    float,  # 入力をそのままエコー（差額計算・判別用）
            "aggressiveness":  str,    # 使用した強気度
            "opening_offer":   float,  # 初回提示（円）
            "target_price":    float,  # 落としどころ（円）
            "walk_away_price": float,  # 引き際（円）
        }
        不正入力（0円以下・None・非数値・非有限値・未知の aggressiveness）は
        None を返す（例外を投げない）。副作用（DB・通信・print）は一切なし。
    """
    # ---- 入力チェック（asking_price）----
    if not isinstance(asking_price, (int, float)):
        return None
    if not math.isfinite(asking_price) or asking_price <= 0:
        return None

    # ---- 入力チェック（aggressiveness）----
    if aggressiveness not in _VALID_AGGRESSIVENESS:
        return None

    # ---- 落としどころの算出 ----
    if aggressiveness == "aggressive":
        target_price = _floor_to_unit(asking_price * _AGGRESSIVE_RATE, _ROUND_UNIT)
    elif aggressiveness == "standard":
        target_price = _floor_to_unit(asking_price * _STANDARD_RATE, _ROUND_UNIT)
    else:  # "mild"
        target_price = _floor_to_unit(asking_price, _MILD_ROUND_UNIT)

    # ---- 初回提示・引き際の算出（共通ロジック）----
    opening_offer   = _floor_to_unit(target_price * _OPENING_OFFER_RATE, _ROUND_UNIT)
    walk_away_price = _floor_to_unit(target_price * _WALK_AWAY_RATE, _ROUND_UNIT)

    return {
        "asking_price":    float(asking_price),
        "aggressiveness":  aggressiveness,
        "opening_offer":   opening_offer,
        "target_price":    target_price,
        "walk_away_price": walk_away_price,
    }

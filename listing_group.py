"""
listing_group.py
====================
同一物件が複数業者から別URLで掲載されるケース（横断重複）を検知するための
正規化・グルーピング純粋関数群。

【背景】
    ミオカステーロ府中紅葉丘の同一部屋が2つの業者から別URLで掲載され、
    URLをユニークキーとする現行方式では別物件として二重に新着通知・
    値下げ通知される問題が実際に発生した（実装指示書 rev.2 問題A）。
    location + area + floor_plan + age を正規化した組を「同一物件」の
    判定キーとして扱うことで、この重複を検知する。

【責務】
    - Listing（またはdict）から正規化済みグループキーを生成する
    - Listingのリストをグループ単位に束ねる

【やらないこと】
    - DB読み書き・通知・スクレイピング（呼び出し側 scraper.py の責務）
    - URL使い回し検知（別物件が同一URLに差し替わるケース。
      これは evaluator.py 側の観測履歴ベースの判定の責務）
"""

from __future__ import annotations

import re
from typing import Any, Optional

ListingLike = Any  # Listing dataclass または dict のどちらも受け付ける

# merge_similar_groups のマージ後グループ内価格差が
# この値（%）を超えたら [警告][group_price_spread] を出す。
# 同一グループに実は別部屋が混入している可能性の検知網。
PRICE_SPREAD_WARNING_PCT = 8.0

# format_dual_listing_note で、業者数がこの件数以上のときは価格を
# 列挙せず「最安〜最高」の要約形式に切り替える（列挙すると通知本文が
# 長大になりすぎるケース＝紅葉丘2の13業者掲載で発覚）。
DUAL_LISTING_SUMMARIZE_THRESHOLD = 4


def _get(listing: ListingLike, key: str) -> str:
    if hasattr(listing, key):
        return getattr(listing, key) or ""
    return listing.get(key, "") or ""


def _to_halfwidth_digits(s: str) -> str:
    zen = "０１２３４５６７８９"
    han = "0123456789"
    return s.translate(str.maketrans(zen, han))


def _normalize_location(location: str) -> str:
    """全角/半角スペース除去・数字を半角化した上で、最初の丁目数字まで採用する。
    「紅葉丘２」と「紅葉丘２-9-10」を同一視するための処理。
    丁目相当の数字が見つからない場合（例: 「稲城市矢野口」）はスペース除去のみ行う。"""
    s = _to_halfwidth_digits(location)
    s = re.sub(r'[\s　]+', '', s)
    m = re.match(r'^(.*?[0-9]+)', s)
    return m.group(1) if m else s


def _normalize_area(area: str) -> str:
    """「90.02m2（壁芯）」「72.5m²」「72.5㎡」→ 数値部分のみ抽出し "90.02" 形式に
    正規化する。半角m・全角ｍ・上付き²・㎡の表記ゆれに対応する
    （suumo_adapter._parse_area と同じ文字クラスを使う）。
    数値が取れない場合は空文字（≠他のどの area とも一致しない値として扱われる）。"""
    m = re.search(r'([\d.]+)\s*[mｍ㎡]', area)
    if not m:
        return ""
    return f"{float(m.group(1)):.2f}"


def _normalize_floor_plan(floor_plan: str) -> str:
    return floor_plan.strip().upper()


def _normalize_age(age: str) -> str:
    """「2003年7月」→「2003-07」。年月が取れない場合は空文字。"""
    m = re.search(r'(\d{4})\s*年\s*(\d{1,2})\s*月', age)
    if not m:
        return ""
    return f"{m.group(1)}-{int(m.group(2)):02d}"


def normalize_for_group(listing: ListingLike) -> tuple[str, str, str, str]:
    """Listing（またはdict）から正規化済みの (location, area, floor_plan, age) を返す。"""
    return (
        _normalize_location(_get(listing, "location")),
        _normalize_area(_get(listing, "area")),
        _normalize_floor_plan(_get(listing, "floor_plan")),
        _normalize_age(_get(listing, "age")),
    )


def group_key(listing: ListingLike) -> str:
    """normalize_for_group の結果を "|" 結合した文字列キーを返す。"""
    return "|".join(normalize_for_group(listing))


def group_listings(listings: list[ListingLike]) -> dict[str, list[ListingLike]]:
    """Listingのリストをグループキー単位に束ねる。順序は入力順を保持する。"""
    groups: dict[str, list[ListingLike]] = {}
    for l in listings:
        groups.setdefault(group_key(l), []).append(l)
    return groups


def _parse_price_man_yen(price: str) -> float:
    """「5290万円」→ 5290.0（万円単位）。パース不能なら inf を返し、
    最安値選定で不利になるようにする（安全側＝誤って代表に選ばれない）。"""
    m = re.search(r'([\d,]+(?:\.\d+)?)\s*万円', price or "")
    if not m:
        return float("inf")
    return float(m.group(1).replace(",", ""))


def select_representative(group: list[ListingLike]) -> ListingLike:
    """グループ内の最安値を代表として返す。同額なら url 昇順で安定的に選ぶ
    （テスト再現性のため。sorted() は安定ソートだが、入力順依存を避けるため
    url を明示的にタイブレークキーにする）。"""
    return sorted(group, key=lambda l: (_parse_price_man_yen(_get(l, "price")), _get(l, "url")))[0]


def format_dual_listing_note(group: list[ListingLike]) -> str:
    """グループが2件以上のときのみ「※同一物件がN業者から掲載」を返す。
    1件（重複なし）のときは空文字を返す。

    業者数が DUAL_LISTING_SUMMARIZE_THRESHOLD 未満なら価格を昇順で列挙し、
    それ以上なら「最安〜最高」の要約形式に切り替える（列挙し続けると
    通知本文が長大になるケースへの対策）。"""
    if len(group) < 2:
        return ""
    ordered = sorted(group, key=lambda l: (_parse_price_man_yen(_get(l, "price")), _get(l, "url")))
    prices = [_parse_price_man_yen(_get(l, "price")) for l in ordered]
    valid_prices = [p for p in prices if p != float("inf")]

    if len(group) >= DUAL_LISTING_SUMMARIZE_THRESHOLD:
        if not valid_prices:
            price_part = "価格不明"
        elif min(valid_prices) == max(valid_prices):
            price_part = f"{valid_prices[0]:,.0f}万"
        else:
            price_part = f"最安{min(valid_prices):,.0f}万〜最高{max(valid_prices):,.0f}万"
        return f"※同一物件が{len(group)}業者から掲載（{price_part}）"

    price_strs = [
        f"{p:,.0f}万" if p != float("inf") else _get(l, "price")
        for l, p in zip(ordered, prices)
    ]
    return f"※同一物件が{len(group)}業者から掲載（{'/'.join(price_strs)}）"


def _parse_normalized_area(area_str: str) -> Optional[float]:
    """normalize_for_group が返す "73.04" 形式の文字列を float に戻す。空文字は None。"""
    if not area_str:
        return None
    try:
        return float(area_str)
    except ValueError:
        return None


def _parse_normalized_age_month_index(age_str: str) -> Optional[int]:
    """normalize_for_group が返す "YYYY-MM" 形式を year*12+month の通し月
    インデックスに変換する（月差の計算をカレンダー境界（年またぎ）でも
    単純な引き算で扱えるようにするため）。形式不一致・空文字は None。"""
    m = re.match(r'^(\d{4})-(\d{2})$', age_str or "")
    if not m:
        return None
    return int(m.group(1)) * 12 + int(m.group(2))


def _should_merge(key_a: str, key_b: str) -> bool:
    """2つの厳密グループキーが「同一物件の表記ゆれ」とみなせるかを判定する。
    location・floor_plan は完全一致必須。area差 ≤0.05㎡・築年月差 ≤1ヶ月まで許容。
    area・age のどちらかが正規化できていない（空文字）場合は安全側に倒し
    マージしない。"""
    loc_a, area_a, fp_a, age_a = key_a.split("|")
    loc_b, area_b, fp_b, age_b = key_b.split("|")
    if loc_a != loc_b or fp_a != fp_b:
        return False

    area_a_f, area_b_f = _parse_normalized_area(area_a), _parse_normalized_area(area_b)
    if area_a_f is None or area_b_f is None or abs(area_a_f - area_b_f) > 0.05:
        return False

    age_a_i, age_b_i = _parse_normalized_age_month_index(age_a), _parse_normalized_age_month_index(age_b)
    if age_a_i is None or age_b_i is None or abs(age_a_i - age_b_i) > 1:
        return False

    return True


class _UnionFind:
    """マージ判定の連鎖（73.00↔73.04↔73.09 のような繋がり）を決定的な
    1グループに解決するための素集合データ構造。根の選び方を文字列昇順に
    固定することで、入力順に依存せず同じ結果になるようにしている。"""

    def __init__(self, items: list[str]) -> None:
        self._parent = {x: x for x in items}

    def find(self, x: str) -> str:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if ra < rb:
            self._parent[rb] = ra
        else:
            self._parent[ra] = rb


def merge_similar_groups(groups: dict[str, list[ListingLike]]) -> dict[str, list[ListingLike]]:
    """group_listings が作った厳密一致グループのうち、表記ゆれ（築年月の
    月ズレ・面積の丸め誤差）で分裂しているものを後段でマージする。

    グループキーは辞書のハッシュとして使う完全一致文字列であるため、
    キー自体に許容誤差を持たせることはできない（73.00/73.04/73.09が
    連鎖したとき、どのキーに寄せるかが不定になる）。そのため
    「厳密キーでグループ化 → 近接グループを Union-Find で決定的にマージ」
    の二段構えにしている。

    マージ後の代表キーは、そのグループ内の最安値物件の厳密キーを採用する。
    マージが発生した場合は表記ゆれの頻度観測用に [info][group_merged] を、
    マージ後グループ内の価格差が PRICE_SPREAD_WARNING_PCT を超える場合は
    別部屋混入の可能性の検知網として [警告][group_price_spread] を出す
    （これは同一建物・同一間取り・同一面積の別の部屋（例: 3階と4階の
    同タイプ）を厳密一致キーでも区別できないという、このキー設計自体の
    既知の限界に対する緩和策であり、この限界自体を解消するものではない）。
    """
    keys = list(groups.keys())
    uf = _UnionFind(keys)

    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            if _should_merge(keys[i], keys[j]) and uf.find(keys[i]) != uf.find(keys[j]):
                print(f"  [info][group_merged] '{keys[i]}' <-> '{keys[j]}'", flush=True)
                uf.union(keys[i], keys[j])

    root_to_keys: dict[str, list[str]] = {}
    for k in keys:
        root_to_keys.setdefault(uf.find(k), []).append(k)

    merged: dict[str, list[ListingLike]] = {}
    for member_keys in root_to_keys.values():
        members: list[ListingLike] = []
        for k in member_keys:
            members.extend(groups[k])
        rep_key = group_key(select_representative(members))
        merged[rep_key] = members

        prices = [p for p in (_parse_price_man_yen(_get(l, "price")) for l in members) if p != float("inf")]
        if len(prices) >= 2:
            lo, hi = min(prices), max(prices)
            spread_pct = (hi - lo) / lo * 100 if lo > 0 else 0.0
            if spread_pct > PRICE_SPREAD_WARNING_PCT:
                print(
                    f"  [警告][group_price_spread] key={rep_key} "
                    f"価格差{spread_pct:.1f}% (最安{lo:,.0f}万〜最高{hi:,.0f}万) "
                    "別部屋混入の可能性あり",
                    flush=True,
                )

    return merged

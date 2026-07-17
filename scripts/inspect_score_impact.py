"""score-fairness-spec STEP 1: 読み取り専用の影響調査スクリプト。

evaluations.db の最新評価日の全件について、現行 resale_score と
docs/score-fairness-spec.md 2章の段階加減点を適用した新式スコアを
対照させ、有望枠（PROMISING_SCORE_THRESHOLD=70）への出入りを集計する。

DBは mode=ro で開く。書き込み・スキーマ変更・本体コードの変更は行わない。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "evaluations.db"
CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
PROMISING_SCORE_THRESHOLD = 70  # scraper.py と同値（読み取りのみ、import はしない）

# USE_MOCK_REINFOLIB汚染キャッシュの既知の目印。過去に架空地区名「テスト町」が
# 実測値と同じ形式でDBに紛れ込み、本番と異なる数値を報告する事故があった。
MOCK_DISTRICT_MARKERS = ("テスト町",)


def _check_cache_contamination() -> None:
    """
    cache/*.json をスキャンし、district_curves に MOCK_DISTRICT_MARKERS が
    含まれていたら即中断する。

    【なぜ curve_source だけのチェックでは不十分か（2026-07-17に実際に
    起きた事故で判明）】: 汚染時、汚染された数値は city_curve（curve_source
    は正常な "city:府中市" のまま）にも紛れ込んでいた。district"テスト町"に
    実際に一致しなければ curve_source の文字列だけを見ても汚染は分からない。
    そのためDBの curve_source 列だけでなく、キャッシュJSON自体の
    district_curves キーも直接見る必要がある。
    """
    if not CACHE_DIR.exists():
        return
    for path in CACHE_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        district_curves = data.get("bundle", {}).get("district_curves", {})
        hit = [d for d in district_curves if any(m in d for m in MOCK_DISTRICT_MARKERS)]
        if hit:
            raise SystemExit(
                f"[汚染検知] {path} の district_curves にモック地区名 {hit} が"
                "含まれています。USE_MOCK_REINFOLIB汚染キャッシュの疑いがあるため中断します。"
            )


def new_score_delta(pct: float | None) -> tuple[int, str]:
    """指示書2章の段階加減点をそのまま再現する（読み取り専用の再計算用）。"""
    if pct is None:
        return 0, ""
    if pct >= 30:
        return -20, "大幅割高"
    if pct >= 15:
        return -12, "割高"
    if pct >= 8:
        return -5, "やや割高"
    if pct <= -10:
        return 8, "割安圏"
    return 0, ""


def main() -> None:
    _check_cache_contamination()

    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    cur = con.cursor()

    cur.execute("SELECT MAX(evaluated_date) FROM evaluations")
    (latest_date,) = cur.fetchone()

    cur.execute(
        """
        SELECT listing_url, listing_name, asking_price, asking_vs_fair_pct, resale_score, curve_source
        FROM evaluations
        WHERE evaluated_date = ?
        ORDER BY resale_score DESC
        """,
        (latest_date,),
    )
    rows = cur.fetchall()

    for url, _name, _price, _pct, _score, curve_source in rows:
        if curve_source and any(marker in curve_source for marker in MOCK_DISTRICT_MARKERS):
            raise SystemExit(
                f"[汚染検知] curve_source={curve_source!r}（URL={url}）にモック地区名が"
                "含まれています。USE_MOCK_REINFOLIB汚染キャッシュの疑いがあるため中断します。"
            )

    table = []
    upgrades = 0
    downgrades = 0
    new_scores = []

    for url, name, price, pct, old_score, _curve_source in rows:
        delta, tag = new_score_delta(pct)
        new_score = old_score if old_score is None else max(0, min(100, old_score + delta))
        is_injected = "/dry-run/" in url or "[DRY_RUN" in (name or "")
        if not is_injected:
            new_scores.append(new_score)

        old_promising = old_score is not None and old_score >= PROMISING_SCORE_THRESHOLD
        new_promising = new_score is not None and new_score >= PROMISING_SCORE_THRESHOLD
        flag = ""
        if not is_injected:
            if old_promising and not new_promising:
                downgrades += 1
                flag = "脱落"
            elif not old_promising and new_promising:
                upgrades += 1
                flag = "昇格"

        table.append(
            {
                "url_tail": url.rsplit("/", 2)[-2] if "/" in url else url,
                "name": (name or "")[:20],
                "price": price,
                "pct": pct,
                "old_score": old_score,
                "new_score": new_score,
                "delta": delta,
                "tag": tag,
                "flag": flag,
                "src": "注入" if is_injected else "実データ",
            }
        )

    n_injected = sum(1 for r in table if r["src"] == "注入")
    print(f"評価日: {latest_date}  件数: {len(rows)}（実データ {len(rows) - n_injected} / 注入(dry_run混入) {n_injected}）")
    print()
    header = f"{'区分':<6} {'URL末尾':<14} {'物件名':<22} {'価格':>11} {'実勢比':>8} {'旧':>4} {'新':>4} {'差':>4} {'出入り':<6} 備考"
    print(header)
    print("-" * len(header))
    for r in table:
        pct_str = f"{r['pct']:+.1f}%" if r["pct"] is not None else "N/A"
        price_str = f"{r['price']:,.0f}" if r["price"] is not None else "N/A"
        print(
            f"{r['src']:<6} {r['url_tail']:<14} {r['name']:<22} {price_str:>11} {pct_str:>8} "
            f"{r['old_score']!s:>4} {r['new_score']!s:>4} {r['delta']:>+4} {r['flag']:<6} {r['tag']}"
        )

    print()
    print("※ 注入行(dry-run混入分)は有望枠出入り集計・スコア分布から除外")
    print(f"有望枠(>={PROMISING_SCORE_THRESHOLD})脱落: {downgrades} 件")
    print(f"有望枠(>={PROMISING_SCORE_THRESHOLD})昇格: {upgrades} 件")

    print()
    print("新式スコア分布（10点刻み）")
    buckets = {i: 0 for i in range(0, 101, 10)}
    for s in new_scores:
        if s is None:
            continue
        b = min(90, (s // 10) * 10)
        buckets[b] += 1
    for b in sorted(buckets):
        bar = "#" * buckets[b]
        print(f"  {b:>3}-{b+9:<3}: {buckets[b]:>2} {bar}")


if __name__ == "__main__":
    main()

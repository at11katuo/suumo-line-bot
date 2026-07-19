"""score-fairness-spec 週次観測: 読み取り専用の定点観測スクリプト。

【2026-07-18 役割変更】もともとはSTEP1の新旧スコア対照表（DBの値に
段階加減点をもう一度適用して「新式」を作る）だったが、デプロイ完了後
（_resale_scoreが本番で段階加減点を計算するようになった後）はDBの
resale_score列自体が既に新式の値になっているため、ここで同じ加減点を
再適用すると二重適用になる（実際に発生した事故: 是政4LDK nc_20893454が
61→41→21と2回減点された）。

このツールはDB値を加工せず表示する。スコア公式（段階加減点の閾値・
点数）をここに複製してはならない。公式は reinfolib_resale._resale_score
の一箇所にのみ存在するべきで、診断ツールに複製を持たせたことが今回の
二重適用事故の根本原因である。以後、この教訓を守ること。

【現在の役割】evaluations.db の最新評価日を対象に、
    - 全件の一覧（区分・実勢比・スコア）
    - 前回評価日比でスコアが動いた物件（値下げ等による小さな変化を拾う網。
      price_drop/score_gainの通知閾値に届かない動きも見える）
    - 有望枠（PROMISING_SCORE_THRESHOLD=70）該当リスト
    - スコア分布・実勢比分布のヒストグラム
を読み取り専用で報告する、週次観測用の定点観測ツール。

DBは mode=ro で開く。書き込み・スキーマ変更・本体コードの変更は行わない。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
from contamination_guard import check_cache_dir, check_curve_source  # noqa: E402

DB_PATH = _REPO_ROOT / "evaluations.db"
CACHE_DIR = _REPO_ROOT / "cache"
PROMISING_SCORE_THRESHOLD = 70  # scraper.py と同値（読み取りのみ、import はしない）


def _is_injected(url: str, name: str | None) -> bool:
    return "/dry-run/" in url or "[DRY_RUN" in (name or "")


def _bucket_label(pct: float) -> str:
    if pct >= 30:
        return "大幅割高"
    if pct >= 15:
        return "割高"
    if pct >= 8:
        return "やや割高"
    if pct <= -10:
        return "割安圏"
    return ""


def main() -> None:
    check_cache_dir(CACHE_DIR)

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
        check_curve_source(curve_source, context=f"URL={url}")

    urls = [r[0] for r in rows]

    # 各URLの直近評価日より前の最新行（前回比較用）を取得する。
    # ウィンドウ関数を使わず、URLごとの全履歴をPython側でグルーピングする
    # （件数が数十件規模のため性能上の問題はない）。
    prev_by_url: dict[str, tuple[str, float | None, int | None]] = {}
    if urls:
        placeholders = ",".join("?" * len(urls))
        cur.execute(
            f"""
            SELECT listing_url, evaluated_date, asking_vs_fair_pct, resale_score
            FROM evaluations
            WHERE listing_url IN ({placeholders}) AND evaluated_date < ?
            ORDER BY listing_url, evaluated_date
            """,
            (*urls, latest_date),
        )
        history: dict[str, list[tuple[str, float | None, int | None]]] = {}
        for url, date_, pct, score in cur.fetchall():
            history.setdefault(url, []).append((date_, pct, score))
        for url, entries in history.items():
            prev_by_url[url] = entries[-1]  # 最新（=latest_dateの直前）の1件

    table = []
    n_promising = 0
    scores = []
    pcts = []

    for url, name, price, pct, score, _curve_source in rows:
        injected = _is_injected(url, name)
        if not injected:
            if score is not None:
                scores.append(score)
            if pct is not None:
                pcts.append(pct)
            if score is not None and score >= PROMISING_SCORE_THRESHOLD:
                n_promising += 1

        table.append(
            {
                "url": url,
                "url_tail": url.rsplit("/", 2)[-2] if "/" in url else url,
                "name": (name or "")[:20],
                "price": price,
                "pct": pct,
                "score": score,
                "src": "注入" if injected else "実データ",
            }
        )

    n_injected = sum(1 for r in table if r["src"] == "注入")
    print(f"評価日: {latest_date}  件数: {len(rows)}（実データ {len(rows) - n_injected} / 注入(dry_run混入) {n_injected}）")
    print()
    header = f"{'区分':<6} {'URL末尾':<14} {'物件名':<22} {'価格':>11} {'実勢比':>8} {'スコア':>5} 備考"
    print(header)
    print("-" * len(header))
    for r in table:
        pct_str = f"{r['pct']:+.1f}%" if r["pct"] is not None else "N/A"
        price_str = f"{r['price']:,.0f}" if r["price"] is not None else "N/A"
        tag = _bucket_label(r["pct"]) if r["pct"] is not None else ""
        print(
            f"{r['src']:<6} {r['url_tail']:<14} {r['name']:<22} {price_str:>11} {pct_str:>8} "
            f"{r['score']!s:>5} {tag}"
        )

    # ---- 前回評価日比でスコアが動いた物件 ----
    print()
    print(f"スコア変動があった物件（前回評価日比、評価日={latest_date}）:")
    changed = []
    for r in table:
        if r["src"] == "注入":
            continue
        prev = prev_by_url.get(r["url"])
        if prev is None or r["score"] is None:
            continue  # 初観測（前回行なし）は比較対象外
        prev_date, prev_pct, prev_score = prev
        if prev_score is None or prev_score == r["score"]:
            continue
        changed.append((r, prev_date, prev_pct, prev_score))

    if not changed:
        print("  変動なし")
    else:
        for r, prev_date, prev_pct, prev_score in changed:
            diff = r["score"] - prev_score
            prev_pct_str = f"{prev_pct:+.1f}%" if prev_pct is not None else "N/A"
            pct_str = f"{r['pct']:+.1f}%" if r["pct"] is not None else "N/A"
            promising_note = ""
            if prev_score < PROMISING_SCORE_THRESHOLD <= r["score"]:
                promising_note = "  ← 有望枠(>=70)に昇格"
            elif r["score"] < PROMISING_SCORE_THRESHOLD <= prev_score:
                promising_note = "  ← 有望枠(>=70)から脱落"
            print(
                f"  {r['url_tail']:<14} {prev_score:>3} → {r['score']:<3} ({diff:+d})   "
                f"実勢比 {prev_pct_str} → {pct_str}   [{prev_date}→{latest_date}]{promising_note}"
            )

    # ---- 有望枠該当リスト ----
    print()
    print(f"有望枠（スコア>={PROMISING_SCORE_THRESHOLD}）該当: {n_promising} 件（実データのみ）")
    for r in table:
        if r["src"] != "注入" and r["score"] is not None and r["score"] >= PROMISING_SCORE_THRESHOLD:
            pct_str = f"{r['pct']:+.1f}%" if r["pct"] is not None else "N/A"
            print(f"  {r['url_tail']:<14} {r['name']:<22} スコア{r['score']}  実勢比{pct_str}")

    # ---- スコア分布 ----
    print()
    print("スコア分布（10点刻み、実データのみ）")
    buckets = {i: 0 for i in range(0, 101, 10)}
    for s in scores:
        b = min(90, (s // 10) * 10)
        buckets[b] += 1
    for b in sorted(buckets):
        bar = "#" * buckets[b]
        print(f"  {b:>3}-{b+9:<3}: {buckets[b]:>2} {bar}")

    # ---- 実勢比分布 ----
    print()
    print("実勢比分布（10%刻み、実データのみ）")
    pct_buckets: dict[int, int] = {}
    for p in pcts:
        b = int(p // 10) * 10
        pct_buckets[b] = pct_buckets.get(b, 0) + 1
    for b in sorted(pct_buckets):
        bar = "#" * pct_buckets[b]
        print(f"  {b:>+4}〜{b+9:<+4}%: {pct_buckets[b]:>2} {bar}")


if __name__ == "__main__":
    main()

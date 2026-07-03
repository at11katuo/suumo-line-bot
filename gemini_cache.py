"""
gemini_cache.py
====================
Gemini評価結果（★数・評価文）を物件URLごとに一度だけ保存しておくモジュール。

【背景】
    Gemini評価は new_listings（新着）のみが対象で、API呼び出し回数を
    抑えるため既知物件への再評価は行わない設計になっている。しかし
    一度Geminiに低評価をつけられた物件の reinfolib 評価が後で改善しても、
    その事実を知る手段がなければ「参考枠」に二度と浮上できない
    （実際に調布市の物件で発生していたことが確認された欠陥）。

    この保存機構により、新着時に一度だけ計算した Gemini 評価を、
    以後の日次実行でも reinfolib 評価と組み合わせて再利用できるようにする。

【責務】
    - 物件URLごとの Gemini★・評価文の保存・読み込みのみ

【やらないこと】
    - Gemini API の呼び出し（scraper.evaluate_listing の責務）
    - 参考枠の有望判定・重複抑制・通知（scraper.py / evaluator.py の責務）
    - 副作用は DB 読み書きのみ（通信・print は一切なし）

【db_path について】
    引数は Optional[Path] = None とし、関数本体の先頭で
    `if db_path is None: db_path = DB_PATH` と解決する。
    関数定義時に固定値へ束縛されるのを避けるための設計
    （evaluator.py / detail_fetcher.py と同じパターン。テストで
    monkeypatch した DB_PATH を正しく反映できるようにするため）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

# evaluations.db と同じファイルに gemini_evaluations テーブルを置く
# （管理1ファイルで完結。detail_cache と同じ考え方）
DB_PATH = Path(__file__).parent / "evaluations.db"


def _init_gemini_table(conn: sqlite3.Connection) -> None:
    """gemini_evaluations テーブルを作る（すでにあれば何もしない）。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gemini_evaluations (
            listing_url  TEXT PRIMARY KEY,  -- 物件の一意キー（SUUMOのURL）
            gemini_score INTEGER NOT NULL,  -- Gemini★数（0=API失敗/応答なし〜5）
            eval_text    TEXT NOT NULL,     -- Gemini評価の生テキスト（そのまま保存）
            evaluated_at TEXT NOT NULL      -- ISO 8601 日時（記録用）
        )
    """)
    conn.commit()


def save_gemini_evaluation(
    url: str,
    score: int,
    eval_text: str,
    db_path: Optional[Path] = None,
) -> None:
    """
    初回の Gemini 評価結果を保存する（UPSERT）。
    以後この URL は再評価しない前提（呼び出し元 = 新着物件ループのみ）。

    DB書き込みに失敗しても例外は投げない（Gemini評価自体は既に完了して
    いるため、記録の失敗だけで処理全体を止める必要はない）。

    db_path: None なら呼び出し時点の DB_PATH を使う。
    """
    if db_path is None:
        db_path = DB_PATH
    now = datetime.now().isoformat(timespec="seconds")
    try:
        conn = sqlite3.connect(db_path)
        try:
            _init_gemini_table(conn)
            conn.execute(
                """
                INSERT INTO gemini_evaluations
                    (listing_url, gemini_score, eval_text, evaluated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(listing_url) DO UPDATE SET
                    gemini_score = excluded.gemini_score,
                    eval_text    = excluded.eval_text,
                    evaluated_at = excluded.evaluated_at
                """,
                (url, score, eval_text, now),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass  # 記録失敗はログのみで十分（呼び出し元 scraper.py 側で警告表示）


def load_gemini_evaluations(
    urls: list[str],
    db_path: Optional[Path] = None,
) -> dict[str, tuple[int, str]]:
    """
    指定 URL 群の保存済み Gemini 評価を一括取得する。

    戻り値:
        {url: (gemini_score, eval_text)}
        DB が存在しない・テーブル未作成・該当URLなし・例外時は
        該当分を含まない dict を返す（例外を出さない）。

    db_path: None なら呼び出し時点の DB_PATH を使う。
    """
    if db_path is None:
        db_path = DB_PATH
    if not urls or not db_path.exists():
        return {}

    result: dict[str, tuple[int, str]] = {}
    try:
        conn = sqlite3.connect(db_path)
        try:
            placeholders = ",".join("?" * len(urls))
            rows = conn.execute(
                f"SELECT listing_url, gemini_score, eval_text "
                f"FROM gemini_evaluations WHERE listing_url IN ({placeholders})",
                list(urls),
            ).fetchall()
        finally:
            conn.close()
        for url, score, eval_text in rows:
            result[url] = (score, eval_text)
    except sqlite3.OperationalError:
        # テーブル未作成（初回実行前など）は空 dict を返す
        pass

    return result

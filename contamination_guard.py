"""
contamination_guard.py
====================
USE_MOCK_REINFOLIB汚染キャッシュ（架空地区名「テスト町」等）の検知ロジック。

過去に、ローカルの汚染キャッシュ（cache/*.json の district_curves に
架空地区「テスト町」が実測値と同じ形式で紛れ込んだもの）を「本番の実測
reinfolib値」と誤認して報告する事故が起きた。この事故を教訓に、以下
2種類の読み取り専用スクリプトが同じ判定ロジックを必要とするため、
ここに1箇所へ集約する（計算式・しきい値を2箇所に持つと後で数字が
合わなくなる問題を避けるための設計。sashine.pyのSTEP2再利用方針と同じ
考え方）:
    - scripts/inspect_score_impact.py（診断専用・手動ワークフロー）
    - dry_run_step4.py（診断専用・手動ワークフロー）の環境健全性チェック

検知は2段構え:
    1. curve_source文字列に既知のモック地区名が含まれるかのチェック
       （軽量だが、汚染がcity_curveにフォールバックした場合は
       curve_sourceが正常な "city:府中市" のままになるため検知できない）
    2. cache/*.json の district_curves キー自体を直接スキャンするチェック
       （1では検知できない、実際に起きたフォールバック経由の汚染を検知する）
2つとも独立に実行することで、どちらか一方の抜け道を塞ぐ。
"""
from __future__ import annotations

import json
from pathlib import Path

MOCK_DISTRICT_MARKERS = ("テスト町",)


def check_curve_source(curve_source: str | None, context: str = "") -> None:
    """curve_source文字列に既知のモック地区名が含まれていたら中断する。"""
    if not curve_source:
        return
    if any(marker in curve_source for marker in MOCK_DISTRICT_MARKERS):
        raise SystemExit(
            f"[汚染検知] curve_source={curve_source!r}{(' (' + context + ')') if context else ''} に"
            "モック地区名が含まれています。USE_MOCK_REINFOLIB汚染キャッシュの疑いがあるため中断します。"
        )


def check_cache_dir(cache_dir: Path) -> None:
    """
    cache/*.json をスキャンし、district_curves に MOCK_DISTRICT_MARKERS が
    含まれていたら中断する（check_curve_sourceだけでは検知できない、
    city_curveへのフォールバック経由の汚染を検知するための2段目）。
    """
    if not cache_dir.exists():
        return
    for path in cache_dir.glob("*.json"):
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

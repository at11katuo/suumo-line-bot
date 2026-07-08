"""
不動産新着物件スクレイパー (SUUMO 中古マンション)
- 新着物件を取得し data.csv と差分比較
- Gemini API で物件を5段階評価し、4〜5★のみ LINE に通知
"""

import csv
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass, fields
from typing import Optional

from google import genai
from google.genai import types
import requests
from bs4 import BeautifulSoup

# ------------------------------------------------------------------ #
# 設定
# ------------------------------------------------------------------ #
LINE_CHANNEL_ACCESS_TOKEN: Optional[str] = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_USER_ID: Optional[str] = os.environ.get("LINE_USER_ID")
GEMINI_API_KEY: Optional[str] = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = "gemini-2.5-flash"
DATA_FILE = "data.csv"

# 1回の実行でGemini評価する件数の上限。無料枠は1日20リクエスト（モデルあたり）
# だが、定期実行が1日2回あるため、1回あたりの上限は 20÷2=10 より少し
# 余裕を持たせて8件にしている（8件×2回=16件/日で無料枠に収まる）。
# 新着が大量発生した日に全件評価しようとすると429エラーが連鎖し実行が
# 異常に長時間化する事故が実際に発生したための安全策。
GEMINI_EVAL_LIMIT_PER_RUN = 8

# 検索URLは環境変数で上書き可能
# 対象: 調布市・府中市 / 中古マンション / 4000〜5500万円
# ※徒歩・面積・築年数フィルターはURLパラメータ非対応のためPythonで後処理
DEFAULT_URL = (
    "https://suumo.jp/jj/bukken/ichiran/JJ010FJ001/"
    "?ar=030&bs=011&ta=13&jspIdFlg=patternShikugun"
    "&sc=13206&sc=13208&sc=13225"   # 府中市(13206)・調布市(13208)・稲城市(13225)
    "&kb=4000&kt=5500"              # 4000万〜5500万円
    "&mb=0&mt=9999999&ekTjCd=&ekTjNm=&tj=0&cnb=0&cn=9999999&srch_navi=1"
    "&po=1&pj=2"  # 新着・更新順。デフォルト（おすすめ順等）だと総件数が
                  # 多い日にMAX_PAGES=3(90件)から漏れる新着物件が出るため固定する
                  # （実際に稲城市の物件が170番目で圏外になった事例で確認済み）
)
TARGET_URL: str = os.environ.get("TARGET_URL", DEFAULT_URL)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

REQUEST_INTERVAL = 2   # ページ間のウェイト（秒）。取得ページ数が変わっても
                       # 1回あたりの間隔は変えず、慎重スタンスを維持する

# ── 取得ページ数の自動決定に使う定数 ──────────────────────────
# 固定ページ数（3→5→10）を都度増やす対処を繰り返してきたが、SUUMOの
# 検索結果総件数は市況により変動するため、根本対応として「1ページ目で
# 総件数を読み取り、必要なページ数を都度計算する」方式に変更した。
# MAX_PAGES は「固定取得ページ数」ではなく「安全上限（キャップ）」に
# 役割を変えている。
MAX_PAGES = 15          # 安全上限（450件）。計算結果がこれを超えたら打ち切る
FALLBACK_PAGES = 10     # 総件数が読み取れない場合のデフォルト（旧実績値）
ITEMS_PER_PAGE = 30     # SUUMO検索結果の1ページあたり件数（観測値。pc未指定時の既定）

# 有望物件の判定しきい値
PROMISING_SCORE_THRESHOLD = 70    # 売りやすさスコアの下限（100点満点）
PROMISING_VS_FAIR_MAX_PCT  = 5.0  # 実勢比乖離率の上限（+は割高）


# ------------------------------------------------------------------ #
# データモデル
# ------------------------------------------------------------------ #
@dataclass
class Listing:
    name: str
    price: str
    location: str
    url: str
    station: str = ""
    floor_plan: str = ""
    area: str = ""
    age: str = ""

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


# ------------------------------------------------------------------ #
# スクレイピング
# ------------------------------------------------------------------ #
def fetch_page(url: str) -> BeautifulSoup:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    print(f"  [HTTP] status={resp.status_code} url={url}", flush=True)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    soup = BeautifulSoup(resp.text, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else "（タイトルなし）"
    print(f"  [PAGE] title=「{title}」", flush=True)
    return soup


def parse_listings(soup: BeautifulSoup) -> list[Listing]:
    results: list[Listing] = []

    for card in soup.select("div.property_unit"):
        # 物件名 + URL
        title_el = card.select_one("h2.property_unit-title a")
        if not title_el:
            continue
        name = title_el.get_text(strip=True)
        href = title_el.get("href", "")
        url = href if href.startswith("http") else f"https://suumo.jp{href}"

        # 価格
        price_el = card.select_one("span.dottable-value")
        price = price_el.get_text(strip=True) if price_el else "（価格不明）"

        # dt/dd ペアをすべて辞書化して各フィールドに割り当て
        dt_map: dict[str, str] = {}
        for dt in card.select("dt"):
            dd = dt.find_next_sibling("dd")
            if dd:
                dt_map[dt.get_text(strip=True)] = dd.get_text(strip=True)

        location = dt_map.get("所在地", "")
        if not location:
            dds = card.select("dd")
            location = dds[2].get_text(strip=True) if len(dds) >= 3 else "（所在地不明）"

        station    = dt_map.get("沿線・駅", "")
        floor_plan = dt_map.get("間取り", "")
        area       = dt_map.get("専有面積", "")
        age        = dt_map.get("築年月", "") or dt_map.get("築年数", "")

        results.append(Listing(
            name=name, price=price, location=location, url=url,
            station=station, floor_plan=floor_plan, area=area, age=age,
        ))

    return results


def get_next_page_url(soup: BeautifulSoup, current_url: str) -> Optional[str]:
    """ページネーション内の「次へ」リンクを返す。なければ None。"""
    nav = soup.select_one("div.pagination.pagination_set-nav")
    for a in (nav if nav else soup).select("a"):
        if a.get_text(strip=True) == "次へ":
            href = a.get("href", "")
            return href if href.startswith("http") else f"https://suumo.jp{href}"
    return None


def _extract_total_count(soup: BeautifulSoup) -> Optional[int]:
    """
    検索結果ページから総件数を読み取る。
    div.pagination_set-hit（例: "226 件"）から数値を抽出する。
    要素が見つからない・数値が取れない場合は None を返す（例外は出さない。
    呼び出し側が None を「読み取り失敗」として安全なデフォルトに
    フォールバックする設計のため）。
    """
    el = soup.select_one("div.pagination_set-hit")
    if not el:
        return None
    m = re.search(r'([\d,]+)', el.get_text(strip=True))
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _calculate_pages_needed(total_count: Optional[int]) -> int:
    """
    総件数から必要な取得ページ数を計算する。

    - total_count が None（読み取り失敗）なら FALLBACK_PAGES を返す
      （固定ページ数だった頃の実績値。安全なデフォルト）。
    - 計算結果が MAX_PAGES（安全上限）を超えるなら MAX_PAGES で
      打ち切り、警告ログを出す（検索条件の異常や想定外の大量ヒットで
      アクセス数が際限なく増えるのを防ぐため）。
    """
    if total_count is None:
        print(
            f"  [警告] 総件数の読み取りに失敗。"
            f"デフォルト{FALLBACK_PAGES}ページで続行します。",
            flush=True,
        )
        return FALLBACK_PAGES

    needed = math.ceil(total_count / ITEMS_PER_PAGE)
    if needed > MAX_PAGES:
        print(
            f"  [警告] 総件数{total_count}件は安全上限"
            f"({MAX_PAGES}ページ={MAX_PAGES * ITEMS_PER_PAGE}件)を超えています。"
            f"上限まで取得します。",
            flush=True,
        )
        return MAX_PAGES
    return needed


def scrape(start_url: str) -> list[Listing]:
    all_listings: list[Listing] = []
    url: Optional[str] = start_url
    effective_pages = FALLBACK_PAGES  # 1ページ目取得前の初期値（安全側のデフォルト）

    for page in range(1, MAX_PAGES + 1):  # MAX_PAGESは絶対に超えない安全上限
        print(f"  ページ {page} 取得中: {url}", flush=True)
        try:
            soup = fetch_page(url)
        except requests.RequestException as e:
            print(f"  [警告] ページ取得失敗: {e}", flush=True)
            break

        # 1ページ目取得直後に総件数を読み取り、必要ページ数を決定する。
        # 読み取りに成功しても失敗しても、このページの parse_listings は
        # 必ず実行・保持する（読み取り失敗時に「0ページ取得」になることを
        # 避けるための二重防御）。
        if page == 1:
            total_count = _extract_total_count(soup)
            effective_pages = _calculate_pages_needed(total_count)
            if total_count is not None:
                print(
                    f"  総件数: {total_count}件 → 取得予定ページ数: {effective_pages}",
                    flush=True,
                )

        listings = parse_listings(soup)
        print(f"  → {len(listings)} 件パース", flush=True)
        all_listings.extend(listings)

        if page >= effective_pages:
            break

        next_url = get_next_page_url(soup, url)
        if not next_url:
            break
        url = next_url
        time.sleep(REQUEST_INTERVAL)

    # URL 重複除去
    seen: set[str] = set()
    unique: list[Listing] = []
    for l in all_listings:
        if l.url not in seen:
            seen.add(l.url)
            unique.append(l)

    return unique


# ------------------------------------------------------------------ #
# 物件フィルタリング（URLパラメータで絞れない条件をPythonで処理）
# ------------------------------------------------------------------ #
def _parse_walk_minutes(station: str) -> Optional[int]:
    """「徒歩X分」をパースして分数を返す。取得不可なら None。"""
    m = re.search(r'徒歩\s*(\d+)\s*分', station)
    return int(m.group(1)) if m else None


def _parse_area_m2(area: str) -> Optional[float]:
    """「XX.XXm2」をパースして㎡数を返す。取得不可なら None。"""
    m = re.search(r'([\d.]+)\s*m', area)
    return float(m.group(1)) if m else None


def _parse_age_years(age: str) -> Optional[int]:
    """「YYYY年M月」から築年数（年）を計算して返す。取得不可なら None。"""
    import datetime
    m = re.search(r'(\d{4})\s*年', age)
    if not m:
        return None
    built_year = int(m.group(1))
    return datetime.date.today().year - built_year


def apply_filters(
    listings: list[Listing],
    max_walk_min: int = 7,
    min_area_m2: float = 65.0,
    min_age_years: int = 10,
    max_age_years: int = 25,
) -> list[Listing]:
    """
    スクレイピング後に徒歩・面積・築年数でフィルタリング。
    値が取得できない物件は条件通過とみなす（見逃し防止）。
    """
    passed, skipped = [], []
    for l in listings:
        walk = _parse_walk_minutes(l.station)
        area = _parse_area_m2(l.area)
        age  = _parse_age_years(l.age)

        if walk is not None and walk > max_walk_min:
            skipped.append((l.name[:20], f"徒歩{walk}分"))
            continue
        if area is not None and area < min_area_m2:
            skipped.append((l.name[:20], f"{area}㎡"))
            continue
        if age is not None and not (min_age_years <= age <= max_age_years):
            skipped.append((l.name[:20], f"築{age}年"))
            continue
        passed.append(l)

    if skipped:
        print(f"  [フィルタ除外] {len(skipped)} 件:", flush=True)
        for name, reason in skipped:
            print(f"    - {name}… ({reason})", flush=True)
    return passed


# ------------------------------------------------------------------ #
# CSV 操作
# ------------------------------------------------------------------ #
CSV_FIELDNAMES = [f.name for f in fields(Listing)]


def load_known_urls(path: str) -> set[str]:
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return {row["url"] for row in reader if row.get("url")}


def save_listings(path: str, listings: list[Listing]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        writer.writerows(l.to_dict() for l in listings)


# ------------------------------------------------------------------ #
# Gemini AI 評価
# ------------------------------------------------------------------ #
_SYSTEM_PROMPT = """あなたはプロの不動産投資家です。以下の物件情報から、ヤドカリ投資（数年後に家族が独立したタイミングで売却し、残債を一括返済する前提）としての「流動性」と「資産価値の維持」を1〜5の星で厳しく評価してください。

【評価基準】
- 駅徒歩5分以内は高く評価（7分ギリギリは少し減点）。
- 「管理計画認定」「新耐震基準」「省エネ基準適合」などの記載があれば大幅加点。
- 予算（5500万以内）に対して、4人家族が住める広さ（3LDKなど）が確保されているか（コスパ）。
- 新築プレミアムがなく、現在が価格の底堅い時期（築15年前後）であるか。
- 築10年未満は新築プレミアム残存リスクあり、築26年以上は旧耐震リスクありとして減点。

【出力形式（厳守）】
総合評価：★★★★☆ (4/5)
ヤドカリ投資メリット：[流動性・資産価値維持の観点から具体的な強みを60文字以内で]
懸念点：[売却時のリスクや資産価値の弱点を40文字以内で]
数年後売却ポテンシャル：[高い／普通／低い のいずれかと、その根拠を30文字以内で]
判定：[★4以上→「即内覧推奨」、★5→「滅多に出ないお宝物件」、★3以下→「様子見」]"""


def evaluate_listing(listing: Listing) -> tuple[int, str]:
    """Gemini で物件を評価し (スコア, 評価テキスト) を返す。失敗時は (0, "")。"""
    if not GEMINI_API_KEY:
        print("  [警告] GEMINI_API_KEY 未設定のため AI 評価をスキップします。", flush=True)
        return (0, "")

    client = genai.Client(api_key=GEMINI_API_KEY)

    prompt = "\n".join([
        f"物件名: {listing.name}",
        f"価格: {listing.price}",
        f"所在地: {listing.location}",
        f"沿線・駅: {listing.station or '不明'}",
        f"間取り: {listing.floor_plan or '不明'}",
        f"専有面積: {listing.area or '不明'}",
        f"築年月: {listing.age or '不明'}",
    ])

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=_SYSTEM_PROMPT,
                ),
            )
            text = response.text.strip()
            m = re.search(r'\((\d)/5\)', text)
            score = int(m.group(1)) if m else 0
            print(f"  [AI] {score}★ {listing.name[:25]}", flush=True)
            return (score, text)
        except Exception as e:
            print(f"  [警告] Gemini 評価失敗 (試行 {attempt}/{max_retries}): {e}", flush=True)
            if attempt < max_retries:
                print("  [リトライ] 60秒待機後に再試行します...", flush=True)
                time.sleep(60)
    return (0, "")


# ------------------------------------------------------------------ #
# LINE Messaging API (Push)
# ------------------------------------------------------------------ #
LINE_API_URL = "https://api.line.me/v2/bot/message/push"
_MAX_MESSAGES_PER_CALL = 5
# AI 評価テキスト分だけ文字数が増えるため 1 メッセージあたり 3 件に絞る
_LISTINGS_PER_MESSAGE = 3


def _build_text(items: list[tuple[Listing, str]], offset: int) -> str:
    lines = []
    for idx, (l, eval_text) in enumerate(items, start=offset + 1):
        parts = [f"【{idx}】{l.name}", f"  価格 : {l.price}", f"  所在地: {l.location}"]
        if l.station:
            parts.append(f"  沿線 : {l.station}")
        if l.floor_plan or l.area:
            parts.append(f"  間取り: {l.floor_plan}  {l.area}".strip())
        if l.age:
            parts.append(f"  築年月: {l.age}")
        parts.append("  ――――――――――")
        # 評価テキスト（複数行ある場合は各行に空白インデント）
        for eval_line in eval_text.splitlines():
            parts.append(f"  {eval_line}")
        parts.append(f"  URL  : {l.url}")
        lines.append("\n".join(parts))
    return "\n\n".join(lines)


def notify_line_text(text: str) -> None:
    """任意のテキスト1件を LINE に Push する。"""
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_USER_ID:
        print("[警告] LINE 認証情報が未設定のため通知をスキップします。", flush=True)
        return
    payload = {
        "to": LINE_USER_ID,
        "messages": [{"type": "text", "text": text}],
    }
    resp = requests.post(
        LINE_API_URL,
        headers={
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=10,
    )
    print(f"LINE送信結果: {resp.status_code} - {resp.text}", flush=True)


def notify_line(scored_listings: list[tuple[Listing, str]]) -> None:
    """AI 評価付き物件リストを LINE に Push する。"""
    if not LINE_CHANNEL_ACCESS_TOKEN:
        print("[警告] LINE_CHANNEL_ACCESS_TOKEN が未設定のため通知をスキップします。", flush=True)
        return
    if not LINE_USER_ID:
        print("[警告] LINE_USER_ID が未設定のため通知をスキップします。", flush=True)
        return

    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    message_texts: list[str] = []
    message_texts.append(
        f"🏠 SUUMO 新着（AI評価 4〜5★）{len(scored_listings)} 件！"
    )

    for i in range(0, len(scored_listings), _LISTINGS_PER_MESSAGE):
        chunk = scored_listings[i : i + _LISTINGS_PER_MESSAGE]
        message_texts.append(_build_text(chunk, i))

    for batch_start in range(0, len(message_texts), _MAX_MESSAGES_PER_CALL):
        batch = message_texts[batch_start : batch_start + _MAX_MESSAGES_PER_CALL]
        payload = {
            "to": LINE_USER_ID,
            "messages": [{"type": "text", "text": t} for t in batch],
        }
        resp = requests.post(LINE_API_URL, headers=headers, json=payload, timeout=10)
        print(f"LINE送信結果: {resp.status_code} - {resp.text}", flush=True)
        time.sleep(1)


# ------------------------------------------------------------------ #
# 価格変動通知（値下げ・スコア改善アラート）
# ------------------------------------------------------------------ #

def _build_text_price_drop(alert: dict) -> str:
    """値下げ・スコア改善アラートの本文を1件分組み立てる。"""
    today = alert["today"]
    prev  = alert["prev"]
    drop  = alert["price_drop"]   # 正の値 = 価格が下がった（円）
    gain  = alert["score_gain"]   # 正の値 = スコアが上がった（点）

    parts = [f"【{alert['name']}】"]

    # 価格の変化
    t_price = today.get("asking_price")
    p_price = prev.get("asking_price")
    if t_price is not None and p_price is not None:
        t_man    = t_price / 10_000
        p_man    = p_price / 10_000
        drop_man = drop / 10_000
        drop_pct = drop / p_price * 100 if p_price else 0
        if drop > 0:
            parts.append(f"  価格  : {p_man:.0f}万円 → {t_man:.0f}万円")
            parts.append(f"          ↓ -{drop_man:.0f}万円（-{drop_pct:.1f}%）")
        else:
            parts.append(f"  価格  : {t_man:.0f}万円（変化なし）")

    # スコアの変化
    t_score = today.get("resale_score")
    p_score = prev.get("resale_score")
    if t_score is not None and p_score is not None:
        if gain > 0:
            parts.append(f"  スコア: {t_score}/100（前回 {p_score} → +{gain}点）")
        elif gain < 0:
            parts.append(f"  スコア: {t_score}/100（前回 {p_score} → {gain}点）")
        else:
            parts.append(f"  スコア: {t_score}/100（変化なし）")

    parts.append(f"  URL   : {alert['url']}")
    parts.append(
        f"  前回  : {prev.get('evaluated_date', '?')} / "
        f"今回: {today.get('evaluated_date', '?')}"
    )
    return "\n".join(parts)


def _price_change_dedup_args(alert: dict) -> tuple:
    """
    アラート1件から、evaluator.is_price_change_notified /
    mark_price_change_notified に渡す引数（url, 旧価格, 新価格, 旧スコア,
    新スコア）を取り出す。両関数を呼ぶ箇所で同じ抽出ロジックを重複させ
    ないための共通ヘルパー。
    """
    prev  = alert["prev"]
    today = alert["today"]
    return (
        alert["url"],
        prev.get("asking_price"), today.get("asking_price"),
        prev.get("resale_score"), today.get("resale_score"),
    )


def _filter_unnotified_price_changes(alerts: list[dict], db_path=None) -> list[dict]:
    """
    detect_changes の結果から、既に同じ内容（URL＋旧価格→新価格＋
    旧スコア→新スコアの組）で通知済みの変化を除外する。

    ここではマーキング（通知済みとして記録すること）は行わない。
    マーキングは notify_line_price_drops が LINE 送信に成功した後にのみ
    行う（値下げ情報は「今しか使えない情報」のため、送信失敗時に
    「通知済みだが実際は届いていない」状態を避ける設計）。

    detect_changes 自体（何を変化とみなすかの判定）はここでは変更しない。
    このフィルタは detect_changes の呼び出し直後にかけるだけの追加の層。
    """
    from evaluator import is_price_change_notified
    result = []
    for alert in alerts:
        url, prev_price, today_price, prev_score, today_score = _price_change_dedup_args(alert)
        if is_price_change_notified(url, prev_price, today_price, prev_score, today_score, db_path=db_path):
            continue  # 同じ変化を過去に通知済み → 今回は通知しない
        result.append(alert)
    return result


def notify_line_price_drops(alerts: list[dict], db_path=None) -> None:
    """
    値下げ・スコア改善アラートを LINE に一括通知する。

    LINE送信が成功したバッチに含まれるアラートのみ、通知済みとして
    マーキングする（mark_price_change_notified）。送信が失敗した
    バッチのアラートはマーキングされないため、次回の実行で
    _filter_unnotified_price_changes を通っても除外されず、
    再度通知が試みられる。
    """
    if not alerts:
        return
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_USER_ID:
        print("[警告] LINE 認証情報が未設定のため価格変動通知をスキップします。", flush=True)
        return

    from evaluator import mark_price_change_notified

    # message_texts[0] はヘッダー行（対応するアラートなし）。
    # message_texts[i+1] が alerts[i] に対応する。
    message_texts: list[str] = [f"📉 価格変動のお知らせ（{len(alerts)}件）"]
    message_texts.extend(_build_text_price_drop(a) for a in alerts)
    corresponding_alerts: list[Optional[dict]] = [None] + list(alerts)

    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    for batch_start in range(0, len(message_texts), _MAX_MESSAGES_PER_CALL):
        batch_end   = batch_start + _MAX_MESSAGES_PER_CALL
        batch       = message_texts[batch_start:batch_end]
        batch_alerts = corresponding_alerts[batch_start:batch_end]

        payload = {
            "to": LINE_USER_ID,
            "messages": [{"type": "text", "text": t} for t in batch],
        }
        resp = requests.post(LINE_API_URL, headers=headers, json=payload, timeout=10)
        print(f"LINE送信結果（価格変動）: {resp.status_code} - {resp.text}", flush=True)

        if resp.status_code == 200:
            for alert in batch_alerts:
                if alert is not None:
                    url, prev_price, today_price, prev_score, today_score = _price_change_dedup_args(alert)
                    mark_price_change_notified(
                        url, prev_price, today_price, prev_score, today_score, db_path=db_path,
                    )

        time.sleep(1)


# ------------------------------------------------------------------ #
# 2段階通知（有望物件＝強調版 / それ以外＝控えめ版）
# ------------------------------------------------------------------ #

def _is_promising(est: dict) -> bool:
    """
    有望物件かどうか判定する。
    - resale_score が PROMISING_SCORE_THRESHOLD 以上
    - asking_vs_fair_pct が PROMISING_VS_FAIR_MAX_PCT 以下（None なら無視）
    """
    score = est.get("resale_score")
    if score is None or score < PROMISING_SCORE_THRESHOLD:
        return False
    vs_fair = est.get("asking_vs_fair_pct")
    if vs_fair is not None and vs_fair > PROMISING_VS_FAIR_MAX_PCT:
        return False
    return True


def _format_listing_age(age_days: Optional[int]) -> Optional[str]:
    """
    確認継続日数の表示文字列を返す。履歴なし（age_days=None）のときは None を返し、
    呼び出し側で「行を出さない」判断に使う。

    ※ SUUMO の掲載日ではなく「このボットが観測し始めてからの日数」であることが
      伝わる文言にしている（実掲載日との誤解を防ぐため）。
        age_days = 0  → "本日はじめて確認"
        age_days >= 1 → "確認してから N日目"
    """
    if age_days is None:
        return None
    if age_days <= 0:
        return "本日はじめて確認"
    return f"確認してから {age_days}日目"


def _build_text_promising(
    listing: Listing,
    eval_text: str,
    est: dict,
    idx: int,
    age_days: Optional[int] = None,
    gemini_score: Optional[int] = None,
) -> str:
    """強調版メッセージ（有望物件用）。reinfolib の評価数値を冒頭に差し込む。"""
    hold_years = est.get("hold_years", 10)
    parts = ["★★ 有望物件 ★★", f"【{idx}】{listing.name}", ""]

    # Gemini の★数を1行追加する。scored（4★以上）のみが対象のため通常は
    # 必ず値が入るが、念のため None（未取得）のときは行自体を省略する
    # （評価が欠けても通知は止めないフォールバックを維持するため）。
    if gemini_score is not None:
        parts.append(f"AI評価: {gemini_score}★")

    score   = est.get("resale_score")
    vs_fair = est.get("asking_vs_fair_pct")
    future  = est.get("future_resale_price")
    if score is not None:
        parts.append(f"売りやすさスコア : {score}/100")
    if vs_fair is not None:
        direction = "割安" if vs_fair <= 0 else "割高"
        parts.append(f"実勢比          : {vs_fair:+.1f}%（{direction}）")
    if future is not None:
        parts.append(f"{hold_years}年後 想定売却額: 約{future / 10_000:.0f}万円")

    parts.append("")
    parts.append(f"価格 : {listing.price}")
    parts.append(f"所在地: {listing.location}")
    if listing.station:
        parts.append(f"沿線 : {listing.station}")
    if listing.floor_plan or listing.area:
        parts.append(f"間取り: {listing.floor_plan}  {listing.area}".strip())
    if listing.age:
        parts.append(f"築年月: {listing.age}")
    # 確認継続日数（履歴があれば1行。売れ残り判断の補助）
    age_line = _format_listing_age(age_days)
    if age_line:
        parts.append(age_line)
    parts.append("――――――――――")

    for line in eval_text.splitlines():
        parts.append(line)

    try:
        for note in json.loads(est.get("notes", "[]")):
            parts.append(f"⚠ {note}")
    except (json.JSONDecodeError, TypeError):
        pass

    parts.append(f"URL  : {listing.url}")
    return "\n".join(parts)


def _build_text_compact(
    listing: Listing,
    idx: int,
    eval_text: str = "",
    age_days: Optional[int] = None,
    gemini_score: Optional[int] = None,
    est: Optional[dict] = None,
) -> str:
    """控えめ版メッセージ（通常物件・評価スキップ物件用）。
    物件名・価格・駅徒歩・URL に加え、Gemini の★数・reinfolibのスコア/
    乖離率・懸念点が取得できればそれぞれ1行ずつ添える（懸念点は全文でなく
    抽出のみ）。いずれも取得できない場合は該当行を省略するだけで、
    通知自体は今まで通り出す（フォールバック維持）。
    eval_text 未指定（""）や懸念点なしのときは従来通り懸念点行を足さない。"""
    station_short = listing.station.split()[-1] if listing.station else ""
    detail = " / ".join(filter(None, [listing.price, listing.floor_plan, station_short]))
    parts = [f"【{idx}】{listing.name}", f"  {detail}"]

    # Gemini の★数を1行追加する（取得できなければ省略）
    if gemini_score is not None:
        parts.append(f"  AI評価: {gemini_score}★")

    # reinfolib のスコア・乖離率を1行にまとめて追加する（参考枠と同じ表現）。
    # est が None、またはスコア・乖離率がどちらも取得できない場合は
    # 「データ評価」行自体を出さない。
    if est is not None:
        score   = est.get("resale_score")
        vs_fair = est.get("asking_vs_fair_pct")
        data_bits = []
        if score is not None:
            data_bits.append(f"スコア{score}/100")
        if vs_fair is not None:
            direction = "割安" if vs_fair <= 0 else "割高"
            data_bits.append(f"実勢比 {vs_fair:+.1f}%（{direction}）")
        if data_bits:
            parts.append(f"  データ評価: {' ・ '.join(data_bits)}")

    # Gemini 評価テキストから「懸念点：xxx」の行だけ抜き出して添える。
    # 全角／半角コロン両対応。`.` は改行を含まないので懸念点の1行だけ取得する。
    # 抽出できない（空文字・懸念点なし）ときは何も足さない＝落ちない。
    m = re.search(r'懸念点[：:]\s*(.+)', eval_text)
    if m and m.group(1).strip():
        parts.append(f"  ⚠ 懸念点: {m.group(1).strip()}")

    # 確認継続日数（履歴があれば URL の直前に1行）
    age_line = _format_listing_age(age_days)
    if age_line:
        parts.append(f"  {age_line}")

    parts.append(f"  URL: {listing.url}")
    return "\n".join(parts)


_COMPACT_PER_MESSAGE = 5  # 控えめ版は1メッセージに最大5件まとめる


def notify_line_two_stage(
    scored: list[tuple[Listing, str]],
    est_map: dict[str, dict],
    gemini_score_map: Optional[dict[str, int]] = None,
) -> None:
    """
    2段階LINE通知。
    - 有望物件（_is_promising=True） → 強調版、1件1メッセージ
    - それ以外（評価スキップ含む） → 控えめ版、最大5件まとめて1メッセージ
    est_map が空のとき（評価失敗フォールバック）は全件が控えめ版になる。

    gemini_score_map: 物件URL → Gemini★数。scored（4★以上）は本来常に
                      値を持つはずだが、未指定・URLが無い場合は該当物件の
                      「AI評価」行を省略するだけで通知自体は止めない。
    """
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_USER_ID:
        print("[警告] LINE 認証情報が未設定のため通知をスキップします。", flush=True)
        return

    # ミュータブルなデフォルト引数を避けるため、呼び出しごとにここで解決する
    if gemini_score_map is None:
        gemini_score_map = {}

    # 各物件の「観測開始からの日数」を DB 履歴から引く。
    # DBなし・履歴なし・例外は None（＝表示しない）になり、通知は止めない。
    try:
        from evaluator import get_listing_age_days  # 循環インポート回避
        age_map = {l.url: get_listing_age_days(l.url) for l, _ in scored}
    except Exception as e:
        print(f"[警告] 確認継続日数の取得に失敗（通知は継続）: {e}", flush=True)
        age_map = {}

    promising = [(l, t) for l, t in scored if _is_promising(est_map.get(l.url, {}))]
    normal    = [(l, t) for l, t in scored if not _is_promising(est_map.get(l.url, {}))]

    message_texts: list[str] = []

    header = f"🏠 SUUMO 新着 {len(scored)}件"
    if promising:
        header += f"（うち有望物件 {len(promising)}件）"
    message_texts.append(header)

    for rank, (listing, eval_text) in enumerate(promising, start=1):
        message_texts.append(
            _build_text_promising(
                listing, eval_text, est_map.get(listing.url, {}), rank,
                age_days=age_map.get(listing.url),
                gemini_score=gemini_score_map.get(listing.url),
            )
        )

    if normal:
        offset = len(promising)
        compact_parts = [
            _build_text_compact(
                l, offset + i + 1, eval_text, age_days=age_map.get(l.url),
                gemini_score=gemini_score_map.get(l.url), est=est_map.get(l.url),
            )
            for i, (l, eval_text) in enumerate(normal)
        ]
        for i in range(0, len(compact_parts), _COMPACT_PER_MESSAGE):
            message_texts.append(
                "\n\n".join(compact_parts[i : i + _COMPACT_PER_MESSAGE])
            )

    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    for batch_start in range(0, len(message_texts), _MAX_MESSAGES_PER_CALL):
        batch = message_texts[batch_start : batch_start + _MAX_MESSAGES_PER_CALL]
        payload = {
            "to": LINE_USER_ID,
            "messages": [{"type": "text", "text": t} for t in batch],
        }
        resp = requests.post(LINE_API_URL, headers=headers, json=payload, timeout=10)
        print(f"LINE送信結果: {resp.status_code} - {resp.text}", flush=True)
        time.sleep(1)


# ------------------------------------------------------------------ #
# 参考枠通知（Gemini 4★未満だが reinfolib 有望な物件を別枠で控えめに通知）
#   ※ 既存の2段階通知（強調版/控えめ版）とは独立した第3カテゴリ。
#      Gemini の一次選別という設計思想を尊重し、強調版とは混同させない。
# ------------------------------------------------------------------ #

_REFERENCE_HEADER = (
    "📋 参考（AI評価対象外・データ上は割安）\n"
    "※AIの自動抽出（4★以上）には入りませんでしたが、市場データ上は\n"
    "　割安圏の物件です。AIの評価理由も併記します。過信にご注意ください。"
)


def _build_text_reference(
    listing: Listing,
    gemini_score: int,
    eval_text: str,
    est: dict,
    idx: int,
    age_days: Optional[int] = None,
) -> str:
    """
    参考枠メッセージの1物件分を組み立てる。
    Gemini が 4★未満（自動抽出の対象外）としたが reinfolib 評価では有望な物件を、
    「データ（reinfolib）」と「AI 判断（Gemini）」の両方が見える形で控えめに示す。
    過信を防ぐため、AI の★数と懸念点を必ず併記する。
    """
    parts = [f"【{idx}】{listing.name}", f"  価格 : {listing.price}"]

    # reinfolib のデータ評価（スコア・実勢比）
    score   = est.get("resale_score")
    vs_fair = est.get("asking_vs_fair_pct")
    data_bits = []
    if score is not None:
        data_bits.append(f"スコア{score}/100")
    if vs_fair is not None:
        direction = "割安" if vs_fair <= 0 else "割高"
        data_bits.append(f"実勢比 {vs_fair:+.1f}%（{direction}）")
    if data_bits:
        parts.append(f"  データ評価: {' ・ '.join(data_bits)}")

    # AI（Gemini）の判断を必ず併記。★数と懸念点で「なぜ自動抽出外だったか」を示す（過信防止）。
    if gemini_score >= 1:
        ai_line = f"  AI評価: {gemini_score}★（5段階）"
    else:
        ai_line = "  AI評価: 判定できず（AI応答なし）"
    # eval_text から「懸念点：xxx」の行だけ抜き出して添える（抽出できなければ節を省略）
    m = re.search(r'懸念点[：:]\s*(.+)', eval_text)
    if m and m.group(1).strip():
        ai_line += f" / 懸念点: {m.group(1).strip()}"
    parts.append(ai_line)

    # 掲載日数（前回追加した _format_listing_age を流用。履歴があれば1行）
    age_line = _format_listing_age(age_days)
    if age_line:
        parts.append(f"  {age_line}")

    parts.append(f"  URL : {listing.url}")
    return "\n".join(parts)


def notify_line_reference(
    rejected: list[tuple[Listing, int, str]],
    est_map: dict[str, dict],
) -> None:
    """
    参考枠通知（独立した第3カテゴリ）。
    Gemini 4★未満（自動抽出の対象外）だが reinfolib 評価で有望な物件だけを、
    強調版とは別の控えめな見出しで通知する。

    引数:
        rejected: (listing, gemini★, eval_text) のリスト。
                  scored（4★以上）に入らなかった物件のみが渡される。
        est_map : 物件URL → reinfolib 評価結果。

    仕様:
        - reinfolib 有望（_is_promising）に該当する物件が0件なら、
          メッセージは一切送信しない（ログのみ）。
        - rejected は scored に含まれない物件のみなので、同一物件が
          強調版/控えめ版と参考枠の両方に出ることはない。
    """
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_USER_ID:
        print("[警告] LINE 認証情報が未設定のため参考枠通知をスキップします。", flush=True)
        return

    # reinfolib 有望なものだけを抽出（ここが参考枠の対象）
    targets = [
        (listing, gemini_score, eval_text)
        for listing, gemini_score, eval_text in rejected
        if _is_promising(est_map.get(listing.url, {}))
    ]
    if not targets:
        print("参考枠（AI対象外×データ割安）の該当はありませんでした。", flush=True)
        return

    # 掲載日数を DB 履歴から引く（失敗しても通知は止めない）
    try:
        from evaluator import get_listing_age_days  # 循環インポート回避
        age_map = {l.url: get_listing_age_days(l.url) for l, _, _ in targets}
    except Exception as e:
        print(f"[警告] 参考枠の確認継続日数の取得に失敗（通知は継続）: {e}", flush=True)
        age_map = {}

    message_texts: list[str] = [_REFERENCE_HEADER]
    blocks = [
        _build_text_reference(
            listing, gemini_score, eval_text,
            est_map.get(listing.url, {}), i + 1,
            age_days=age_map.get(listing.url),
        )
        for i, (listing, gemini_score, eval_text) in enumerate(targets)
    ]
    # 控えめ版と同様、_COMPACT_PER_MESSAGE 件ずつ束ねる
    for i in range(0, len(blocks), _COMPACT_PER_MESSAGE):
        message_texts.append("\n\n".join(blocks[i : i + _COMPACT_PER_MESSAGE]))

    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    for batch_start in range(0, len(message_texts), _MAX_MESSAGES_PER_CALL):
        batch = message_texts[batch_start : batch_start + _MAX_MESSAGES_PER_CALL]
        payload = {
            "to": LINE_USER_ID,
            "messages": [{"type": "text", "text": t} for t in batch],
        }
        resp = requests.post(LINE_API_URL, headers=headers, json=payload, timeout=10)
        print(f"LINE送信結果（参考枠）: {resp.status_code} - {resp.text}", flush=True)
        time.sleep(1)


def _find_reference_candidates(
    rejected: list[tuple[Listing, int, str]],
    est_map: dict[str, dict],
    db_path=None,
) -> list[tuple[Listing, int, str]]:
    """
    rejected のうち reinfolib 有望かつ「まだ参考枠で通知していない」ものだけを返す。

    【背景】
        従来、参考枠（notify_line_reference）は new_listings（新着）だけを
        対象にしていたため、一度 Gemini に4★未満をつけられた既知物件は、
        その後 reinfolib 評価がどれだけ改善しても二度と参考枠に浮上
        できなかった（実際に調布市の物件で確認された欠陥）。
        この関数は、新着分だけでなく既知物件分（gemini_cache に保存済みの
        Gemini評価と組み合わせたもの）も含めた rejected を受け取り、
        reinfolib 有望フィルタと重複抑制を行った上で、実際に通知すべき
        ものだけに絞り込む。

    重複抑制:
        一度でも参考枠として通知した URL は、以後ずっと抑制する
        （有望/非有望の二値判定であり、指値候補の強気度のような
        段階的な指標がないための単純な方式）。

    notify_line_reference 自体は変更しない。この関数は notify_line_reference
    を呼ぶ「前」に候補を絞り込む事前フィルタとして機能する
    （notify_line_reference は渡された rejected に対して同じ
    _is_promising フィルタを内部でも行うため、二重フィルタになるが
    害はない。既存の notify_line_reference のテスト・契約を一切
    変更しないためにこの設計にしている）。

    引数:
        rejected: (listing, gemini★, eval_text) のリスト。新着分(new_rejected)
                  と既知物件分(known_rejected)を呼び出し側で結合して渡す想定。
        est_map : 物件URL → reinfolib 評価結果（current 全件をカバーしている
                  必要がある。new_listings だけに絞ったものを渡すと既知物件が
                  必ず非有望judgeになってしまうので注意）。
        db_path : テスト用。None なら evaluator.DB_PATH を使う。

    戻り値:
        通知すべき (listing, gemini★, eval_text) のリスト。
        該当0件なら空リスト（例外は投げない）。
    """
    from evaluator import is_reference_notified, mark_reference_notified

    targets = [
        (listing, gemini_score, eval_text)
        for listing, gemini_score, eval_text in rejected
        if _is_promising(est_map.get(listing.url, {}))
    ]

    result: list[tuple[Listing, int, str]] = []
    for listing, gemini_score, eval_text in targets:
        if is_reference_notified(listing.url, db_path=db_path):
            continue  # 既に参考枠で通知済み → 再通知しない
        result.append((listing, gemini_score, eval_text))
        mark_reference_notified(listing.url, db_path=db_path)

    return result


# ------------------------------------------------------------------ #
# 指値候補通知（独立した第4カテゴリ。sashine.py の STEP1〜3を組み合わせるだけ）
#   ※ 新しい計算式・しきい値はここでは作らない。sashine.find_sashine_candidate
#      の判定結果をそのまま使う。
# ------------------------------------------------------------------ #

_SASHINE_HEADER = (
    "💰 指値候補（現在は割高、指値なら有望域）\n"
    "※あくまで交渉の目安です。売主の事情により通らないこともあります。\n"
    "※日数はbot確認開始からの目安で、実際の掲載期間はより長い可能性があります"
)

_AGGRESSIVENESS_LABELS = {"aggressive": "強気", "standard": "標準", "mild": "控えめ"}


def _build_text_sashine(
    listing: Listing,
    found: dict,
    est_now,          # reinfolib_resale.ResaleEstimate（現在の売出価格での評価）
    age_days: Optional[int],
    idx: int,
) -> str:
    """指値候補1件分のメッセージを組み立てる。"""
    targets       = found["targets"]
    est_at_target = found["est_at_target"]
    agg_label     = _AGGRESSIVENESS_LABELS.get(found["aggressiveness"], found["aggressiveness"])

    vs_fair_now = est_now.asking_vs_fair_pct
    if vs_fair_now is not None:
        now_direction = "割安" if vs_fair_now <= 0 else "割高"
        now_vs_str = f"（乖離率 {vs_fair_now:+.1f}%・{now_direction}）"
    else:
        now_vs_str = ""

    parts = [f"【{idx}】{listing.name}", f"  売出価格 : {listing.price}{now_vs_str}"]

    age_line = _format_listing_age(age_days)
    if age_line:
        parts.append(f"  {age_line} → 強気度: {agg_label}")
    else:
        parts.append(f"  強気度: {agg_label}")

    parts.append("  --- 指値目安 ---")
    opening_man = targets["opening_offer"]   / 10_000
    target_man  = targets["target_price"]    / 10_000
    walk_man    = targets["walk_away_price"] / 10_000

    vs_fair_target = est_at_target.asking_vs_fair_pct
    target_vs_str = f"（指値後の乖離率 {vs_fair_target:+.1f}%・有望域）" if vs_fair_target is not None else ""

    parts.append(f"  初回提示    : {opening_man:.0f}万円")
    parts.append(f"  落としどころ : {target_man:.0f}万円{target_vs_str}")
    parts.append(f"  引き際      : {walk_man:.0f}万円")
    parts.append(f"  URL : {listing.url}")
    return "\n".join(parts)


def _find_sashine_candidates(
    city_groups: dict[str, list],
    detail_cache: Optional[dict],
    db_path=None,
) -> list[tuple]:
    """
    city_groups（市コード→Listingリスト）の全物件から指値候補を探す。

    evaluate_and_save と同じ手順（get_curve → suumo_to_candidate →
    estimate_resale）をもう一度実行して est_now を得る。sashine.py は
    DB行の形（dict）を一切知らない設計のため、DBから復元するのではなく
    ResaleEstimate をここで直接計算し直す（curve はファイルキャッシュ済み
    のため、再計算のコストは無視できるレベル）。

    重複抑制: 同じ強気度で既に通知済み（sashine_notifications テーブル）
    ならスキップする。強気度が変わったとき（例: standard→aggressive）
    だけ再通知の対象にする。

    例外はここでは捕まえない。呼び出し側 main() の try/except に
    隔離を任せる（指値候補判定の失敗が既存の通知を止めないようにするため）。

    引数:
        db_path: テスト用。None のとき（本番の呼び出し方）は
                 evaluator.DB_PATH（本番の evaluations.db）を使う。
                 evaluator.get_listing_age_days 等は db_path 引数が
                 「関数定義時に束縛されるデフォルト値」のため、テストで
                 一時DBに差し替えるにはここで明示的に db_path を
                 渡す必要がある（呼び出し側で上書きしないと本番DBに
                 書き込んでしまうため）。

    戻り値: [(Listing, found_dict, est_now, age_days), ...]
        found_dict は sashine.find_sashine_candidate の戻り値
        （aggressiveness / targets / est_at_target を含む）。
    """
    from datetime import date

    from build_curves import TARGET_AREAS, get_curve
    from evaluator import DB_PATH as _EVALUATOR_DB_PATH
    from evaluator import (
        DEFAULT_HOLD_YEARS,
        get_listing_age_days,
        get_sashine_notified_aggressiveness,
        mark_sashine_notified,
    )
    from reinfolib_resale import estimate_resale
    from sashine import find_sashine_candidate
    from suumo_adapter import suumo_to_candidate

    effective_db_path = db_path if db_path is not None else _EVALUATOR_DB_PATH

    code_to_name = {code: name for name, code in TARGET_AREAS.items()}
    year = date.today().year

    results: list[tuple] = []
    for city_code, listings_for_city in city_groups.items():
        city_name = code_to_name.get(city_code, city_code)
        curve = get_curve(city_name=city_name, city_code=city_code)
        if curve is None:
            continue  # カーブ取得不可のエリアはスキップ（evaluate_and_saveと同じ扱い）

        for listing in listings_for_city:
            candidate = suumo_to_candidate(
                listing,
                detail=detail_cache.get(listing.url) if detail_cache else None,
            )
            if candidate is None:
                continue

            est_now  = estimate_resale(candidate, curve, year, DEFAULT_HOLD_YEARS)
            age_days = get_listing_age_days(listing.url, db_path=effective_db_path)

            found = find_sashine_candidate(
                candidate, curve, year, DEFAULT_HOLD_YEARS, age_days, est_now
            )
            if found is None:
                continue

            # 重複抑制: 同じ強気度で既に通知済みならスキップする
            prev_aggressiveness = get_sashine_notified_aggressiveness(
                listing.url, db_path=effective_db_path
            )
            if prev_aggressiveness == found["aggressiveness"]:
                continue

            results.append((listing, found, est_now, age_days))
            mark_sashine_notified(listing.url, found["aggressiveness"], db_path=effective_db_path)

    return results


def notify_line_sashine_candidates(candidates: list[tuple]) -> None:
    """
    指値候補通知（独立した第4カテゴリ）。
    _find_sashine_candidates が返した候補（重複抑制フィルタ通過済み）を
    まとめて通知する。該当0件なら何も送信しない。
    """
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_USER_ID:
        print("[警告] LINE 認証情報が未設定のため指値候補通知をスキップします。", flush=True)
        return
    if not candidates:
        print("指値候補（AI評価とは独立の第4カテゴリ）の該当はありませんでした。", flush=True)
        return

    message_texts: list[str] = [_SASHINE_HEADER]
    blocks = [
        _build_text_sashine(listing, found, est_now, age_days, i + 1)
        for i, (listing, found, est_now, age_days) in enumerate(candidates)
    ]
    for i in range(0, len(blocks), _COMPACT_PER_MESSAGE):
        message_texts.append("\n\n".join(blocks[i : i + _COMPACT_PER_MESSAGE]))

    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    for batch_start in range(0, len(message_texts), _MAX_MESSAGES_PER_CALL):
        batch = message_texts[batch_start : batch_start + _MAX_MESSAGES_PER_CALL]
        payload = {
            "to": LINE_USER_ID,
            "messages": [{"type": "text", "text": t} for t in batch],
        }
        resp = requests.post(LINE_API_URL, headers=headers, json=payload, timeout=10)
        print(f"LINE送信結果（指値候補）: {resp.status_code} - {resp.text}", flush=True)
        time.sleep(1)


# ------------------------------------------------------------------ #
# エントリポイント
# ------------------------------------------------------------------ #
def main() -> None:
    print("=== 不動産スクレイパー 開始 ===", flush=True)
    print(f"対象URL: {TARGET_URL}", flush=True)

    # スクレイピング
    current = scrape(TARGET_URL)
    if not current:
        print("物件が取得できませんでした。セレクタを確認してください。", flush=True)
        sys.exit(1)
    print(f"合計取得: {len(current)} 件", flush=True)

    # Pythonフィルタ（徒歩7分以内・65㎡以上・築10〜25年）
    current = apply_filters(current, max_walk_min=7, min_area_m2=65.0, min_age_years=10, max_age_years=25)
    print(f"フィルタ後: {len(current)} 件", flush=True)

    # 差分比較
    known_urls = load_known_urls(DATA_FILE)
    new_listings = [l for l in current if l.url not in known_urls]
    print(f"新着: {len(new_listings)} 件 (既知: {len(known_urls)} 件)", flush=True)

    # 国交省評価（current 全件。新着がなくても値下げ検知のために毎日実行）
    # ・物件を所在市でグルーピングし、エリアごとに正しいカーブで評価する。
    # ・カーブはエリア単位キャッシュを使い回すため、物件数によらず
    #   API 呼び出しはエリア数ぶん（調布・府中・稲城 各1回）に抑えられる。
    # ・毎日 current 件数ぶんの行が DB に積まれる（価格変動追跡の意図した仕様）。
    # ・例外が出ても後続の通知（2段階・価格変動とも）に影響しない。
    price_drop_alerts: list[dict] = []
    est_map: dict[str, dict] = {}
    sashine_candidates: list[tuple] = []  # 指値候補（第4カテゴリ）。失敗時は空のまま
    try:
        from evaluator import evaluate_and_save, load_evaluations_today, detect_changes, resolve_city_code  # 循環インポート回避
        from detail_fetcher import fetch_detail, load_detail_cache, save_detail_cache, get_uncached_urls  # 循環インポート回避

        # ── 詳細取得（新着 & 未登録のみ）──────────────────────────────
        # new_listings のうち detail_cache に「一度も登録されていない」URLだけを取得する。
        # 登録済み（取得失敗で NULL の場合も含む）は再取得しない。
        # アクセス数 = 未登録の新着件数のみ（通常は当日の新着件数）。
        new_listing_urls  = [l.url for l in new_listings]
        uncached_urls_set = set(get_uncached_urls(new_listing_urls))
        already_cached_count = len(new_listing_urls) - len(uncached_urls_set)
        fetch_success_count  = 0
        fetch_fail_count     = 0
        for listing in new_listings:
            if listing.url not in uncached_urls_set:
                continue  # detail_cache に登録済みはスキップ（重複 fetch 防止）
            data = fetch_detail(listing.url)  # 内部で4秒待機・timeout=15秒
            if data is not None:
                fetch_success_count += 1
            else:
                fetch_fail_count += 1
            # 失敗（data=None）のときも「試み済み」として保存し、次回の重複 fetch を防ぐ
            save_detail_cache(listing.url, data or {"total_units": None, "repair_fund_monthly": None})

        # 集計ログ: 母数（新着件数）と内訳（既存キャッシュ対象外／新規fetch成功・失敗）を
        # 明示する。「0件成功」等の数字だけを見て「fetchが失敗した」と誤読されないよう、
        # 母数と内訳を必ずセットで出す（過去に誤解を招いた反省を踏まえた表現）。
        print(
            f"[詳細取得サマリ] 新着{len(new_listings)}件 → "
            f"キャッシュ既存(対象外){already_cached_count}件 / "
            f"新規fetch対象{len(uncached_urls_set)}件"
            f"（成功{fetch_success_count}件・失敗{fetch_fail_count}件）",
            flush=True,
        )

        # current 全件のキャッシュを DB から一括読み込み（新着は今保存、既知は以前保存）
        detail_cache = load_detail_cache([l.url for l in current])
        # ──────────────────────────────────────────────────────────────

        # 所在地から市区町村コードを判定してグルーピング
        city_groups: dict[str, list[Listing]] = {}
        skipped_city = 0
        for listing in current:
            code = resolve_city_code(listing.location)
            if code is not None:
                city_groups.setdefault(code, []).append(listing)
            else:
                print(f"  [警告] 市コード判定不可のためスキップ: {listing.location!r}", flush=True)
                skipped_city += 1
        # エリアごとに対応するカーブで評価して DB に保存（detail_cache を渡して精度向上）
        for city_code, listings_for_city in city_groups.items():
            evaluate_and_save(listings_for_city, city_code=city_code, detail_cache=detail_cache)
        print(f"市コード判定不可でスキップ: {skipped_city}件", flush=True)

        # ── 指値候補の判定（第4カテゴリ。current全件が対象）─────────────
        # 既存の評価パイプラインとは別に専用のtry/exceptで隔離する。
        # ここで例外が起きても、直後の値下げ検知や、この後の全ての
        # 通知（値下げ・2段階・参考枠）には一切影響しない。
        try:
            sashine_candidates = _find_sashine_candidates(city_groups, detail_cache)
        except Exception as e:
            print(f"[警告] 指値候補判定に失敗（既存の通知は継続）: {e}", flush=True)
            sashine_candidates = []

        price_drop_alerts = detect_changes([l.url for l in current])
        # 1日2回の定期実行で同じ変化が重複通知される事故を受け、既に同じ
        # 内容で通知済みの変化を除外する（detect_changes自体は無変更）。
        price_drop_alerts = _filter_unnotified_price_changes(price_drop_alerts)
        # est_map は参考枠（既知物件を含む）でも使うため current 全件に拡大する。
        # notify_line_two_stage は scored（new_listings由来のみ）しか参照しない
        # ため、この拡大は既存の2段階通知の挙動に影響しない。
        est_map = load_evaluations_today([l.url for l in current])
    except Exception as e:
        print(f"[警告] 評価パイプライン失敗（通知は継続）: {e}", flush=True)

    # ── data.csv 保存（Gemini評価より前に、必ず実行する）───────────────
    # 以前は main() の最後（Gemini評価・参考枠処理より後）で保存していたが、
    # 新着が大量発生した日にGemini評価ループが長時間化し、手動キャンセル
    # されたことで data.csv 保存自体が一度も実行されず、フィルタ通過した
    # 全物件の記録が失われる事故が実際に発生した。
    # 詳細取得・reinfolib評価の直後、時間のかかるGemini評価より前に移動し、
    # new_listings の有無に関わらず毎回実行することで、以降の処理が
    # 中断しても記録は必ず残るようにする。
    try:
        save_listings(DATA_FILE, current)
        print("data.csv を更新しました。", flush=True)
    except Exception as e:
        print(f"[警告] data.csv 保存に失敗: {e}", flush=True)

    # 値下げ・スコア改善通知（2段階通知より先に送る）
    if price_drop_alerts:
        notify_line_price_drops(price_drop_alerts)
    else:
        print("価格変動のある物件はありませんでした。", flush=True)

    # 指値候補通知（第4カテゴリ。current全件が対象のため new_listings の
    # 有無に関わらずここで送る。該当0件なら内部で何も送信しない）
    notify_line_sashine_candidates(sashine_candidates)

    # ── Gemini評価: 新着 + 優先評価対象（前回上限で見送った既知物件）──────
    # 1回のGemini呼び出し件数に上限(GEMINI_EVAL_LIMIT_PER_RUN)を設ける。
    # 新着が大量発生した日に全件評価しようとすると、無料枠のレート制限
    # （1日20リクエスト）に抵触して429が連鎖し、実行が異常に長時間化する
    # 事故が実際に発生したための対策。
    #
    # 優先度: 前回見送られた既知物件 → 今回の新着、の順で評価する
    # （新着が続くたびに古いバックログが際限なく後回しにされるのを防ぐ）。
    # 既知物件のうち gemini_evaluations 未登録のものは
    # backfill_gemini_evaluations.find_backfill_targets と全く同じ
    # ロジックで検出する（「未評価かどうか」の判定基準を二重管理しない）。
    scored:           list[tuple[Listing, str]]      = []
    new_rejected:     list[tuple[Listing, int, str]] = []  # (listing, gemini★, eval_text)
    gemini_score_map: dict[str, int] = {}  # 通知文面にAI評価の★数を表示するため保持

    new_listing_urls_now = {l.url for l in new_listings}
    known_listings_now    = [l for l in current if l.url not in new_listing_urls_now]
    try:
        from backfill_gemini_evaluations import find_backfill_targets  # 循環インポート回避
        unevaluated_known = find_backfill_targets(known_listings_now)
    except Exception as e:
        print(f"[警告] 優先評価対象の抽出に失敗（新着のみで継続）: {e}", flush=True)
        unevaluated_known = []

    eval_targets = unevaluated_known + new_listings  # 優先度順（既知の未評価分→新着）
    targets_now  = eval_targets[:GEMINI_EVAL_LIMIT_PER_RUN]
    skipped_now  = eval_targets[GEMINI_EVAL_LIMIT_PER_RUN:]
    unevaluated_known_urls = {l.url for l in unevaluated_known}

    print(
        f"[Gemini評価サマリ] 優先(未評価の既知物件){len(unevaluated_known)}件 + "
        f"新着{len(new_listings)}件 = 対象{len(eval_targets)}件",
        flush=True,
    )
    if skipped_now:
        print(
            f"[警告] Gemini評価件数が上限({GEMINI_EVAL_LIMIT_PER_RUN}件)を超えたため、"
            f"{len(skipped_now)}件は次回の実行に持ち越します。",
            flush=True,
        )

    if targets_now:
        print(f"Gemini で {len(targets_now)} 件を評価中...", flush=True)
        from gemini_cache import save_gemini_evaluation  # 循環インポート回避

        new_targets_processed = 0
        for listing in targets_now:
            score, eval_text = evaluate_listing(listing)
            gemini_score_map[listing.url] = score
            # 初回評価時のみ保存。以後この物件が既知物件になっても、この
            # 保存済みスコアを参考枠判定に再利用する（Gemini APIは呼ばない）。
            try:
                save_gemini_evaluation(listing.url, score, eval_text)
            except Exception as e:
                print(f"[警告] Gemini評価の保存に失敗（評価自体は継続）: {e}", flush=True)

            if listing.url in unevaluated_known_urls:
                # 優先評価された既知物件は、もう「新着」ではないため
                # 2段階通知(scored/new_rejected)には入れない。
                # gemini_evaluations への保存のみ行い、4★未満なら次回以降の
                # 参考枠判定で自然に拾われる（4★以上でも新着通知はしない）。
                pass
            else:
                new_targets_processed += 1
                if score >= 4:
                    scored.append((listing, eval_text))
                else:
                    # 4★未満は従来は捨てていた。参考枠判定のため (物件, ★数, 評価文) を保持する。
                    new_rejected.append((listing, score, eval_text))
            time.sleep(15)  # Gemini API TPM制限対策（無料枠: 429回避）

        skipped_low_score = new_targets_processed - len(scored)
        print(f"4★以上: {len(scored)} 件 / 3★以下スキップ: {skipped_low_score} 件", flush=True)

        if scored:
            notify_line_two_stage(scored, est_map, gemini_score_map)
        else:
            print("通知対象（4★以上）の物件はありませんでした。", flush=True)
    else:
        print("Gemini評価対象の物件はありませんでした。", flush=True)

    # ── 参考枠（第3カテゴリ。current全件が対象。new_listingsの有無に関わらず実行）──
    # 新着分(new_rejected) + 既知物件分（gemini_cache に保存済みの Gemini評価が
    # 4★未満のもの）を結合し、reinfolib有望フィルタ・重複抑制を通してから通知する。
    # これにより「新着時にGeminiに低評価をつけられた既知物件が、その後
    # reinfolib評価が改善しても二度と参考枠に出ない」という欠陥を解消する。
    # 専用try/exceptで隔離: ここが失敗しても新着分だけで参考枠を継続させる
    # （既存の新着ベースの参考枠を道連れにしない）。
    try:
        from gemini_cache import load_gemini_evaluations  # 循環インポート回避

        new_listing_urls_set = {l.url for l in new_listings}
        known_listings = [l for l in current if l.url not in new_listing_urls_set]
        known_gemini   = load_gemini_evaluations([l.url for l in known_listings])
        known_rejected = [
            (listing, known_gemini[listing.url][0], known_gemini[listing.url][1])
            for listing in known_listings
            if listing.url in known_gemini and known_gemini[listing.url][0] < 4
        ]
        all_rejected = new_rejected + known_rejected

        # 集計ログ: 母数（既知件数）と内訳（Gemini評価保存済み／未保存）を明示する。
        # 「読み込み0件」だけを見て不具合と誤読されないよう、「未保存＝この物件が
        # gemini_evaluations導入前から既知だったため対象外」という理由も添える。
        gemini_missing_count = len(known_listings) - len(known_gemini)
        print(
            f"[参考枠サマリ] 既知{len(known_listings)}件 → "
            f"Gemini評価保存済み{len(known_gemini)}件"
            f"（未保存{gemini_missing_count}件はgemini_evaluations未登録のため対象外） / "
            f"うち4★未満{len(known_rejected)}件",
            flush=True,
        )

        reference_candidates = _find_reference_candidates(all_rejected, est_map)
    except Exception as e:
        print(f"[警告] 参考枠の対象抽出に失敗（新着分のみで継続）: {e}", flush=True)
        reference_candidates = new_rejected

    # 参考枠通知。該当0件なら内部で何も送らない。既存の2段階通知には一切影響しない。
    notify_line_reference(reference_candidates, est_map)

    # data.csv は評価パイプライン直後（Gemini評価より前）で既に保存済み。
    print("=== 完了 ===", flush=True)


if __name__ == "__main__":
    main()

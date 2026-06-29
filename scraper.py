"""
不動産新着物件スクレイパー (SUUMO 中古マンション)
- 新着物件を取得し data.csv と差分比較
- Gemini API で物件を5段階評価し、4〜5★のみ LINE に通知
"""

import csv
import json
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

# 検索URLは環境変数で上書き可能
# 対象: 調布市・府中市 / 中古マンション / 4000〜5500万円
# ※徒歩・面積・築年数フィルターはURLパラメータ非対応のためPythonで後処理
DEFAULT_URL = (
    "https://suumo.jp/jj/bukken/ichiran/JJ010FJ001/"
    "?ar=030&bs=011&ta=13"
    "&sc=13207&sc=13209&sc=13211"   # 調布市・稲城市・府中市
    "&cb=4000.0&ct=5500.0" # 4000万〜5500万円
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

MAX_PAGES = 3          # 取得する最大ページ数
REQUEST_INTERVAL = 2   # ページ間のウェイト（秒）

# 有望物件の判定しきい値
PROMISING_SCORE_THRESHOLD = 70    # 売りやすさスコアの下限（100点満点）
PROMISING_VS_FAIR_MAX_PCT  = 5.0  # 実勢比乖離率の上限（+は割高）
EVAL_CITY_CODE = "13208"          # 評価に使うエリアコード（現時点は調布市のみ）


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


def scrape(start_url: str) -> list[Listing]:
    all_listings: list[Listing] = []
    url: Optional[str] = start_url

    for page in range(1, MAX_PAGES + 1):
        print(f"  ページ {page} 取得中: {url}", flush=True)
        try:
            soup = fetch_page(url)
        except requests.RequestException as e:
            print(f"  [警告] ページ取得失敗: {e}", flush=True)
            break

        listings = parse_listings(soup)
        print(f"  → {len(listings)} 件パース", flush=True)
        all_listings.extend(listings)

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


def notify_line_price_drops(alerts: list[dict]) -> None:
    """値下げ・スコア改善アラートを LINE に一括通知する。"""
    if not alerts:
        return
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_USER_ID:
        print("[警告] LINE 認証情報が未設定のため価格変動通知をスキップします。", flush=True)
        return

    # ヘッダー + 1件1メッセージで組み立て、_MAX_MESSAGES_PER_CALL 件ずつ送信
    message_texts: list[str] = [f"📉 価格変動のお知らせ（{len(alerts)}件）"]
    for alert in alerts:
        message_texts.append(_build_text_price_drop(alert))

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
        print(f"LINE送信結果（価格変動）: {resp.status_code} - {resp.text}", flush=True)
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


def _build_text_promising(
    listing: Listing,
    eval_text: str,
    est: dict,
    idx: int,
) -> str:
    """強調版メッセージ（有望物件用）。reinfolib の評価数値を冒頭に差し込む。"""
    hold_years = est.get("hold_years", 10)
    parts = ["★★ 有望物件 ★★", f"【{idx}】{listing.name}", ""]

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


def _build_text_compact(listing: Listing, idx: int) -> str:
    """控えめ版メッセージ（通常物件・評価スキップ物件用）。物件名・価格・駅徒歩・URL のみ。"""
    station_short = listing.station.split()[-1] if listing.station else ""
    detail = " / ".join(filter(None, [listing.price, listing.floor_plan, station_short]))
    return f"【{idx}】{listing.name}\n  {detail}\n  URL: {listing.url}"


_COMPACT_PER_MESSAGE = 5  # 控えめ版は1メッセージに最大5件まとめる


def notify_line_two_stage(
    scored: list[tuple[Listing, str]],
    est_map: dict[str, dict],
) -> None:
    """
    2段階LINE通知。
    - 有望物件（_is_promising=True） → 強調版、1件1メッセージ
    - それ以外（評価スキップ含む） → 控えめ版、最大5件まとめて1メッセージ
    est_map が空のとき（評価失敗フォールバック）は全件が控えめ版になる。
    """
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_USER_ID:
        print("[警告] LINE 認証情報が未設定のため通知をスキップします。", flush=True)
        return

    promising = [(l, t) for l, t in scored if _is_promising(est_map.get(l.url, {}))]
    normal    = [(l, t) for l, t in scored if not _is_promising(est_map.get(l.url, {}))]

    message_texts: list[str] = []

    header = f"🏠 SUUMO 新着 {len(scored)}件"
    if promising:
        header += f"（うち有望物件 {len(promising)}件）"
    message_texts.append(header)

    for rank, (listing, eval_text) in enumerate(promising, start=1):
        message_texts.append(
            _build_text_promising(listing, eval_text, est_map.get(listing.url, {}), rank)
        )

    if normal:
        offset = len(promising)
        compact_parts = [
            _build_text_compact(l, offset + i + 1)
            for i, (l, _) in enumerate(normal)
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
    # ・カーブはエリア単位キャッシュを使い回すため、物件数によらず
    #   API 呼び出しはエリア数ぶん（現時点では 1 回）に抑えられる。
    # ・毎日 current 件数ぶんの行が DB に積まれる（価格変動追跡の意図した仕様）。
    # ・例外が出ても後続の通知（2段階・価格変動とも）に影響しない。
    price_drop_alerts: list[dict] = []
    est_map: dict[str, dict] = {}
    try:
        from evaluator import evaluate_and_save, load_evaluations_today, detect_changes  # 循環インポート回避
        evaluate_and_save(current, city_code=EVAL_CITY_CODE)
        price_drop_alerts = detect_changes([l.url for l in current])
        # est_map は新着物件の2段階通知用にのみ使うため new_listings のURLに絞る
        est_map = load_evaluations_today([l.url for l in new_listings])
    except Exception as e:
        print(f"[警告] 評価パイプライン失敗（通知は継続）: {e}", flush=True)

    # 値下げ・スコア改善通知（2段階通知より先に送る）
    if price_drop_alerts:
        notify_line_price_drops(price_drop_alerts)
    else:
        print("価格変動のある物件はありませんでした。", flush=True)

    if not new_listings:
        print("新着物件はありませんでした。", flush=True)
        print("=== 完了 ===", flush=True)
        return

    # Gemini で評価 → 4〜5★のみ抽出
    print(f"Gemini で {len(new_listings)} 件を評価中...", flush=True)
    scored: list[tuple[Listing, str]] = []
    for listing in new_listings:
        score, eval_text = evaluate_listing(listing)
        if score >= 4:
            scored.append((listing, eval_text))
        time.sleep(15)  # Gemini API TPM制限対策（無料枠: 429回避）

    skipped = len(new_listings) - len(scored)
    print(f"4★以上: {len(scored)} 件 / 3★以下スキップ: {skipped} 件", flush=True)

    # LINE 通知（2段階）& CSV 保存（新着が1件でもあれば必ず保存）
    if scored:
        notify_line_two_stage(scored, est_map)
    else:
        print("通知対象（4★以上）の物件はありませんでした。", flush=True)

    save_listings(DATA_FILE, current)
    print("data.csv を更新しました。", flush=True)
    print("=== 完了 ===", flush=True)


if __name__ == "__main__":
    main()

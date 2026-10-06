from __future__ import annotations

import io
import json
import re
import time
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from google import genai
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer


st.set_page_config(page_title="PONTE SEO改善アプリ", page_icon="📈", layout="wide")

CSS = """
<style>
  .block-container {max-width: 980px; padding-top: 3.5rem; padding-bottom: 5rem;}
  h1 {font-size: clamp(1.75rem, 5vw, 2.6rem) !important; letter-spacing: .02em;}
  .subtle {color:#64748b; margin-bottom:1.8rem;}
  .stTextInput input {height: 3.15rem; border-radius: .75rem;}
  .stButton button {height:3.15rem; border-radius:.75rem; font-weight:700; width:100%;}
  [data-testid="stMetric"] {background:#f8fafc; padding:1rem; border-radius:.8rem; border:1px solid #e2e8f0;}
  .result-box {padding:1.15rem 1.25rem; border:1px solid #e2e8f0; border-radius:.85rem; background:#fff; margin:.5rem 0 1.5rem;}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)

DEFAULT_MODEL = "gemini-3.6-flash"
FALLBACK_MODELS = ("gemini-3.5-flash", "gemini-3.5-flash-lite")
RETIRED_MODELS = {"gemini-2.5-flash", "models/gemini-2.5-flash"}
UA = "PONTE-SEO-Analyzer/1.0 (+website quality audit)"
SITE_OPTIONS = {
    "ぽんて鍼灸整骨院": "https://ponte-nene.jp/",
    "ぽんてアロマサロン": "https://ponte-aroma.jp/",
    "ぽんておすすめブログ": "https://ponte-nene.net/",
}
GBP_PROFILE_NAMES = {
    "ぽんて鍼灸整骨院": "ぽんて鍼灸整骨院／ぽんてカイロプラクティックオフィス",
    "ぽんてアロマサロン": "Ponte Aroma Salon",
}
ANALYSIS_TARGETS = {
    "ぽんて鍼灸整骨院（サイト）": {
        "kind": "site", "url": SITE_OPTIONS["ぽんて鍼灸整骨院"], "name": "ぽんて鍼灸整骨院",
    },
    "ぽんてアロマサロン（サイト）": {
        "kind": "site", "url": SITE_OPTIONS["ぽんてアロマサロン"], "name": "ぽんてアロマサロン",
    },
    "ぽんておすすめブログ（サイト）": {
        "kind": "site", "url": SITE_OPTIONS["ぽんておすすめブログ"], "name": "ぽんておすすめブログ",
    },
    "ぽんて鍼灸整骨院（GBP）": {
        "kind": "gbp", "url": SITE_OPTIONS["ぽんて鍼灸整骨院"], "name": "ぽんて鍼灸整骨院",
        "gbp_profile_name": GBP_PROFILE_NAMES["ぽんて鍼灸整骨院"],
    },
    "ぽんてアロマサロン（GBP）": {
        "kind": "gbp", "url": SITE_OPTIONS["ぽんてアロマサロン"], "name": "ぽんてアロマサロン",
        "gbp_profile_name": GBP_PROFILE_NAMES["ぽんてアロマサロン"],
    },
}


def secret(name: str, default=""):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


def normalize_url(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("URLを入力してください。")
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value
    parsed = urlparse(value)
    if not parsed.netloc:
        raise ValueError("正しいURLを入力してください。")
    return value.rstrip("/") + "/"


def _clean_name(value) -> str:
    return re.sub(r"[\s_　]+", " ", str(value).replace("\ufeff", "").strip().lower())


def _read_csv_bytes(raw: bytes) -> pd.DataFrame:
    last_error = None
    for encoding in ("utf-8-sig", "cp932", "shift_jis", "utf-16"):
        try:
            text = raw.decode(encoding)
            lines = text.splitlines()
            keywords = ("クリック", "表示回数", "clicks", "impressions", "セッション", "sessions",
                        "ランディング", "landing page", "クエリ", "query", "ページ", "page",
                        "検索語句", "search term", "通話", "calls", "ルート", "directions",
                        "ウェブサイト", "website", "ビジネス プロフィール", "business profile")
            scores = [sum(word in _clean_name(line) for word in keywords) for line in lines[:30]]
            header_row = scores.index(max(scores)) if scores and max(scores) else 0
            return pd.read_csv(io.StringIO("\n".join(lines[header_row:])), on_bad_lines="skip")
        except Exception as exc:
            last_error = exc
    raise ValueError(f"CSVを読み取れませんでした: {last_error}")


def _find_header_row(preview: pd.DataFrame) -> int:
    keywords = ("クリック", "表示回数", "clicks", "impressions", "セッション", "sessions",
                "ランディング", "landing page", "クエリ", "query", "ページ", "page",
                "検索語句", "search term", "通話", "calls", "ルート", "directions",
                "ウェブサイト", "website", "ビジネス プロフィール", "business profile")
    best_row, best_score = 0, 0
    for index, row in preview.iterrows():
        values = " | ".join(_clean_name(x) for x in row.tolist() if pd.notna(x))
        score = sum(word in values for word in keywords)
        if score > best_score:
            best_row, best_score = int(index), score
    return best_row


def read_uploaded_tables(uploaded_files) -> list[tuple[str, pd.DataFrame]]:
    tables = []
    for uploaded in uploaded_files or []:
        raw = uploaded.getvalue()
        lower_name = uploaded.name.lower()
        if lower_name.endswith(".csv"):
            tables.append((uploaded.name, _read_csv_bytes(raw)))
        else:
            book = pd.ExcelFile(io.BytesIO(raw))
            for sheet in book.sheet_names:
                preview = pd.read_excel(book, sheet_name=sheet, header=None, nrows=25)
                header_row = _find_header_row(preview)
                frame = pd.read_excel(book, sheet_name=sheet, skiprows=header_row)
                if not frame.dropna(how="all").empty:
                    tables.append((f"{uploaded.name} / {sheet}", frame))
    return tables


def _find_column(df: pd.DataFrame, aliases: tuple[str, ...]):
    normalized = {_clean_name(col): col for col in df.columns}
    for alias in aliases:
        target = _clean_name(alias)
        if target in normalized:
            return normalized[target]
    for clean, original in normalized.items():
        if any(_clean_name(alias) in clean for alias in aliases):
            return original
    return None


def _number_series(series: pd.Series, percent=False) -> pd.Series:
    text = series.astype(str).str.replace(",", "", regex=False).str.replace("%", "", regex=False)
    values = pd.to_numeric(text, errors="coerce").fillna(0)
    if percent and values.max() > 1:
        values = values / 100
    return values


def normalize_gsc_files(uploaded_files) -> tuple[pd.DataFrame, list[str]]:
    output, notes = [], []
    aliases = {
        "query": ("query", "queries", "top queries", "クエリ", "検索キーワード", "上位のクエリ"),
        "page": ("page", "pages", "top pages", "ページ", "上位のページ"),
        "clicks": ("clicks", "クリック数", "クリック"),
        "impressions": ("impressions", "表示回数"),
        "ctr": ("ctr", "平均ctr"),
        "position": ("position", "average position", "掲載順位", "平均掲載順位"),
    }
    for source, frame in read_uploaded_tables(uploaded_files):
        frame = frame.dropna(how="all").copy()
        found = {name: _find_column(frame, options) for name, options in aliases.items()}
        if not found["clicks"] and not found["impressions"]:
            continue
        clean = pd.DataFrame(index=frame.index)
        clean["query"] = frame[found["query"]].fillna("").astype(str) if found["query"] else ""
        clean["page"] = frame[found["page"]].fillna("").astype(str) if found["page"] else ""
        for metric in ("clicks", "impressions", "ctr", "position"):
            clean[metric] = _number_series(frame[found[metric]], percent=metric == "ctr") if found[metric] else 0
        clean["source"] = source
        output.append(clean)
    if not output:
        if uploaded_files:
            notes.append("Search Consoleファイル内に、クリック数・表示回数の列を見つけられませんでした。")
        return pd.DataFrame(columns=["query", "page", "clicks", "impressions", "ctr", "position", "source"]), notes
    result = pd.concat(output, ignore_index=True)
    result = result[(result["query"] != "") | (result["page"] != "")]
    return result, notes


def normalize_ga4_files(uploaded_files) -> tuple[pd.DataFrame, list[str]]:
    output, notes = [], []
    aliases = {
        "landing_page": ("landing page + query string", "landing page", "ランディング ページ + クエリ文字列", "ランディングページ"),
        "sessions": ("sessions", "セッション"),
        "active_users": ("active users", "アクティブ ユーザー", "アクティブユーザー数", "ユーザー"),
        "engagement_rate": ("engagement rate", "エンゲージメント率"),
        "key_events": ("key events", "キーイベント", "コンバージョン"),
    }
    for source, frame in read_uploaded_tables(uploaded_files):
        frame = frame.dropna(how="all").copy()
        found = {name: _find_column(frame, options) for name, options in aliases.items()}
        if not found["landing_page"] or not found["sessions"]:
            continue
        clean = pd.DataFrame(index=frame.index)
        clean["landing_page"] = frame[found["landing_page"]].fillna("").astype(str)
        for metric in ("sessions", "active_users", "engagement_rate", "key_events"):
            clean[metric] = _number_series(frame[found[metric]], percent=metric == "engagement_rate") if found[metric] else 0
        clean["source"] = source
        output.append(clean)
    if not output:
        if uploaded_files:
            notes.append("GA4ファイル内に、ランディングページ・セッションの列を見つけられませんでした。")
        return pd.DataFrame(columns=["landing_page", "sessions", "active_users", "engagement_rate", "key_events", "source"]), notes
    result = pd.concat(output, ignore_index=True)
    result = result[result["landing_page"] != ""]
    return result, notes


def normalize_gbp_files(uploaded_files) -> tuple[list[dict], list[str]]:
    """GBPの書き出し形式が複数あっても、表構造を保ったままAIへ渡す。"""
    datasets, notes = [], []
    try:
        tables = read_uploaded_tables(uploaded_files)
    except Exception as exc:
        return [], [f"GBPファイルを読み取れませんでした: {exc}"]
    for source, frame in tables:
        frame = frame.dropna(how="all").dropna(axis=1, how="all").copy()
        frame = frame.loc[:, [not _clean_name(col).startswith("unnamed") for col in frame.columns]]
        if frame.empty:
            continue
        frame.columns = [str(col).strip() for col in frame.columns]
        rows = json.loads(frame.head(300).to_json(orient="records", force_ascii=False, date_format="iso"))
        datasets.append({"source": source, "columns": list(frame.columns), "rows": rows})
    if uploaded_files and not datasets:
        notes.append("GBPファイル内に分析できる表データを見つけられませんでした。")
    return datasets, notes


def get_html(url: str, timeout=15):
    response = requests.get(url, headers={"User-Agent": UA}, timeout=timeout, allow_redirects=True)
    response.raise_for_status()
    if "text/html" not in response.headers.get("content-type", ""):
        raise ValueError("HTMLページではありません。")
    return response.url, response.text


def discover_urls(base_url: str, limit=15) -> list[str]:
    host = urlparse(base_url).netloc
    candidates = [urljoin(base_url, "/sitemap.xml"), urljoin(base_url, "/sitemap_index.xml")]
    urls = [base_url]
    visited_maps = set()

    def parse_map(map_url: str, depth=0):
        if depth > 1 or map_url in visited_maps or len(urls) >= limit:
            return
        visited_maps.add(map_url)
        try:
            r = requests.get(map_url, headers={"User-Agent": UA}, timeout=12)
            if not r.ok:
                return
            soup = BeautifulSoup(r.content, "xml")
            locs = [x.get_text(strip=True) for x in soup.find_all("loc")]
            for loc in locs:
                if loc.endswith(".xml"):
                    parse_map(loc, depth + 1)
                elif urlparse(loc).netloc == host and loc not in urls:
                    urls.append(loc)
                    if len(urls) >= limit:
                        return
        except Exception:
            return

    for candidate in candidates:
        parse_map(candidate)
        if len(urls) > 1:
            break
    return urls[:limit]


def audit_page(url: str) -> dict:
    started = time.perf_counter()
    final_url, html = get_html(url)
    elapsed = time.perf_counter() - started
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    meta = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    description = meta.get("content", "").strip() if meta else ""
    canonical = soup.find("link", attrs={"rel": lambda x: x and "canonical" in x})
    h1 = [x.get_text(" ", strip=True) for x in soup.find_all("h1")]
    headings = [{x.name: x.get_text(" ", strip=True)} for x in soup.find_all(["h2", "h3"])][:40]
    text = " ".join(soup.get_text(" ", strip=True).split())
    images = soup.find_all("img")
    return {
        "url": final_url, "status": 200, "response_seconds": round(elapsed, 2),
        "title": title, "title_length": len(title), "meta_description": description,
        "description_length": len(description), "h1": h1, "h1_count": len(h1),
        "headings": headings, "text_length": len(text), "image_count": len(images),
        "images_without_alt": sum(1 for img in images if not img.get("alt", "").strip()),
        "canonical": canonical.get("href", "") if canonical else "",
    }


def crawl_site(url: str) -> list[dict]:
    results = []
    for page_url in discover_urls(url):
        try:
            results.append(audit_page(page_url))
        except Exception as exc:
            results.append({"url": page_url, "error": str(exc)[:160]})
    return results


def compact_records(df: pd.DataFrame, sort_by: str, limit=80):
    if df.empty:
        return []
    return df.sort_values(sort_by, ascending=False).head(limit).round(4).to_dict("records")


ACTIONABLE_REPORT_RULES = """
あなたはマーケティングと実行支援のプロです。読者はIT初心者の店舗運営者です。
目的は順位やアクセスだけでなく、適切な新規客の問い合わせ・予約・来店につなげることです。
【具体化の必須ルール】
- 「SEOを強化」「導線を改善」「情報を充実」だけでは不可。最初に行う3つを順序付きで示す。
- 用語は平易な日本語で説明する。CTRは検索結果が表示されたうちクリックされた割合、など。
- 各施策は対象ページURLまたはGBPの編集項目、理由、操作手順3〜7段階、文案例、完了確認、担当、作業時間の目安を必須とする。
- 管理画面やCMSは未確認なので、WordPressと断定しない。画面名は一般的な案内・表示が異なる可能性を示す。設定できない時は制作会社への依頼文を用意する。
- サイトは検索語句→対象ページ→予約・相談までの流れで優先度を判断する。タイトル/説明文、サービス説明、内部リンク、予約導線を根拠に応じて選ぶ。未取得の本文・ボタン・料金等を「存在しない」と断定しない。
- GBPは検索語句と表示・電話・ルート・サイトクリック等の実測を分けて判断する。基本情報・カテゴリ・サービス・写真・投稿・予約リンクは未確認なら確認作業として提案し、現在の欠落を断定しない。投稿回数を増やすだけで順位が上がると約束しない。
- 問い合わせ率を閲覧数やクリック数だけから推測しない。電話クリックやルート検索を来店人数として扱わない。順位・集客成果を保証しない。
- 実測事実、仮説、推奨施策を明確に区別する。取得していない検索順位・競合・期間・店舗情報・効果・予約URLを創作しない。
- 同じ期間・同じ集計粒度のデータだけ比較し、クエリ別とページ別の件数を足して総計にしない。データ不足なら確認手順と取得すべきデータを示す。
- 指標は現在値（未取得なら未取得）、改善目安（仮の目標と明記）、確認画面、確認時期、改善しない場合の次の手を示す。
- 30日計画は実施・動作確認・初期計測の計画とする。効果判断は短期間で断定せず、変動や季節性も確認する。
- 無料で自分で行える施策を優先する。有料作業や権限が必要な作業は明示する。編集前のバックアップと公開後のスマホ確認を含める。
- 口コミ依頼は実際の利用者に公平に行う。高評価限定の依頼、特典と引換え、架空口コミ、キーワードを強制した口コミ文案を禁止する。
- 医療・健康分野は治癒保証・断定・誇大な効果表現を避ける。文案の料金や営業時間等は［要確認］の置換箇所として明示する。
【出力の追加必須項目】
指定JSONへ次の3項目を必ず追加する（既存項目も省略しない）。
"first_three_actions": ["最初に行うこと1：対象と具体的な作業", "2：具体的な作業", "3：具体的な作業"],
"implementation_guide": [
  {"title":"施策名", "priority":"高/中/低", "target":"実在する対象URLまたは編集項目",
   "evidence":"実測根拠または仮説・要確認の明記", "goal":"集客につながる理由",
   "owner":"自分／制作会社など", "time_estimate":"作業時間の目安", "requirements":"権限・費用・事前確認",
   "steps":["開く画面と操作", "変更する内容", "保存とスマホ動作確認"],
   "copy_example":"参考にできるタイトル・説明文・予約案内・投稿等の文案。不要なら不要と理由",
   "completion_check":"何を見て完了と判断するか", "help_request":"できない場合に担当者へ送る依頼文"}
],
"measurement_plan": [
  {"metric":"指標と初心者向けの意味", "baseline":"現在値または未取得",
   "provisional_target":"保証ではない仮目標／まず基準値を測る",
   "where_to_check":"確認する画面と操作", "when":"実装直後／毎週／効果検証時期",
   "next_if_no_change":"改善しない時の具体的な次の手"}
]
implementation_guideは重要な施策4〜6件。量より実行可能性を優先し、最低1件は予約・問い合わせ導線の確認と改善を含める。
GBP専用分析ではサイト記事リライトを含めず、GBPの予約・サイトへのリンク等の導線確認を扱う。
"""



def build_prompt(url: str, crawl: list[dict], gsc: pd.DataFrame, ga4: pd.DataFrame,
                 gbp_profile_name: str = "", gbp_data: list[dict] | None = None,
                 analysis_kind: str = "site") -> str:
    gsc_low_ctr = []
    if not gsc.empty:
        candidates = gsc[(gsc.impressions >= 20) & (gsc.position <= 20)].copy()
        gsc_low_ctr = compact_records(candidates, "impressions", 80)
    data = {
        "target_url": url,
        "analysis_kind": analysis_kind,
        "period": "ユーザーがGoogle画面からダウンロードしたファイルの集計期間",
        "public_page_audit": crawl,
        "gsc_low_ctr_opportunities": gsc_low_ctr,
        "gsc_top": compact_records(gsc, "clicks", 80),
        "ga4_top_landing_pages": compact_records(ga4, "sessions", 80),
        "gbp_profile_name": gbp_profile_name,
        "gbp_performance_exports": gbp_data or [],
    }
    if analysis_kind == "gbp":
        return f"""
あなたは地域密着型店舗のGoogleビジネスプロフィール（GBP）運用を支援する、日本語MEOコンサルタントです。
次のGBP実測データだけを根拠に、選択された店舗の表示機会と来店・問い合わせ行動を改善してください。
数値にない事実を創作せず、医療・健康領域では断定、誇大表現、治癒保証を避けてください。
今回はGBP専用分析です。サイトのSEOリライトやブログ記事は作成しないでください。
{ACTIONABLE_REPORT_RULES}

分析データ:
{json.dumps(data, ensure_ascii=False)}

以下のJSONオブジェクトだけを返してください。Markdownコードフェンスは禁止です。
{{
  "executive_summary": "最重要結論（300字以内）",
  "data_findings": [{{"finding":"事実", "evidence":"数値・検索語句・期間", "impact":"影響"}}],
  "gbp_analysis": {{"summary":"GBPの重要な結論", "strengths":["強みと根拠"], "issues":["課題と根拠"], "actions":["優先順位付き改善策"], "post_ideas":["GBP投稿案"]}},
  "next_30_days": [{{"week":"1週目", "task":"実施内容", "success":"完了条件"}}],
  "measurement_notes": ["データ欠落や判断上の注意"]
}}
"""
    return f"""
あなたは月間100万PVサイトを担当する、日本語SEOコンサルタント兼Webマーケターです。
次の実測データだけを根拠に、地域密着型の鍼灸整骨院・アロマサロン・おすすめ情報サイトのいずれかを改善してください。
医療・健康領域では断定、誇大表現、治癒保証を避け、一次情報の確認が必要な点を明記してください。
{ACTIONABLE_REPORT_RULES}

分析データ:
{json.dumps(data, ensure_ascii=False)}

以下のJSONオブジェクトだけを返してください。Markdownコードフェンスは禁止です。
{{
  "executive_summary": "最重要結論（300字以内）",
  "data_findings": [{{"finding":"事実", "evidence":"数値・URL", "impact":"影響"}}],
  "priorities": [{{"priority":"高/中/低", "issue":"課題", "action":"具体策", "kpi":"指標", "target":"目安", "effort":"小/中/大"}}],
  "rewrite_target": {{"url":"最優先URL", "reason":"選定理由", "direction":"リライト方針", "primary_keyword":"主軸KW", "secondary_keywords":["関連KW"]}},
  "gbp_analysis": {{"summary":"GBPの重要な結論", "strengths":["強み"], "issues":["課題と根拠"], "actions":["優先順位付き改善策"], "post_ideas":["GBP投稿案"]}},
  "reader_problems": ["想定読者が抱える具体的な悩み"],
  "article_outline": [{{"heading":"H2見出し", "purpose":"狙い", "subheadings":["H3"]}}],
  "completed_article": "読者の検索意図を満たす完成本文。Markdown形式。タイトル、導入、H2/H3、まとめ、自然な予約・相談導線を含める。データにない店舗情報・料金・効果は創作しない。2,500〜4,000字程度",
  "next_30_days": [{{"week":"1週目", "task":"実施内容", "success":"完了条件"}}],
  "measurement_notes": ["データ欠落や判断上の注意"]
}}
"""


def parse_json_response(text: str) -> dict:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("AIの回答をJSONとして読み取れませんでした。")
    return json.loads(cleaned[start:end + 1])


def _can_try_another_model(exc: Exception) -> bool:
    message = str(exc).lower()
    retryable_words = (
        "503", "unavailable", "high demand", "overloaded", "capacity",
        "500", "502", "504", "timeout", "deadline_exceeded",
        "404", "not_found", "no longer available",
    )
    return any(word in message for word in retryable_words)


def run_ai(api_key: str, model: str, prompt: str) -> tuple[dict, str]:
    client = genai.Client(api_key=api_key)
    candidate_models = []
    for candidate in (model, *FALLBACK_MODELS):
        if candidate and candidate not in candidate_models:
            candidate_models.append(candidate)

    errors = []
    for index, candidate in enumerate(candidate_models):
        try:
            response = client.models.generate_content(
                model=candidate,
                contents=prompt,
                config={"temperature": 0.25, "response_mime_type": "application/json"},
            )
            return parse_json_response(response.text), candidate
        except Exception as exc:
            errors.append(exc)
            if not _can_try_another_model(exc):
                raise
            if index < len(candidate_models) - 1:
                time.sleep(2)

    raise RuntimeError(
        "Geminiが一時的に混み合っています。自動再試行でも接続できませんでした。"
        "5〜10分ほど待ってから、もう一度「分析する」を押してください。"
    ) from errors[-1]


def actionable_report_markdown(report: dict) -> str:
    """画面とダウンロードで共通の、初心者向け実行指示書。"""
    lines = []
    first_actions = report.get("first_three_actions", [])
    if first_actions:
        lines.append("## まず取り組む3つの作業")
        lines.extend(f"- {action}" for action in first_actions)
    guide = report.get("implementation_guide", [])
    if guide:
        lines.append("\n## 初心者向け・具体的な実行手順")
    for index, item in enumerate(guide, 1):
        lines.append(f"\n### 作業{index}：{item.get('title', '')}")
        for label, key in (("優先度", "priority"), ("変更・確認する場所", "target"),
                           ("根拠・確認が必要な点", "evidence"), ("集客につながる理由", "goal"),
                           ("担当", "owner"), ("作業時間の目安", "time_estimate"),
                           ("権限・費用・事前確認", "requirements")):
            lines.append(f"- {label}：{item.get(key, '')}")
        lines.append("\n操作手順：")
        for step_index, step in enumerate(item.get("steps", []), 1):
            lines.append(f"- 手順{step_index}：{step}")
        for label, key in (("変更する文案・参考例", "copy_example"),
                           ("完了の確認方法", "completion_check"),
                           ("自分でできない場合の依頼文", "help_request")):
            lines.extend([f"\n{label}：", str(item.get(key, ""))])
    measurement = report.get("measurement_plan", [])
    if measurement:
        lines.append("\n## 集客につながったかを確認する方法")
    for item in measurement:
        lines.append(f"\n### {item.get('metric', '')}")
        for label, key in (("現在値", "baseline"), ("仮の目標", "provisional_target"),
                           ("確認する場所と操作", "where_to_check"), ("確認時期", "when"),
                           ("改善しない場合の次の手", "next_if_no_change")):
            lines.append(f"- {label}：{item.get(key, '')}")
    return "\n".join(lines)



def markdown_report(target_label: str, url: str, report: dict, analysis_kind: str) -> str:
    report_title = "GBP改善レポート" if analysis_kind == "gbp" else "SEO・マーケティング改善レポート"
    lines = [f"# {report_title}\n\n対象: {target_label}\n\n関連サイト: {url}\n", "## 最重要結論", report.get("executive_summary", "")]
    lines.append(actionable_report_markdown(report))
    if analysis_kind == "gbp":
        gbp_report = report.get("gbp_analysis", {})
        lines += ["\n## GBP分析", gbp_report.get("summary", "")]
        for title, key in (("強み", "strengths"), ("課題", "issues"),
                           ("優先して行う改善策", "actions"), ("GBP投稿案", "post_ideas")):
            lines.append(f"\n### {title}")
            lines += [f"- {item}" for item in gbp_report.get(key, [])]
    else:
        lines.append("\n## 改善優先度")
        for item in report.get("priorities", []):
            lines.append(f"- **{item.get('priority', '')}** {item.get('issue', '')}：{item.get('action', '')} "
                         f"（指標：{item.get('kpi', '')}／目安：{item.get('target', '')}／工数：{item.get('effort', '')}）")
        rewrite = report.get("rewrite_target", {})
        if rewrite:
            lines += ["\n## 最優先リライト", f"URL：{rewrite.get('url', '')}",
                      f"理由：{rewrite.get('reason', '')}", f"方向性：{rewrite.get('direction', '')}",
                      f"主軸キーワード：{rewrite.get('primary_keyword', '')}",
                      f"関連キーワード：{', '.join(rewrite.get('secondary_keywords', []))}"]
        lines += ["\n## 想定読者の悩み"] + [f"- {x}" for x in report.get("reader_problems", [])]
        lines += ["\n## 記事の構成案"]
        for item in report.get("article_outline", []):
            lines.append(f"### {item.get('heading', '')}")
            lines.append(item.get("purpose", ""))
            lines += [f"- {s}" for s in item.get("subheadings", [])]
        lines += ["\n## 完成した本文", report.get("completed_article", "")]

    lines.append("\n## 30日間の実行計画")
    for item in report.get("next_30_days", []):
        lines.append(f"- **{item.get('week', '')}**：{item.get('task', '')}（完了条件：{item.get('success', '')}）")
    lines.append("\n## 分析根拠")
    for item in report.get("data_findings", []):
        lines.append(f"- {item.get('finding', '')}（根拠：{item.get('evidence', '')}／影響：{item.get('impact', '')}）")
    lines.append("\n## 分析上の注意")
    lines += [f"- {note}" for note in report.get("measurement_notes", [])]
    return "\n".join(lines)


def report_pdf(markdown: str) -> bytes:
    """Markdownレポート全体を日本語のA4 PDFへ変換する。"""
    font = "PonteNotoSansJP"
    if font not in pdfmetrics.getRegisteredFontNames():
        app_dir = Path(__file__).parent
        font_candidates = (
            app_dir / "NotoSansJP-Regular.ttf",
            app_dir / "assets" / "NotoSansJP-Regular.ttf",
        )
        font_path = next((path for path in font_candidates if path.exists()), None)
        if font_path:
            pdfmetrics.registerFont(TTFont(font, str(font_path)))
        else:
            # フォントをアップロードし忘れた場合も、PDF作成自体は止めない。
            font = "HeiseiKakuGo-W5"
            if font not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(UnicodeCIDFont(font))
    styles = getSampleStyleSheet()
    base = dict(fontName=font, wordWrap="CJK", textColor=colors.HexColor("#293241"))
    heading = {
        1: ParagraphStyle("ReportTitle", parent=styles["Normal"], fontSize=19, leading=27, spaceAfter=15, **base),
        2: ParagraphStyle("ReportSection", parent=styles["Normal"], fontSize=13, leading=20, spaceBefore=14, spaceAfter=7, **base),
        3: ParagraphStyle("ReportSubsection", parent=styles["Normal"], fontSize=11, leading=17, spaceBefore=9, spaceAfter=5, **base),
    }
    body = ParagraphStyle("ReportBody", parent=styles["Normal"], fontSize=9.5, leading=16, spaceAfter=5, **base)
    bullet = ParagraphStyle("ReportBullet", parent=body, leftIndent=12, firstLineIndent=-10)
    story = []

    for line in markdown.splitlines():
        value = line.strip()
        if not value:
            story.append(Spacer(1, 4))
            continue
        level = len(value) - len(value.lstrip("#"))
        if 1 <= level <= 3 and value[level:level + 1] == " ":
            story.append(Paragraph(escape(value[level + 1:]), heading[level]))
            continue
        is_bullet = value.startswith("- ")
        if is_bullet:
            value = value[2:]
        # PDFの全本文に同じ内容を入れ、Markdown装飾だけを表示用に除く。
        value = re.sub(r"\*\*(.*?)\*\*", r"\1", value)
        value = re.sub(r"^#{1,3}\s+", "", value)
        story.append(Paragraph(("・" if is_bullet else "") + escape(value), bullet if is_bullet else body))

    output = io.BytesIO()
    def add_footer(canvas, document):
        canvas.saveState()
        canvas.setFont(font, 8)
        canvas.setFillColor(colors.HexColor("#64748b"))
        canvas.drawCentredString(A4[0] / 2, 28, str(document.page))
        canvas.restoreState()

    document = SimpleDocTemplate(output, pagesize=A4, leftMargin=43, rightMargin=43,
                                 topMargin=42, bottomMargin=45, title="PONTE 分析レポート")
    document.build(story, onFirstPage=add_footer, onLaterPages=add_footer)
    return output.getvalue()


def report_filename(target: dict, extension: str) -> str:
    date = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d")
    kind = "GBP" if target["kind"] == "gbp" else "サイト"
    safe_name = re.sub(r"[^\w\u3040-\u30ff\u3400-\u9fff-]", "_", target["name"])
    return f"{safe_name}_{kind}_{date}.{extension}"


def render_saved_result(saved: dict) -> None:
    """セッションに保存した分析結果を、再実行後も同じ状態で表示する。"""
    target = saved["target"]
    report = saved["report"]
    analysis_kind = saved["analysis_kind"]
    crawl = saved["crawl"]
    gsc_rows = saved["gsc_rows"]
    ga4_rows = saved["ga4_rows"]
    gbp_rows = saved["gbp_rows"]
    gbp_profile_name = saved["gbp_profile_name"]

    st.success("分析が完了しました")
    st.caption(f"表示中の分析結果：{saved['selected_target']}")
    if saved["warnings"]:
        with st.expander("データ連携のお知らせ"):
            for warning in saved["warnings"]:
                st.warning(warning)

    if analysis_kind == "gbp":
        c1, c2 = st.columns(2)
        c1.metric("分析対象", target["name"])
        c2.metric("GBPデータ", f"{gbp_rows:,}行")
    else:
        c1, c2, c3 = st.columns(3)
        c1.metric("確認ページ", f"{sum('error' not in x for x in crawl)}件")
        c2.metric("GSCデータ", f"{gsc_rows:,}行")
        c3.metric("GA4データ", f"{ga4_rows:,}行")

    st.subheader("最重要結論")
    st.info(report.get("executive_summary", ""))

    instructions = actionable_report_markdown(report)
    if instructions:
        st.markdown(instructions)
        st.caption("改善目標は保証ではありません。公開前に店舗情報を確認し、公開後は予約までの操作をスマホで試してください。")

    if analysis_kind == "site":
        with st.expander("SEO・マーケティング改善点", expanded=True):
            priorities = pd.DataFrame(report.get("priorities", []))
            if not priorities.empty:
                st.dataframe(priorities, use_container_width=True, hide_index=True)
            rewrite_target = report.get("rewrite_target", {})
            if rewrite_target:
                st.markdown(
                    f"**最優先リライト:** {rewrite_target.get('url','')}  \n"
                    f"**理由:** {rewrite_target.get('reason','')}  \n"
                    f"**方向性:** {rewrite_target.get('direction','')}"
                )

    gbp_report = report.get("gbp_analysis", {})
    if gbp_profile_name and gbp_report:
        with st.expander(f"{gbp_profile_name}｜GBP分析", expanded=True):
            st.info(gbp_report.get("summary", ""))
            for title, key in (("強み", "strengths"), ("課題", "issues"),
                               ("優先して行う改善策", "actions"), ("GBP投稿案", "post_ideas")):
                st.markdown(f"**{title}**")
                for item in gbp_report.get(key, []):
                    st.markdown(f"- {item}")

    if analysis_kind == "site":
        st.header("想定読者の悩み")
        for item in report.get("reader_problems", []):
            st.markdown(f"- {item}")

        st.header("記事の構成案")
        for section in report.get("article_outline", []):
            st.subheader(section.get("heading", ""))
            st.caption(section.get("purpose", ""))
            for sub in section.get("subheadings", []):
                st.markdown(f"- {sub}")

        st.header("完成した本文")
        st.markdown(report.get("completed_article", ""))

    with st.expander("30日間の実行計画・分析根拠"):
        plan = pd.DataFrame(report.get("next_30_days", []))
        if not plan.empty:
            st.dataframe(plan, use_container_width=True, hide_index=True)
        findings = pd.DataFrame(report.get("data_findings", []))
        if not findings.empty:
            st.dataframe(findings, use_container_width=True, hide_index=True)
        for note in report.get("measurement_notes", []):
            st.caption(f"・{note}")

    st.subheader("レポートをダウンロード")
    st.caption("一方をダウンロードした後も結果は保持され、続けてもう一方をダウンロードできます。")
    pdf_col, text_col = st.columns(2)
    with pdf_col:
        st.download_button(
            "PDFでダウンロード",
            saved["pdf_data"],
            saved["pdf_filename"],
            "application/pdf",
            use_container_width=True,
            on_click="ignore",
            key="download_pdf",
        )
    with text_col:
        st.download_button(
            "Markdownでダウンロード",
            saved["markdown_data"],
            saved["markdown_filename"],
            "text/markdown",
            use_container_width=True,
            on_click="ignore",
            key="download_markdown",
        )


with st.sidebar:
    st.header("初回設定")
    api_key = st.text_input("Gemini APIキー", value=secret("GEMINI_API_KEY", ""), type="password")
    configured_model = str(secret("GEMINI_MODEL", DEFAULT_MODEL)).strip()
    if configured_model in RETIRED_MODELS:
        configured_model = DEFAULT_MODEL
    model = st.text_input("Geminiモデル", value=configured_model)
    st.caption("混雑時は別の安定モデルへ自動的に切り替えます。")
    st.divider()
    st.subheader("分析データ")
    gsc_files = st.file_uploader(
        "Search Consoleデータ",
        type=["csv", "xlsx", "xls"],
        accept_multiple_files=True,
        help="検索パフォーマンスからダウンロードしたCSVまたはExcelを選択します。複数ファイルも選べます。",
    )
    ga4_files = st.file_uploader(
        "Googleアナリティクス（GA4）データ",
        type=["csv", "xlsx", "xls"],
        accept_multiple_files=True,
        help="ランディングページのレポートをCSVまたはExcelで選択します。",
    )
    gbp_files = st.file_uploader(
        "Googleビジネスプロフィール（GBP）データ",
        type=["csv", "xlsx", "xls"],
        accept_multiple_files=True,
        help="選択する店舗のGBPパフォーマンスや検索語句をダウンロードしたCSV／Excelを選択します。",
    )
    st.caption("Googleの管理者権限やサービスアカウントは不要です。")
    with st.expander("ダウンロードするデータ"):
        st.markdown(
            "**Search Console**：検索結果のパフォーマンスで期間を指定し、右上の「エクスポート」からExcelがおすすめです。  \n"
            "**GA4**：レポート → エンゲージメント → ランディングページで、右上の共有アイコンからCSVをダウンロードします。"
            "  \n**GBP**：Googleビジネスプロフィールのパフォーマンス画面から、検索語句や操作数のCSV／Excelをダウンロードします。"
        )

st.title("PONTE SEO改善アプリ")
st.markdown('<p class="subtle">3サイトのSEO分析と、2店舗のGBP分析から選べます。</p>', unsafe_allow_html=True)

selected_target = st.selectbox(
    "分析する対象",
    options=list(ANALYSIS_TARGETS),
)
target = ANALYSIS_TARGETS[selected_target]
analysis_kind = target["kind"]
url_input = target["url"]
if analysis_kind == "gbp":
    st.caption("GBPのCSV／Excelをサイドバーにアップロードしてから分析してください。")
else:
    st.caption(f"対象URL：{urlparse(url_input).netloc}")
analyze = st.button("分析する", type="primary", use_container_width=True)

if analyze:
    try:
        url = normalize_url(url_input)
        if not api_key:
            st.error("サイドバーの「Gemini APIキー」を設定してください。")
            st.stop()

        first_message = "GBPデータを確認しています…" if analysis_kind == "gbp" else "公開ページを確認しています…"
        progress = st.progress(0, text=first_message)
        crawl = crawl_site(url) if analysis_kind == "site" else []
        progress.progress(30, text="アップロードデータを読み込んでいます…")

        warnings = []
        gsc_df, ga4_df, gbp_data = pd.DataFrame(), pd.DataFrame(), []
        gbp_profile_name = target.get("gbp_profile_name", "")
        try:
            if analysis_kind == "site":
                gsc_df, gsc_notes = normalize_gsc_files(gsc_files)
                ga4_df, ga4_notes = normalize_ga4_files(ga4_files)
                warnings.extend(gsc_notes + ga4_notes)
                if gsc_df.empty:
                    warnings.append("Search Consoleデータがないため、その部分は公開ページ情報だけで分析しました。")
                if ga4_df.empty:
                    warnings.append("GA4データがないため、その部分は公開ページ情報だけで分析しました。")
            else:
                gbp_data, gbp_notes = normalize_gbp_files(gbp_files)
                warnings.extend(gbp_notes)
        except Exception as exc:
            warnings.append(f"アップロードデータを読み取れませんでした: {exc}")

        if analysis_kind == "gbp" and not gbp_data:
            progress.empty()
            st.error(f"{gbp_profile_name}のGBPデータをアップロードしてください。")
            st.stop()

        progress.progress(60, text="SEO課題と改善優先度を分析しています…")
        if analysis_kind == "gbp":
            progress.progress(60, text="GBPの強み・課題・改善優先度を分析しています…")
        prompt = build_prompt(url, crawl, gsc_df, ga4_df, gbp_profile_name, gbp_data, analysis_kind)
        report, used_model = run_ai(api_key, model, prompt)
        if used_model != model:
            warnings.append(f"{model}が混雑していたため、{used_model}へ自動切替して分析しました。")
        progress.progress(100, text="分析が完了しました。")
        time.sleep(.2)
        progress.empty()

        output = markdown_report(selected_target, url, report, analysis_kind)
        pdf_data = report_pdf(output)
        st.session_state["analysis_result"] = {
            "selected_target": selected_target,
            "target": dict(target),
            "analysis_kind": analysis_kind,
            "report": report,
            "warnings": list(warnings),
            "crawl": crawl,
            "gsc_rows": len(gsc_df),
            "ga4_rows": len(ga4_df),
            "gbp_rows": sum(len(x.get("rows", [])) for x in gbp_data),
            "gbp_profile_name": gbp_profile_name,
            "pdf_data": pdf_data,
            "pdf_filename": report_filename(target, "pdf"),
            "markdown_data": output.encode("utf-8-sig"),
            "markdown_filename": report_filename(target, "md"),
        }
    except Exception as exc:
        error_text = str(exc)
        if _can_try_another_model(exc):
            st.error("Geminiが一時的に混み合っています。5〜10分後に、もう一度「分析する」を押してください。")
            st.caption("URLやAPIキーの設定ミスではありません。入力やアップロードをやり直す必要もありません。")
        else:
            st.error(f"分析を完了できませんでした: {error_text}")
            st.caption("Gemini APIキーと、アップロードしたファイル形式をご確認ください。")

saved_result = st.session_state.get("analysis_result")
if saved_result:
    render_saved_result(saved_result)

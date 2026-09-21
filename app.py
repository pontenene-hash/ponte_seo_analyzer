from __future__ import annotations

import io
import json
import re
import time
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from google import genai


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


def build_prompt(url: str, crawl: list[dict], gsc: pd.DataFrame, ga4: pd.DataFrame,
                 gbp_profile_name: str = "", gbp_data: list[dict] | None = None) -> str:
    gsc_low_ctr = []
    if not gsc.empty:
        candidates = gsc[(gsc.impressions >= 20) & (gsc.position <= 20)].copy()
        gsc_low_ctr = compact_records(candidates, "impressions", 80)
    data = {
        "target_url": url,
        "period": "ユーザーがGoogle画面からダウンロードしたファイルの集計期間",
        "public_page_audit": crawl,
        "gsc_low_ctr_opportunities": gsc_low_ctr,
        "gsc_top": compact_records(gsc, "clicks", 80),
        "ga4_top_landing_pages": compact_records(ga4, "sessions", 80),
        "gbp_profile_name": gbp_profile_name,
        "gbp_performance_exports": gbp_data or [],
    }
    return f"""
あなたは月間100万PVサイトを担当する、日本語SEOコンサルタント兼Webマーケターです。
次の実測データだけを根拠に、地域密着型の鍼灸整骨院・アロマサロン・おすすめ情報サイトのいずれかを改善してください。
医療・健康領域では断定、誇大表現、治癒保証を避け、一次情報の確認が必要な点を明記してください。

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


def run_ai(api_key: str, model: str, prompt: str) -> dict:
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config={"temperature": 0.25, "response_mime_type": "application/json"},
    )
    return parse_json_response(response.text)


def markdown_report(url: str, report: dict) -> str:
    lines = [f"# SEO・マーケティング改善レポート\n\n対象: {url}\n", "## 最重要結論", report.get("executive_summary", "")]
    lines += ["\n## 想定読者の悩み"] + [f"- {x}" for x in report.get("reader_problems", [])]
    lines += ["\n## 記事の構成案"]
    for x in report.get("article_outline", []):
        lines.append(f"### {x.get('heading','')}")
        lines.append(x.get("purpose", ""))
        lines += [f"- {s}" for s in x.get("subheadings", [])]
    lines += ["\n## 完成した本文", report.get("completed_article", "")]
    return "\n".join(lines)


with st.sidebar:
    st.header("初回設定")
    api_key = st.text_input("Gemini APIキー", value=secret("GEMINI_API_KEY", ""), type="password")
    configured_model = str(secret("GEMINI_MODEL", DEFAULT_MODEL)).strip()
    if configured_model in RETIRED_MODELS:
        configured_model = DEFAULT_MODEL
    model = st.text_input("Geminiモデル", value=configured_model)
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
st.markdown('<p class="subtle">サイトの実測データから、改善案とリライト原稿をまとめて作成します。</p>', unsafe_allow_html=True)

selected_site = st.selectbox(
    "分析するサイト",
    options=list(SITE_OPTIONS),
    format_func=lambda name: f"{name}（{urlparse(SITE_OPTIONS[name]).netloc}）",
)
url_input = SITE_OPTIONS[selected_site]
analyze = st.button("分析する", type="primary", use_container_width=True)

if analyze:
    try:
        url = normalize_url(url_input)
        if not api_key:
            st.error("サイドバーの「Gemini APIキー」を設定してください。")
            st.stop()

        progress = st.progress(0, text="公開ページを確認しています…")
        crawl = crawl_site(url)
        progress.progress(30, text="アップロードデータを読み込んでいます…")

        warnings = []
        try:
            gsc_df, gsc_notes = normalize_gsc_files(gsc_files)
            ga4_df, ga4_notes = normalize_ga4_files(ga4_files)
            gbp_data, gbp_notes = normalize_gbp_files(gbp_files)
            warnings.extend(gsc_notes + ga4_notes + gbp_notes)
        except Exception as exc:
            gsc_df, ga4_df = pd.DataFrame(), pd.DataFrame()
            gbp_data = []
            warnings.append(f"アップロードデータを読み取れませんでした: {exc}")
        if gsc_df.empty:
            warnings.append("Search Consoleデータがないため、その部分は公開ページ情報だけで分析しました。")
        if ga4_df.empty:
            warnings.append("GA4データがないため、その部分は公開ページ情報だけで分析しました。")
        gbp_profile_name = GBP_PROFILE_NAMES.get(selected_site, "")
        if gbp_profile_name and not gbp_data:
            warnings.append(f"{gbp_profile_name}のGBPデータがないため、GBP実績値の分析は省略しました。")
        if not gbp_profile_name and gbp_data:
            warnings.append("おすすめブログにはGBPがないため、アップロードされたGBPデータは分析対象外です。")
            gbp_data = []

        progress.progress(60, text="SEO課題と改善優先度を分析しています…")
        prompt = build_prompt(url, crawl, gsc_df, ga4_df, gbp_profile_name, gbp_data)
        report = run_ai(api_key, model, prompt)
        progress.progress(100, text="分析が完了しました。")
        time.sleep(.2)
        progress.empty()

        st.success("分析が完了しました")
        if warnings:
            with st.expander("データ連携のお知らせ"):
                for warning in warnings:
                    st.warning(warning)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("確認ページ", f"{sum('error' not in x for x in crawl)}件")
        c2.metric("GSCデータ", f"{len(gsc_df):,}行")
        c3.metric("GA4データ", f"{len(ga4_df):,}行")
        c4.metric("GBPデータ", f"{sum(len(x.get('rows', [])) for x in gbp_data):,}行")

        st.subheader("最重要結論")
        st.info(report.get("executive_summary", ""))

        with st.expander("SEO・マーケティング改善点", expanded=True):
            priorities = pd.DataFrame(report.get("priorities", []))
            if not priorities.empty:
                st.dataframe(priorities, use_container_width=True, hide_index=True)
            target = report.get("rewrite_target", {})
            if target:
                st.markdown(f"**最優先リライト:** {target.get('url','')}  \n**理由:** {target.get('reason','')}  \n**方向性:** {target.get('direction','')}")

        gbp_report = report.get("gbp_analysis", {})
        if gbp_profile_name and gbp_report:
            with st.expander(f"{gbp_profile_name}｜GBP分析", expanded=True):
                st.info(gbp_report.get("summary", ""))
                for title, key in (("強み", "strengths"), ("課題", "issues"),
                                   ("優先して行う改善策", "actions"), ("GBP投稿案", "post_ideas")):
                    st.markdown(f"**{title}**")
                    for item in gbp_report.get(key, []):
                        st.markdown(f"- {item}")

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

        output = markdown_report(url, report)
        st.download_button("レポートをダウンロード", output.encode("utf-8-sig"), "seo_improvement_report.md", "text/markdown", use_container_width=True)
    except Exception as exc:
        st.error(f"分析を完了できませんでした: {exc}")
        st.caption("URL、Gemini APIキー、アップロードしたファイル形式をご確認ください。")

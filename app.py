from __future__ import annotations

import json
import re
import time
from datetime import date, timedelta
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from google import genai
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import DateRange, Dimension, Metric, RunReportRequest
from google.oauth2 import service_account
from googleapiclient.discovery import build


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

DEFAULT_MODEL = "gemini-2.5-flash"
UA = "PONTE-SEO-Analyzer/1.0 (+website quality audit)"


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


def credential_info(uploaded_file, pasted_json: str):
    raw = uploaded_file.getvalue().decode("utf-8") if uploaded_file else pasted_json
    if not raw:
        raw = secret("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if isinstance(raw, dict):
        return raw
    return json.loads(raw) if raw else None


def make_credentials(info):
    if not info:
        return None
    scopes = [
        "https://www.googleapis.com/auth/webmasters.readonly",
        "https://www.googleapis.com/auth/analytics.readonly",
    ]
    return service_account.Credentials.from_service_account_info(info, scopes=scopes)


def property_map_from_settings(text: str) -> dict:
    if not text:
        stored = secret("GA4_PROPERTY_MAP", {})
        if isinstance(stored, dict):
            return dict(stored)
        text = str(stored)
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        result = {}
        for line in text.splitlines():
            if "=" in line:
                host, prop = line.split("=", 1)
                result[host.strip()] = prop.strip()
        return result


def resolve_ga_property(url: str, mapping: dict, direct: str) -> str:
    if direct.strip():
        return direct.strip().replace("properties/", "")
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return str(mapping.get(host, "")).replace("properties/", "")


def resolve_gsc_property(service, url: str) -> str:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    candidates = [url, url.rstrip("/"), f"sc-domain:{host}"]
    sites = service.sites().list().execute().get("siteEntry", [])
    allowed = {x["siteUrl"] for x in sites if x.get("permissionLevel") not in (None, "siteUnverifiedUser")}
    for candidate in candidates:
        if candidate in allowed:
            return candidate
    for site in allowed:
        if site.startswith("sc-domain:") and site.split(":", 1)[1].removeprefix("www.") == host:
            return site
        if urlparse(site).netloc.lower().removeprefix("www.") == host:
            return site
    raise PermissionError(f"Search Consoleで {host} の閲覧権限が見つかりません。")


def fetch_gsc(credentials, url: str) -> pd.DataFrame:
    service = build("searchconsole", "v1", credentials=credentials, cache_discovery=False)
    site_url = resolve_gsc_property(service, url)
    end = date.today() - timedelta(days=3)
    start = end - timedelta(days=89)
    body = {
        "startDate": start.isoformat(), "endDate": end.isoformat(),
        "dimensions": ["query", "page"], "rowLimit": 1000,
        "dataState": "final",
    }
    rows = service.searchanalytics().query(siteUrl=site_url, body=body).execute().get("rows", [])
    return pd.DataFrame([{
        "query": r["keys"][0], "page": r["keys"][1], "clicks": r.get("clicks", 0),
        "impressions": r.get("impressions", 0), "ctr": r.get("ctr", 0), "position": r.get("position", 0)
    } for r in rows])


def fetch_ga4(credentials, property_id: str) -> pd.DataFrame:
    if not property_id:
        raise ValueError("このドメインのGA4プロパティIDが未設定です。")
    client = BetaAnalyticsDataClient(credentials=credentials)
    request = RunReportRequest(
        property=f"properties/{property_id}",
        dimensions=[Dimension(name="landingPagePlusQueryString")],
        metrics=[Metric(name="sessions"), Metric(name="activeUsers"), Metric(name="engagementRate"), Metric(name="keyEvents")],
        date_ranges=[DateRange(start_date="90daysAgo", end_date="yesterday")],
        limit=1000,
    )
    response = client.run_report(request)
    return pd.DataFrame([{
        "landing_page": r.dimension_values[0].value,
        "sessions": int(float(r.metric_values[0].value or 0)),
        "active_users": int(float(r.metric_values[1].value or 0)),
        "engagement_rate": float(r.metric_values[2].value or 0),
        "key_events": float(r.metric_values[3].value or 0),
    } for r in response.rows])


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


def build_prompt(url: str, crawl: list[dict], gsc: pd.DataFrame, ga4: pd.DataFrame) -> str:
    gsc_low_ctr = []
    if not gsc.empty:
        candidates = gsc[(gsc.impressions >= 20) & (gsc.position <= 20)].copy()
        gsc_low_ctr = compact_records(candidates, "impressions", 80)
    data = {
        "target_url": url,
        "period": "直近90日（GSCは確定データのため3日前まで）",
        "public_page_audit": crawl,
        "gsc_low_ctr_opportunities": gsc_low_ctr,
        "gsc_top": compact_records(gsc, "clicks", 80),
        "ga4_top_landing_pages": compact_records(ga4, "sessions", 80),
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
    model = st.text_input("Geminiモデル", value=secret("GEMINI_MODEL", DEFAULT_MODEL))
    st.divider()
    st.caption("Googleデータ連携（任意・推奨）")
    credential_file = st.file_uploader("サービスアカウントJSON", type=["json"])
    credential_json = st.text_area("またはJSONを貼り付け", value="", height=90)
    ga_property = st.text_input("GA4プロパティID（今回だけ）", value="")
    ga_map_text = st.text_area("ドメイン別GA4設定", value="", placeholder='ponte-nene.jp=123456789\nponte-aroma.jp=987654321')
    st.caption("Secrets設定済みなら、ここへの毎回入力は不要です。")

st.title("PONTE SEO改善アプリ")
st.markdown('<p class="subtle">サイトの実測データから、改善案とリライト原稿をまとめて作成します。</p>', unsafe_allow_html=True)

url_input = st.text_input("特定のサイトのURL", placeholder="https://ponte-nene.jp", label_visibility="collapsed")
analyze = st.button("分析する", type="primary", use_container_width=True)

if analyze:
    try:
        url = normalize_url(url_input)
        if not api_key:
            st.error("サイドバーの「Gemini APIキー」を設定してください。")
            st.stop()

        progress = st.progress(0, text="公開ページを確認しています…")
        crawl = crawl_site(url)
        progress.progress(30, text="Googleデータを確認しています…")

        gsc_df, ga4_df = pd.DataFrame(), pd.DataFrame()
        warnings = []
        try:
            info = credential_info(credential_file, credential_json)
            credentials = make_credentials(info)
            if credentials:
                try:
                    gsc_df = fetch_gsc(credentials, url)
                except Exception as exc:
                    warnings.append(f"Search Console: {exc}")
                try:
                    mapping = property_map_from_settings(ga_map_text)
                    prop = resolve_ga_property(url, mapping, ga_property)
                    ga4_df = fetch_ga4(credentials, prop)
                except Exception as exc:
                    warnings.append(f"GA4: {exc}")
            else:
                warnings.append("Google認証が未設定のため、公開ページのみ分析しました。")
        except Exception as exc:
            warnings.append(f"Google認証情報を読み取れませんでした: {exc}")

        progress.progress(60, text="SEO課題と改善優先度を分析しています…")
        prompt = build_prompt(url, crawl, gsc_df, ga4_df)
        report = run_ai(api_key, model, prompt)
        progress.progress(100, text="分析が完了しました。")
        time.sleep(.2)
        progress.empty()

        st.success("分析が完了しました")
        if warnings:
            with st.expander("データ連携のお知らせ"):
                for warning in warnings:
                    st.warning(warning)

        c1, c2, c3 = st.columns(3)
        c1.metric("確認ページ", f"{sum('error' not in x for x in crawl)}件")
        c2.metric("GSCデータ", f"{len(gsc_df):,}行")
        c3.metric("GA4データ", f"{len(ga4_df):,}行")

        st.subheader("最重要結論")
        st.info(report.get("executive_summary", ""))

        with st.expander("SEO・マーケティング改善点", expanded=True):
            priorities = pd.DataFrame(report.get("priorities", []))
            if not priorities.empty:
                st.dataframe(priorities, use_container_width=True, hide_index=True)
            target = report.get("rewrite_target", {})
            if target:
                st.markdown(f"**最優先リライト:** {target.get('url','')}  \n**理由:** {target.get('reason','')}  \n**方向性:** {target.get('direction','')}")

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
        st.caption("URL、APIキー、Google APIの有効化、権限設定をご確認ください。")

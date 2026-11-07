"""Unattended, read-only PONTE monthly reporting job. Run from repository root."""
from __future__ import annotations

import base64
import json
import os
import re
import sys
from datetime import date, timedelta
from email.message import EmailMessage
from io import BytesIO
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from google import genai
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
from xml.sax.saxutils import escape


TARGETS = (
    ("ぽんて鍼灸整骨院", "サイト", "ponte-nene.jp", "NENE"),
    ("ぽんてアロマサロン", "サイト", "ponte-aroma.jp", "AROMA"),
    ("ぽんておすすめブログ", "サイト", "ponte-nene.net", "BLOG"),
    ("ぽんて鍼灸整骨院", "GBP", "ponte-nene.jp", "NENE"),
    ("ぽんてアロマサロン", "GBP", "ponte-aroma.jp", "AROMA"),
)
ROOT = Path(__file__).resolve().parent
OUT = ROOT / "monthly_reports"
SESSION = requests.Session()


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"必要な設定 {name} がありません。")
    return value


def period(today):
    end = today.replace(day=1) - timedelta(days=1)
    start = end.replace(day=1)
    for _ in range(2):
        start = (start - timedelta(days=1)).replace(day=1)
    return start, end


def api(method, url, token, **kwargs):
    headers = {"Authorization": f"Bearer {token}", **kwargs.pop("headers", {})}
    response = SESSION.request(method, url, headers=headers,
                               timeout=45, **kwargs)
    if not response.ok:
        raise RuntimeError(f"Google API HTTP {response.status_code}。対象の閲覧権限・API承認・IDを確認してください。")
    return response.json()


def refresh_token():
    response = SESSION.post("https://oauth2.googleapis.com/token", data={
        "client_id": required("GOOGLE_CLIENT_ID"),
        "client_secret": required("GOOGLE_CLIENT_SECRET"),
        "refresh_token": required("GOOGLE_REFRESH_TOKEN"),
        "grant_type": "refresh_token",
    }, timeout=30)
    if not response.ok:
        raise RuntimeError("Googleの更新トークンを利用できません。再同意・テスト状態・OAuth設定を確認してください。")
    return response.json()["access_token"]


def gsc(token, property_url, start, end):
    url = "https://www.googleapis.com/webmasters/v3/sites/" + quote(property_url, safe="") + "/searchAnalytics/query"
    datasets = {}
    for dimension in ("query", "page"):
        result = api("POST", url, token, json={"startDate": str(start), "endDate": str(end),
            "dimensions": [dimension], "rowLimit": 1000, "type": "web"})
        datasets[dimension] = [{"key": r["keys"][0], "clicks": r.get("clicks"),
            "impressions": r.get("impressions"), "ctr": r.get("ctr"), "position": r.get("position")}
            for r in result.get("rows", [])]
    # Date breakdown makes the actual last available day visible in the report.
    dates = api("POST", url, token, json={"startDate": str(start), "endDate": str(end),
        "dimensions": ["date"], "rowLimit": 1000, "type": "web"})
    datasets["daily"] = [{"date": r["keys"][0], "clicks": r.get("clicks"),
        "impressions": r.get("impressions")} for r in dates.get("rows", [])]
    return datasets


def ga4(token, property_id, start, end):
    if not property_id.isdigit():
        raise ValueError("GA4プロパティIDは数字です。")
    url = f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport"
    def run(dimensions, limit):
        result = api("POST", url, token, json={"dateRanges": [{"startDate": str(start), "endDate": str(end)}],
            "dimensions": [{"name": d} for d in dimensions],
            "metrics": [{"name": m} for m in ("sessions", "activeUsers", "engagementRate", "keyEvents")],
            "limit": str(limit), "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}]})
        return [{"dimensions": [v.get("value") for v in row.get("dimensionValues", [])],
                 "sessions": row["metricValues"][0].get("value"),
                 "users": row["metricValues"][1].get("value"),
                 "engagement_rate": row["metricValues"][2].get("value"),
                 "key_events": row["metricValues"][3].get("value")}
                for row in result.get("rows", [])]
    return {"landing_pages": run(["landingPagePlusQueryString"], 1000),
            "daily": run(["date"], 1000)}


def gbp(token, location, start, end):
    if not re.fullmatch(r"locations/\d+", location):
        raise ValueError("GBP店舗IDは locations/数字 の形式です。")
    base = f"https://businessprofileperformance.googleapis.com/v1/{location}"
    metrics = ("BUSINESS_IMPRESSIONS_DESKTOP_MAPS", "BUSINESS_IMPRESSIONS_MOBILE_MAPS",
        "BUSINESS_IMPRESSIONS_DESKTOP_SEARCH", "BUSINESS_IMPRESSIONS_MOBILE_SEARCH",
        "BUSINESS_DIRECTION_REQUESTS", "CALL_CLICKS", "WEBSITE_CLICKS")
    params = [("dailyMetrics", m) for m in metrics]
    for label, day in (("start", start), ("end", end)):
        for part in ("year", "month", "day"):
            params.append((f"dailyRange.{label}_date.{part}", getattr(day, part)))
    series = api("GET", base + ":fetchMultiDailyMetricsTimeSeries", token, params=params)
    daily = []
    for group in series.get("multiDailyMetricTimeSeries", []):
        for item in group.get("dailyMetricTimeSeries", []):
            for point in item.get("timeSeries", {}).get("datedValues", []):
                d = point.get("date", {})
                daily.append({"date": f"{d.get('year', 0):04d}-{d.get('month', 0):02d}-{d.get('day', 0):02d}",
                              "metric": item.get("dailyMetric"), "count": point.get("value", "0")})
    keyword_params = {"monthlyRange.start_month.year": start.year,
        "monthlyRange.start_month.month": start.month, "monthlyRange.end_month.year": end.year,
        "monthlyRange.end_month.month": end.month, "pageSize": 100}
    keywords = []
    for _ in range(3):
        result = api("GET", base + "/searchkeywords/impressions/monthly", token, params=keyword_params)
        keywords.extend(result.get("searchKeywordsCounts", []))
        if not result.get("nextPageToken"):
            break
        keyword_params["pageToken"] = result["nextPageToken"]
    return {"daily": daily, "keywords": keywords}


def digest(kind, data):
    """Bound prompt size while retaining monthly trends, top pages/terms and coverage."""
    if kind == "サイト":
        g, a = data["gsc"], data["ga4"]
        return {"gsc_top_queries": g["query"][:40], "gsc_top_pages": g["page"][:40],
                "gsc_daily": g["daily"], "ga4_top_landing_pages": a["landing_pages"][:40],
                "ga4_daily": a["daily"]}
    monthly = {}
    for row in data["daily"]:
        key = (row["date"][:7], row["metric"])
        monthly[key] = monthly.get(key, 0) + int(row["count"])
    return {"monthly_metrics": [{"month": month, "metric": metric, "count": value}
                                for (month, metric), value in sorted(monthly.items())],
            "available_dates": sorted({x["date"] for x in data["daily"]}),
            "keywords_top": data["keywords"][:40]}


def analysis(name, kind, start, end, compact):
    client = genai.Client(api_key=required("GEMINI_API_KEY"))
    prompt = f"""あなたは日本のローカル集客に詳しいSEO/MEOコンサルタント。{name}の{kind}を分析する。
対象期間は{start}〜{end}。月別の比較、集客導線、優先度を検討する。月初の未反映データを考慮し、
実際の最終取得日と欠損を明記。GSC、GA4、GBPの数値定義を混同せず、クリックを予約・来店と断定しない。
データにない事実・効果・順位は創作しない。上位行には上限がある。医療効果の断定を避ける。
初心者が実行できるよう、以下を日本語のMarkdownで具体的に書く：
1. 対象・期間・データ取得範囲・未確定事項、2. 3か月の推移と根拠、3. 優先度順の今月の3施策
（対象ページまたはプロフィール箇所、クリック操作や文案、担当者、所要時間、確認方法）、
4. 4週間の実施予定、5. 次月に確認するKPIと比較条件、6. 要確認事項。
分析データ: {json.dumps(compact, ensure_ascii=False, separators=(',', ':'))}"""
    model = os.environ.get("GEMINI_MODEL", "").strip() or "gemini-3.6-flash"
    response = client.models.generate_content(model=model, contents=prompt)
    if not response.text:
        raise RuntimeError("Geminiの回答が空です。")
    return response.text


def make_pdf(title, markdown):
    fontfile = ROOT / "NotoSansJP-Regular.ttf"
    font = "PonteJapanese" if fontfile.exists() else "HeiseiKakuGo-W5"
    if font == "PonteJapanese" and font not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(font, str(fontfile)))
    elif font != "PonteJapanese":
        pdfmetrics.registerFont(UnicodeCIDFont(font))
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=40, rightMargin=40,
                            topMargin=42, bottomMargin=42)
    style = ParagraphStyle("body", fontName=font, fontSize=9, leading=15, spaceAfter=7,
                           wordWrap="CJK")
    heading = ParagraphStyle("heading", parent=style, fontSize=14, leading=21, spaceAfter=13)
    story = [Paragraph(escape(title), heading), Spacer(1, 8)]
    for line in markdown.splitlines():
        if not line.strip():
            story.append(Spacer(1, 5))
            continue
        clean = re.sub(r"^\s{0,3}(#{1,6}\s*|[-*]\s+|\d+\.\s+)", "", line)
        clean = escape(clean).replace("**", "")
        story.append(Paragraph(clean, style))
    doc.build(story)
    return buffer.getvalue()


def upload_drive(token, filename, content, mime, folder):
    metadata = {"name": filename}
    if folder:
        metadata["parents"] = [folder]
    boundary = "ponte_monthly_report_boundary"
    body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
        + json.dumps(metadata, ensure_ascii=False) + f"\r\n--{boundary}\r\nContent-Type: {mime}\r\n\r\n").encode()
    body += content + f"\r\n--{boundary}--\r\n".encode()
    result = api("POST", "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,webViewLink",
                 token, data=body, headers={"Content-Type": f"multipart/related; boundary={boundary}"})
    return result.get("webViewLink") or f"https://drive.google.com/file/d/{result['id']}/view"


def send_mail(token, recipient, subject, body):
    msg = EmailMessage()
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")
    api("POST", "https://gmail.googleapis.com/gmail/v1/users/me/messages/send", token,
        json={"raw": raw})


def main(today=None):
    today = today or (date.fromisoformat(os.environ["REPORT_DATE"]) if os.environ.get("REPORT_DATE")
                      else __import__("datetime").datetime.now(ZoneInfo("Asia/Tokyo")).date())
    start, end = period(today)
    token = refresh_token()
    folder = os.environ.get("DRIVE_FOLDER_ID", "").strip()
    recipient = os.environ.get("REPORT_EMAIL_TO", "").strip()
    OUT.mkdir(exist_ok=True)
    links, errors = [], []
    for name, kind, host, suffix in TARGETS:
        label = f"{name}_{kind}_{today.isoformat()}"
        try:
            if kind == "サイト":
                gsc_property = required("GSC_" + suffix)
                property_id = required("GA4_" + suffix)
                data = {"gsc": gsc(token, gsc_property, start, end),
                        "ga4": ga4(token, property_id, start, end)}
                if not data["gsc"]["daily"] and not data["ga4"]["daily"]:
                    raise RuntimeError("GSCとGA4に期間内のデータがありません。")
            else:
                data = gbp(token, required("GBP_" + suffix), start, end)
                if not data["daily"]:
                    raise RuntimeError("GBPの期間内のデータがありません。")
            if kind == "サイト":
                gsc_dates = [x["date"] for x in data["gsc"]["daily"]]
                ga4_dates = [x["dimensions"][0] for x in data["ga4"]["daily"]]
                coverage = (f"GSC取得日: {min(gsc_dates)}〜{max(gsc_dates)}" if gsc_dates else "GSC: 期間内データなし")
                coverage += " / " + (f"GA4取得日: {min(ga4_dates)}〜{max(ga4_dates)}" if ga4_dates else "GA4: 期間内データなし")
            else:
                dates = [x["date"] for x in data["daily"]]
                coverage = f"GBP日別指標の取得日: {min(dates)}〜{max(dates)}"
                coverage += f" / 検索語句: {len(data['keywords'])}件（最大300件）"
            report = f"取得範囲: {coverage}\n\n" + analysis(name, kind, start, end, digest(kind, data))
            title = f"{name} {kind} 3か月分析（{start}〜{end}）"
            pdf = make_pdf(title, report)
            md = f"# {title}\n\n{report}\n".encode("utf-8")
            for extension, content, mime in (("pdf", pdf, "application/pdf"),
                                              ("md", md, "text/markdown")):
                filename = f"{label}.{extension}"
                (OUT / filename).write_bytes(content)
                link = upload_drive(token, filename, content, mime, folder)
                links.append(f"{name}（{kind}）{extension.upper()}: {link}")
            print(f"作成・保存完了: {label}")
        except Exception as exc:
            # Never print HTTP response bodies, secrets, or raw exceptions from providers.
            errors.append(f"{name}（{kind}）: {type(exc).__name__}: {str(exc) if isinstance(exc, (ValueError, RuntimeError)) else '処理に失敗しました'}")
            print(f"失敗: {name}（{kind}）", file=sys.stderr)
    summary = f"分析対象期間: {start}〜{end}\n作成日: {today}\n\n" + "\n".join(links)
    if errors:
        summary += "\n\n未完了:\n" + "\n".join(errors)
    (OUT / f"実行結果_{today}.txt").write_text(summary, encoding="utf-8")
    if recipient:
        send_mail(token, recipient, f"PONTE 3か月分析レポート {today}", summary)
    if errors:
        raise RuntimeError(f"{len(errors)}件の分析が未完了です。実行結果ファイルを確認してください。")
    print("5件の分析とDrive保存が完了しました。")


if __name__ == "__main__":
    main()

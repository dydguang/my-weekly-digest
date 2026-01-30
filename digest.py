import os
import json
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone

import requests
from openai import OpenAI
print("DIGEST VERSION: 2026-01-30 CT-LEGACY")

TZ_UTC = timezone.utc


def pubmed_search(query: str, days_back: int = 7, retmax: int = 20):
    """
    PubMed via NCBI E-utilities:
    - ESearch to get PMIDs
    - ESummary to get title/journal/date
    Docs: https://eutils.ncbi.nlm.nih.gov/entrez/eutils/  (NCBI Bookshelf)
    """
    # date range (UTC)
    end = datetime.now(TZ_UTC).date()
    start = (datetime.now(TZ_UTC) - timedelta(days=days_back)).date()

    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
    esearch = base + "esearch.fcgi"
    esummary = base + "esummary.fcgi"

    params = {
        "db": "pubmed",
        "term": query,
        "retmode": "json",
        "retmax": str(retmax),
        "datetype": "pdat",
        "mindate": str(start),
        "maxdate": str(end),
    }
    r = requests.get(esearch, params=params, timeout=30)
    r.raise_for_status()
    pmids = r.json().get("esearchresult", {}).get("idlist", [])
    if not pmids:
        return []

    r2 = requests.get(
        esummary,
        params={"db": "pubmed", "id": ",".join(pmids), "retmode": "json"},
        timeout=30,
    )
    r2.raise_for_status()
    data = r2.json().get("result", {})
    items = []
    for pid in pmids:
        rec = data.get(pid, {})
        title = rec.get("title", "").strip().rstrip(".")
        journal = rec.get("fulljournalname", "")
        pubdate = rec.get("pubdate", "")
        url = f"https://pubmed.ncbi.nlm.nih.gov/{pid}/"
        items.append(
            {
                "source": "PubMed",
                "id": f"PMID:{pid}",
                "title": title or f"PubMed record {pid}",
                "meta": f"{journal} | {pubdate}",
                "url": url,
                "snippet": "",  # ESummary usually doesn't include abstract
            }
        )
    return items


def clinicaltrials_search(query: str, days_back: int = 7, page_size: int = 20):
    endpoint = "https://clinicaltrials.gov/api/v2/studies"
    cutoff_date = (datetime.now(TZ_UTC) - timedelta(days=days_back)).date().isoformat()

    # 方案A：用 sort=LastUpdatePostDate:desc（新站点 v2 常见写法）
    params = {
        "query.term": query,
        "pageSize": str(page_size),
        "sort": "LastUpdatePostDate:desc",
    }

    r = requests.get(endpoint, params=params, timeout=30)
    if r.status_code == 400:
        # 方案B（兜底）：不用 sort，改用“网站同款语法”把 LastUpdatePostDate 写进 query 里
        # 网站 URL 里常见 term=AREA[LastUpdatePostDate] RANGE[YYYY-MM-DD,MAX] 这种表达式
        # 这样即使 sort 规则变动，也能先拿到最近更新的数据
        params = {
            "query.term": f'AREA[LastUpdatePostDate] RANGE[{cutoff_date},MAX] AND ({query})',
            "pageSize": str(page_size),
        }
        r = requests.get(endpoint, params=params, timeout=30)

    r.raise_for_status()
    js = r.json()

    studies = js.get("studies", [])
    items = []
    for s in studies:
        prot = (s.get("protocolSection") or {})
        ident = (prot.get("identificationModule") or {})
        status = (prot.get("statusModule") or {})

        nct = ident.get("nctId") or ""
        title = ident.get("briefTitle") or "Clinical trial update"
        last_update = status.get("lastUpdatePostDate") or ""
        overall = status.get("overallStatus") or ""
        url = f"https://clinicaltrials.gov/study/{nct}" if nct else "https://clinicaltrials.gov/"

        items.append(
            {
                "source": "ClinicalTrials.gov",
                "id": f"NCT:{nct}" if nct else f"CT:{hash(title)}",
                "title": title,
                "meta": f"{overall} | last update: {last_update}",
                "url": url,
                "snippet": "",
            }
        )

    return items




def build_prompt(items):
    lines = []
    for it in items:
        lines.append(
            f"- [{it['source']}] {it['title']}\n"
            f"  Meta: {it['meta']}\n"
            f"  Link: {it['url']}\n"
        )

    return f"""
你是一名血液肿瘤方向的研究助理。请基于我给你的条目列表，写一份中文周报（不是医疗建议）。
要求：
1) 只能使用条目中提供的信息；不要编造疗效数字、样本量、结论强度。若信息不足，请明确写“条目未提供，需阅读全文确认”。
2) 每条结论都必须附上对应链接。
3) 输出固定结构：
   - TL;DR（三条以内）
   - 研究进展（按主题：CAR-T / 双抗 / 其他新药 / MRD与诊断 / 真实世界与安全性；没有就略过）
   - 临床试验更新（列出 NCT 号、状态、更新时间）
   - 下周关注点（2-5条关键词）
4) 风格：简洁、可追溯、像研究组内部周报。

条目列表：
{chr(10).join(lines)}
""".strip()


def generate_report(items):
    client = OpenAI()
    prompt = build_prompt(items)

    resp = client.responses.create(
        model="gpt-4o-mini",
        input=prompt,
    )
    # responses API output text
    out = resp.output_text
    return out


def send_email(subject: str, body: str):
    host = os.environ["SMTP_HOST"]
    port = int(os.environ["SMTP_PORT"])
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    sender = os.environ["SMTP_FROM"]
    to_addr = os.environ["REPORT_EMAIL_TO"]

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to_addr

    with smtplib.SMTP(host, port, timeout=30) as server:
        server.starttls()
        server.login(user, password)
        server.sendmail(sender, [to_addr], msg.as_string())


def main():
    topic_pubmed = "multiple myeloma OR plasma cell myeloma"
    topic_ct = "multiple myeloma"
    days_back = 7

    items = []
    items.extend(pubmed_search(topic_pubmed, days_back=days_back, retmax=20))
    items.extend(clinicaltrials_search(topic_ct, days_back=days_back, page_size=20))

    # 去重（按 id）
    uniq = {}
    for it in items:
        uniq[it["id"]] = it
    items = list(uniq.values())

    report = generate_report(items)

    subject = "多发性骨髓瘤｜每周研究进展周报"
    send_email(subject, report)
    print("OK: report generated and sent.")


if __name__ == "__main__":
    main()

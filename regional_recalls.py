"""Regional model-notice search. Only the vehicle description leaves the workshop."""
import json
import re
from functools import lru_cache
from html import unescape
from time import time
from urllib.parse import urlencode, urlsplit
from urllib.request import urlopen
from xml.etree import ElementTree

OECD = "https://globalrecalls.oecd.org"
NZ = "https://www.vehiclerecallsafety.nzta.govt.nz/index.html"
MARKETS = {
    "ca": ["CA"], "gb": ["GB"], "au": ["AU"], "jp": ["JP"], "kr": ["KR"], "ae": ["AE"],
    "eu": "EEA AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE".split(),
}
PUBLISHERS = {
    "sa": ("ar", "SA", "SA:ar", "استدعاء", ("mc.gov.sa", "recalls.sa", "spa.gov.sa")),
    "cn": ("zh-CN", "CN", "CN:zh-Hans", "召回", ("samr.gov.cn", "samrdprc.org.cn")),
}
# Keep Latin badges searchable alongside the local names used in official notices.
ALIASES = {
    "accord": ("أكورد", "雅阁"), "civic": ("سيفيك", "思域"), "prius": ("بريوس", "普锐斯"),
    "corolla": ("كورولا", "卡罗拉"), "camry": ("كامري", "凯美瑞"), "cr-v": ("سي آر في", "CR-V"),
    "rav4": ("راف فور", "RAV4"), "model 3": ("موديل 3", "Model 3"),
}
MAKES = {"honda": ("هوندا", "本田"), "toyota": ("تويوتا", "丰田"), "tesla": ("تسلا", "特斯拉")}


def clean(value):
    return " ".join(re.findall(r"[\w-]+", value, re.UNICODE))


def safe_link(value):
    return isinstance(value, str) and urlsplit(value).scheme in ("https", "http") and bool(urlsplit(value).hostname)


def oecd_notices(region, make, model):
    countries = " OR ".join("country-were-recalled:" + code for code in MARKETS[region])
    params = urlencode(dict(q=f'"{make.lower()}" "{model.lower()}" ({countries})', start=0, end=20,
                            lang="en", uiLang="en", sort="date", order="desc"))
    with urlopen(OECD + "/ws/search.xqy?" + params, timeout=15) as response:
        data = json.load(response)
    results = data["results"]
    total = data["total"]
    if not isinstance(results, list) or not isinstance(total, int) or total < len(results) or (total and not results):
        raise ValueError("Incomplete OECD response")
    records = []
    for row in results:
        url = row.get("extUrl")
        if not safe_link(url):
            url = OECD + "/ws/getrecall.xqy?" + urlencode({"uri": row["uri"]})
        records.append({"title": row["product.name"], "date": row["date"], "url": url})
    return {"source": "OECD GlobalRecalls", "total": total, "records": records}


@lru_cache(maxsize=1)
def nz_feed(hour):
    with urlopen("https://admin.fuelsaver.govt.nz/GetRecalls.aspx?mode=nzta", timeout=20) as response:
        rows = json.load(response)
    if not isinstance(rows, list) or not rows or "RecallType" not in rows[0]:
        raise ValueError("Missing NZTA feed")
    return rows


def nz_notices(make, model):
    badge = re.compile(r"(?<!\w)" + re.escape(model) + r"(?!\w)", re.I)
    records = []
    for row in nz_feed(int(time() // 3600)):
        if (row["RecallType"] != "Safety Recall" or make.casefold() != (row["Make"] or "").strip().casefold()
                or not badge.search(row["Models"] or "")):
            continue
        title = " · ".join(str(row[k]).strip() for k in ("CampaignNumber", "Models", "Years") if row.get(k))
        records.append({"title": title, "date": "", "description": unescape(re.sub("<[^>]+>", " ", row.get("Statement") or "")),
                        "url": row["WebsiteURL"] if safe_link(row.get("WebsiteURL")) else NZ})
    return {"source": "NZ Transport Agency", "total": len(records), "records": records[:20]}


def indexed_notices(region, make, model):
    lang, country, edition, recall, hosts = PUBLISHERS[region]
    index = 0 if region == "sa" else 1
    def term(text, aliases):
        alias = aliases.get(text.casefold(), (text, text))[index]
        return f'"{text}"' if alias == text else f'("{text}" OR "{alias}")'
    domains = " OR ".join("site:" + host for host in hosts)
    query = f"{term(make, MAKES)} {term(model, ALIASES)} {recall} ({domains})"
    url = "https://news.google.com/rss/search?" + urlencode(dict(q=query, hl=lang, gl=country, ceid=edition))
    with urlopen(url, timeout=15) as response:
        channel = ElementTree.parse(response).find("channel")
    if channel is None:
        raise ValueError("Missing search feed")
    records = {}
    for item in channel.findall("item"):
        source = item.find("source")
        host = urlsplit(source.get("url", "")).hostname if source is not None else ""
        link = item.findtext("link", "")
        if (not host or not any(host == h or host.endswith("." + h) for h in hosts)
                or not safe_link(link) or urlsplit(link).hostname != "news.google.com"):
            continue
        records[link] = {"title": item.findtext("title"), "date": item.findtext("pubDate"), "url": link}
    return {"source": "Indexed government notices (Google News)", "total": len(records), "records": list(records.values())[:20]}


@lru_cache(maxsize=256)
def regional_notices(region, make, model, hour):
    make, model = clean(make), clean(model)
    if not make or not model:
        raise ValueError("Missing vehicle description")
    if region == "nz":
        return nz_notices(make, model)
    if region in PUBLISHERS:
        return indexed_notices(region, make, model)
    return oecd_notices(region, make, model)


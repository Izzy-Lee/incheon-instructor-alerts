"""인천 강사 공고 수집기.

sources.yaml 의 사이트를 읽어 강사 공고만 골라 data/postings.json 에 쌓고,
오늘 요약(reports/날짜.md), 이슈 본문(out/issue.md), 웹페이지 데이터(docs/data.json)를 만든다.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlencode

import requests
import urllib3
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
KST = timezone(timedelta(hours=9))
NOW = datetime.now(KST)
TODAY = NOW.date()
URGENT_DAYS = 3
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
DATE_RE = re.compile(r"(20\d\d)\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{1,2})")
SHORT_DATE_RE = re.compile(r"(?<![\d.])(\d{2})[-.](\d{2})[-.](\d{2})(?![\d.])")  # 26.11.25 같은 두 자리 연도
RANGE_RE = re.compile(
    r"(20\d\d\s*[-./]\s*\d{1,2}\s*[-./]\s*\d{1,2})\s*[~∼-]\s*(20\d\d\s*[-./]\s*\d{1,2}\s*[-./]\s*\d{1,2})"
)
WEEK = "월화수목금토일"

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})


# ---------- 공통 ----------
def load_yaml(name):
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))


def fetch(url: str) -> str:
    last = None
    for attempt in range(2):
        try:
            try:
                r = session.get(url, timeout=20)
            except requests.exceptions.SSLError:
                # 일부 공공기관 사이트는 인증서 체인이 불완전함. 공개 게시판 읽기만 하므로 재시도.
                r = session.get(url, timeout=20, verify=False)
            r.raise_for_status()
            if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
                r.encoding = r.apparent_encoding
            return r.text
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise last


def to_date(s: str | None):
    if not s:
        return None
    m = DATE_RE.search(s)
    y = int(m[1]) if m else None
    if not m:
        m = SHORT_DATE_RE.search(s)
        if not m:
            return None
        y = 2000 + int(m[1])
    try:
        return datetime(y, int(m[2]), int(m[3])).date()
    except ValueError:
        return None


def clean(s: str) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    s = re.sub(r"^(공지|NOTICE)\s+", "", s)
    for junk in ("새로운 글", "NEW", "첨부파일"):
        s = s.replace(junk, " ")
    return re.sub(r"\s+", " ", s).strip()


def norm_title(t: str) -> str:
    t = re.sub(r"\[[^\]]*공고[^\]]*\]", "", t)
    return re.sub(r"[\s\W_]+", "", t).lower()


def make_id(title: str) -> str:
    return hashlib.sha1(norm_title(title).encode()).hexdigest()[:12]


# ---------- 사이트별 읽기 ----------
def parse_platform(src):
    """인천일자리플랫폼 공공채용 목록 (여러 페이지)."""
    out, page = [], 1
    while page <= 10:
        q = urlencode({"page": page, "pageRows": 50, "srchKey": "ABCD", "srchSort": "deadline_ASC"})
        soup = BeautifulSoup(fetch(f"{src['url']}?{q}"), "html.parser")
        rows = soup.select("table tbody tr")
        if not rows:
            break
        for tr in rows:
            a = tr.select_one(".pubPolicies_name a")
            if not a:
                continue
            m = re.search(r"rcrut_info_sn=(\d+)", a.get("href", ""))
            link = urljoin(src["url"], f"view.do?rcrut_info_sn={m[1]}") if m else src["url"]
            info = clean(" ".join(li.get_text(" ") for li in tr.select(".pubPolicies_item")))
            dm = re.search(r"마감일\s*(20\d\d-\d\d-\d\d)", info)
            sm = re.search(r"시작일\s*(20\d\d-\d\d-\d\d)", info)
            out.append(
                {
                    "title": clean(a.get_text(" ")),
                    "org": clean(tr.select_one(".pubPolicies_org").get_text(" ")) if tr.select_one(".pubPolicies_org") else "",
                    "via": clean(tr.select_one(".pubPolicies_ministry").get_text(" ")) if tr.select_one(".pubPolicies_ministry") else "",
                    "url": link,
                    "posted": sm[1] if sm else None,
                    "deadline": dm[1] if dm else None,
                }
            )
        total = re.search(r"총\s*([\d,]+)\s*건", soup.get_text(" "))
        if not total or page * 50 >= int(total[1].replace(",", "")):
            break
        page += 1
    return out


def parse_links(src):
    """일반 게시판: 날짜가 함께 적힌 줄(tr/li)의 링크를 공고로 본다. urls 목록이 있으면 모두 읽는다."""
    urls = src.get("urls") or [src["url"]]
    out, seen = [], set()
    for u in urls:
        try:
            out += _parse_links_page({**src, "url": u}, seen)
        except Exception:  # noqa: BLE001
            if len(urls) == 1:
                raise
        time.sleep(0.3)
    return out


def _parse_links_page(src, seen):
    soup = BeautifulSoup(fetch(src["url"]), "html.parser")
    out = []
    for a in soup.find_all("a"):
        text = clean(a.get("title") if (a.get_text(strip=True).endswith("...") and a.get("title")) else a.get_text(" "))
        if len(text) < 6:
            continue
        row = a.find_parent(["tr", "li"])
        if row is None:  # 표·목록이 아닌 카드형(div) 게시판: 날짜가 보이는 가까운 상자까지 올라감
            row = a
            for _ in range(3):
                row = row.parent
                if row is None:
                    break
                t = row.get_text(" ")
                if len(t) > 400:
                    row = None
                    break
                if DATE_RE.search(t) or SHORT_DATE_RE.search(t):
                    break
            if row is None:
                continue
        row_text = clean(row.get_text(" "))
        if not (DATE_RE.search(row_text) or SHORT_DATE_RE.search(row_text)) or len(row_text) > 400:
            continue
        href = a.get("href", "") or ""
        link = src["url"] if (not href or href.startswith(("javascript", "#"))) else urljoin(src["url"], href)
        key = norm_title(text)
        if key in seen:
            continue
        seen.add(key)
        rng = RANGE_RE.search(row_text)
        dates = [d for d in (to_date(x.group(0)) for x in DATE_RE.finditer(row_text)) if d] or [
            d for d in (to_date(x.group(0)) for x in SHORT_DATE_RE.finditer(row_text)) if d
        ]
        out.append(
            {
                "title": text,
                "org": src.get("org") or src["name"].replace(" 채용공고", ""),
                "via": src["name"],
                "url": link,
                "posted": dates[0].isoformat() if dates else None,
                "deadline": to_date(rng[2]).isoformat() if rng and to_date(rng[2]) else None,
            }
        )
    return out


def parse_gojobs(src):
    """나라일터 모집공고: 검색어별로 최근 몇 페이지를 읽고 기관·제목에 '인천'이 들어간 것만 남긴다."""
    base = "https://www.gojobs.go.kr/"
    out = []
    for kw in src.get("keywords", ["강사"]):
        for page in range(1, src.get("pages", 10) + 1):
            q = urlencode({"menuNo": 401, "searchKeyword": kw, "pageIndex": page})
            soup = BeautifulSoup(fetch(f"{base}apmList.do?{q}"), "html.parser")
            links = soup.select("a[href*=fn_apmView]")
            if not links:
                break
            for a in links:
                tr = a.find_parent("tr")
                cells = [clean(td.get_text(" ")) for td in tr.find_all("td")]
                if len(cells) < 5:
                    continue
                title, org = clean(a.get_text(" ")), cells[2]
                regions = src.get("region", "인천")
                regions = regions if isinstance(regions, list) else [regions]
                if not any(r in (org + title) for r in regions):
                    continue
                m = re.search(r"'(\d+)'\s*,\s*'(\d+)'", a.get("href", ""))
                url = (
                    f"{base}apmView.do?" + urlencode({"menuNo": 401, "searchJobsecode": m[1], "empmnsn": m[2]})
                    if m
                    else src["url"]
                )
                out.append(
                    {
                        "title": title,
                        "org": org,
                        "via": src["name"],
                        "url": url,
                        "posted": to_date(cells[3]).isoformat() if to_date(cells[3]) else None,
                        "deadline": to_date(cells[4]).isoformat() if to_date(cells[4]) else None,
                    }
                )
            time.sleep(0.5)
    return out


def parse_ice(src):
    """인천광역시교육청 채용공고 게시판 (등록일·모집상태·기관·직종·제목·모집종료일...)."""
    base = "https://www.ice.go.kr/ice/na/ntt/"
    out = []
    for page in range(1, src.get("pages", 2) + 1):
        q = urlencode({"mi": src["mi"], "bbsId": src["bbsId"], "listCo": 100, "currPage": page})
        soup = BeautifulSoup(fetch(f"{base}selectNttList.do?{q}"), "html.parser")
        links = soup.select("a.nttInfoBtn")
        if not links:
            break
        for a in links:
            tds = [clean(td.get_text(" ")) for td in a.find_parent("tr").find_all("td")]
            if len(tds) < 6:
                continue
            nid = a.get("data-id")
            out.append(
                {
                    "title": clean(a.get("title") or a.get_text(" ")),
                    "org": re.sub(r"\((공립|사립)\)", "", tds[2]).strip(),
                    "kind": tds[3],
                    "status": tds[1],
                    "via": src["name"],
                    "url": f"{base}selectNttInfo.do?" + urlencode({"mi": src["mi"], "bbsId": src["bbsId"], "nttSn": nid})
                    if nid
                    else src["url"],
                    "posted": to_date(tds[0]).isoformat() if to_date(tds[0]) else None,
                    "deadline": to_date(tds[5]).isoformat() if to_date(tds[5]) else None,
                }
            )
    return out


def parse_rss(src):
    """RSS 피드 (그누보드 게시판 등)."""
    soup = BeautifulSoup(fetch(src["url"]), "html.parser")
    out = []
    for item in soup.find_all("item"):
        title = clean(item.find("title").get_text(" ")) if item.find("title") else ""
        link_tag = item.find("link")
        link = (link_tag.get_text(strip=True) or (link_tag.next_sibling or "").strip()) if link_tag else src["url"]
        date_tag = item.find(["pubdate", "dc:date", "date"])
        posted = None
        if date_tag:
            raw = date_tag.get_text(" ", strip=True)
            d = to_date(raw)
            if not d:
                try:
                    from email.utils import parsedate_to_datetime
                    d = parsedate_to_datetime(raw).astimezone(KST).date()
                except Exception:  # noqa: BLE001
                    d = None
            posted = d.isoformat() if d else None
        out.append({"title": title, "org": src.get("org") or src["name"], "via": src["name"],
                    "url": link or src["url"], "posted": posted, "deadline": None})
    return out


def parse_eminwon(src):
    """새올 고시공고 목록: 링크 대신 칸(td)에 searchDetail('번호')가 걸린 형식."""
    soup = BeautifulSoup(fetch(src["url"]), "html.parser")
    base = src["url"].split("?")[0]
    out = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        m = None
        for td in tds:
            m = re.search(r"searchDetail\('(\d+)'\)", td.get("onclick", "") or "")
            if m:
                break
        if not m or len(tds) < 4:
            continue
        cells = [clean(td.get_text(" ")) for td in tds]
        title = max(cells, key=len)
        dates = [to_date(c) for c in cells if to_date(c)]
        out.append({
            "title": title, "org": src.get("org") or src["name"], "via": src["name"],
            "url": base + "?" + urlencode({"jndinm": "OfrNotAncmtEJB", "context": "NTIS", "method": "selectOfrNotAncmt",
                                           "methodnm": "selectOfrNotAncmtRegst", "homepage_pbs_yn": "Y", "subCheck": "Y",
                                           "not_ancmt_mgt_no": m[1]}),
            "posted": dates[0].isoformat() if dates else None, "deadline": None,
        })
    return out


PARSERS = {"eminwon": parse_eminwon, "platform": parse_platform, "links": parse_links, "gojobs": parse_gojobs, "ice": parse_ice, "rss": parse_rss}


# ---------- 분류 ----------
def classify(item, kw):
    t = item["title"]
    if any(x in t for x in kw["exclude"]):
        return None
    kind = item.get("kind", "")
    if not any(x in t for x in kw["include"]) and not any(x in kind for x in kw.get("include_kinds", [])):
        return None
    hay = t + " " + item.get("org", "")
    for c in kw["categories"]:
        if not c["words"] or any(w in hay for w in c["words"]):
            return None if c.get("drop") else c
    return kw["categories"][-1]


def fmt_date(iso):
    d = to_date(iso)
    return f"{d.month}.{d.day}({WEEK[d.weekday()]})" if d else "미기재"


def dday(iso):
    d = to_date(iso)
    if not d:
        return None
    n = (d - TODAY).days
    return "D-DAY" if n == 0 else (f"D-{n}" if n > 0 else "마감")


def line(p):
    dl = f"마감 {fmt_date(p['deadline'])}" + (f" · {dday(p['deadline'])}" if p.get("deadline") else "")
    org = f"{p['org']} · " if p.get("org") else ""
    if p.get("kind"):
        org += f"{p['kind']} · "
    return f"- **[{p['category']}]** {org}[{p['title']}]({p['url']})  \n  {dl} · 출처: {p['via']}"


# ---------- 메인 ----------
def main():
    sources = load_yaml("sources.yaml")
    kw = load_yaml("keywords.yaml")
    db_path = ROOT / "data" / "postings.json"
    db = json.loads(db_path.read_text(encoding="utf-8")) if db_path.exists() else {}
    first_run = not db

    status, found = [], {}
    for src in sources:
        print(f"수집: {src['name']}", flush=True)
        try:
            items = PARSERS[src["type"]](src)
            hits = 0
            for it in items:
                cat = classify(it, kw)
                if not cat:
                    continue
                dl = to_date(it.get("deadline"))
                if dl and dl < TODAY:  # 이미 마감된 공고는 건너뜀
                    continue
                ps = to_date(it.get("posted"))
                if not dl and ps and (TODAY - ps).days > 45:  # 마감일 모르는 오래된 글은 건너뜀
                    continue
                hits += 1
                it["category"], it["mine"] = cat["name"], bool(cat.get("mine"))
                pid = make_id(it["title"])
                if pid in found:  # 여러 사이트에 같은 공고 → 합치기
                    old = found[pid]
                    old["deadline"] = old.get("deadline") or it.get("deadline")
                    if it["via"] not in old["via"]:
                        old["via"] += f", {it['via']}"
                    continue
                found[pid] = it
            status.append({"name": src["name"], "ok": True, "total": len(items), "hits": hits})
        except Exception as e:  # noqa: BLE001
            status.append({"name": src["name"], "ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"})
        time.sleep(1)

    today = TODAY.isoformat()
    new, changed = [], []
    for pid, it in found.items():
        if pid not in db:
            db[pid] = {**it, "first_seen": today}
            new.append(db[pid])
        else:
            prev = db[pid]
            if it.get("deadline") and prev.get("deadline") and it["deadline"] != prev["deadline"]:
                changed.append({**it, "old_deadline": prev["deadline"]})
            db[pid] = {**prev, **it, "deadline": it.get("deadline") or prev.get("deadline")}
        db[pid]["last_seen"] = today

    # 오래된 공고 정리 (마감 14일 지남 또는 60일간 안 보임)
    for pid in list(db):
        p = db[pid]
        dl, ls = to_date(p.get("deadline")), to_date(p.get("last_seen"))
        if (dl and (TODAY - dl).days > 14) or (ls and (TODAY - ls).days > 60):
            del db[pid]

    urgent = sorted(
        [p for p in db.values() if p.get("deadline") and 0 <= (to_date(p["deadline"]) - TODAY).days <= URGENT_DAYS],
        key=lambda p: p["deadline"],
    )
    new.sort(key=lambda p: (not p["mine"], p.get("deadline") or "9999"))
    mine = [p for p in db.values() if p["mine"] and (not p.get("deadline") or to_date(p["deadline"]) >= TODAY)]

    ok = sum(s["ok"] for s in status)
    title = f"[인천 강사공고] {TODAY.month}/{TODAY.day}({WEEK[TODAY.weekday()]}) 신규 {len(new)}건 · 마감임박 {len(urgent)}건"
    md = [
        f"## 인천 강사 구인공고 일일 요약 — {today} ({WEEK[TODAY.weekday()]})",
        "",
        f"신규 {len(new)}건 · 마감 임박 {len(urgent)}건 · 변경 {len(changed)}건 · 수집 소스 {ok}/{len(status)} 정상",
        "",
    ]
    if first_run:
        md += ["> 첫 실행이라 지금 올라와 있는 공고가 모두 신규로 잡혔어요. 내일부터는 새로 올라온 것만 나와요.", ""]
    md += ["### 🎨 내 분야 (미술·공예)", ""]
    md += [line(p) for p in sorted(mine, key=lambda p: p.get("deadline") or "9999")] or ["없음"]
    md += ["", f"### ⏰ 마감 임박 ({URGENT_DAYS}일 이내)", ""]
    md += [line(p) for p in urgent] or ["없음"]
    md += ["", "### 🆕 신규 공고", ""]
    md += [line(p) for p in new] or ["없음"]
    md += ["", "### 🔄 변경 공고", ""]
    md += [line(p) + f" (이전 마감 {fmt_date(p['old_deadline'])})" for p in changed] or ["없음"]
    md += ["", "### 📎 수집 상태", "", "| 소스 | 상태 | 읽은 글 | 강사 공고 |", "|---|---|---|---|"]
    for s in status:
        md.append(
            f"| {s['name']} | {'정상' if s['ok'] else '실패'} | {s.get('total', '-')} | {s.get('hits', s.get('error', ''))} |"
        )
    md += ["", f"실행 시각 {NOW.strftime('%H:%M')} KST"]
    body = "\n".join(md)

    (ROOT / "data").mkdir(exist_ok=True)
    db_path.write_text(json.dumps(db, ensure_ascii=False, indent=1), encoding="utf-8")
    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "reports" / f"{today}.md").write_text(f"# {title}\n\n{body}\n", encoding="utf-8")
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs" / "data.json").write_text(
        json.dumps(
            {"updated": NOW.strftime("%Y-%m-%d %H:%M"), "status": status, "postings": sorted(db.values(), key=lambda p: p.get("deadline") or "9999")},
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    out = ROOT / "out"
    out.mkdir(exist_ok=True)
    (out / "issue_title.txt").write_text(title, encoding="utf-8")
    (out / "issue.md").write_text(body, encoding="utf-8")
    (out / "should_post").write_text("yes" if (new or urgent or changed) else "no")
    print(title)
    print(json.dumps(status, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

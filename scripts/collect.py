"""인천 강사 공고 수집기.

sources.yaml 의 사이트를 읽어 강사 공고만 골라 data/postings.json 에 쌓고,
오늘 요약(reports/날짜.md), 이슈 본문(out/issue.md), 웹페이지 데이터(docs/data.json)를 만든다.
"""
from __future__ import annotations

import hashlib
import os
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


def tidy_title(t: str) -> str:
    """목록 줄 전체가 링크로 잡힌 경우 제목만 남긴다 (예: '지원사업 제목 OOO 공지 2026-11-23 792 예술지원팀')."""
    t = clean(t)
    if "제목 " in t[:20]:
        t = t.split("제목 ", 1)[1]
    t = re.sub(r"^.*?D-\d+\s+(마감일자\s+\S+\s+)?", "", t) if re.search(r"D-\d+", t[:40]) else t
    t = re.sub(r"새로운\s?게시글|새글|NEW", " ", t)
    m = DATE_RE.search(t)
    if m and m.start() > 12:
        t = t[: m.start()]
    w = t.split()
    for i in range(3, len(w) // 2 + 1):  # 'A B C A B' 처럼 제목이 두 번 붙은 경우
        a, b = " ".join(w[:i]), " ".join(w[i:])
        if a.startswith(b) or b.startswith(a):
            t = " ".join(w[:i])
            break
    t = re.sub(r"[\s\[(]+$", "", t.strip())
    t = re.sub(r"\s+(공지|첨부파일|파일있음)\s*$", "", t)
    return re.sub(r"\s+", " ", t).strip()


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
        text = (tidy_title if src.get("tidy") else clean)(a.get("title") if (a.get_text(strip=True).endswith("...") and a.get("title")) else a.get_text(" "))
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
        if src.get("detail") and link == src["url"]:
            mm = re.search(r"(\d{3,})", href + " " + (a.get("onclick") or ""))
            if mm:
                link = src["detail"].format(id=mm[1])
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
                "deadline": (to_date(rng[2]).isoformat() if rng and to_date(rng[2]) else
                             (to_date(dm[1]).isoformat() if (dm := re.search(r"마감\S*\s*(20\d\d[-./]\d{1,2}[-./]\d{1,2})", row_text)) else None)),
                "extra": row_text,
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


def parse_zighang(src):
    """직행(zighang.com) 채용공고 API: 지역(구·시) + 검색어로 최신순 조회."""
    api = "https://api.zighang.com/api/recruitments/v4"
    art = re.compile(r"미술|공예|그림|드로잉|수채|회화|캘리|일러스트|웹툰|만화|도예")
    drop = set(src.get("drop_categories", []))
    out, seen = [], set()
    for kw in src.get("keywords", ["강사"]):
        for page in range(src.get("pages", 2)):
            params = [("page", page), ("size", 100), ("sortCondition", "LATEST"), ("orderCondition", "DESC"), ("keyword", kw)]
            params += [("districts", d) for d in src["districts"]]
            r = session.get(api, params=params, timeout=20,
                            headers={"Origin": "https://zighang.com", "Referer": "https://zighang.com/recruitment"})
            r.raise_for_status()
            content = (r.json().get("data") or {}).get("content") or []
            for c in content:
                if c["id"] in seen:
                    continue
                seen.add(c["id"])
                cats = c.get("depthTwos") or []
                if drop & set(cats) and not art.search(c.get("title", "")):
                    continue
                out.append({
                    "title": clean(c.get("title", "")),
                    "org": ((c.get("company") or {}).get("name") or "").strip(),
                    "kind": "·".join(cats),
                    "via": src["name"],
                    "url": f"https://zighang.com/recruitment/{c['id']}",
                    "posted": (c.get("createdAt") or "")[:10] or None,
                    "deadline": to_date(c.get("endDate")).isoformat() if to_date(c.get("endDate")) else None,
                })
            if len(content) < 100:
                break
            time.sleep(0.5)
    return out


def parse_neulbom(src):
    """늘봄허브 '학교별 요청 공고' (지역 필터, 모집기간 ±1~2달)."""
    base = "https://neulbomhub.kosac.re.kr/hmpg/prpb/papr/"
    out = []
    for rgn in src.get("regions", ["R1200"]):
        data = {
            "miv_pageNo": 1, "miv_pageSize": 100, "searchkey": "prrg", "scType": "PG1", "recmnt_month": 0,
            "recmnt_bgng": (TODAY - timedelta(days=30)).isoformat(), "recmnt_end": (TODAY + timedelta(days=90)).isoformat(),
            "list_prgrm_status": "Y", "list_prgrm_status_ALL": "Y", "list_oper_rgn_lclsf_cd": rgn, "page_size_sel": 100,
        }
        r = session.post(base + "paprListPgng.do", data=data, timeout=20,
                         headers={"X-Requested-With": "XMLHttpRequest", "Referer": base + "paprListPage.do"})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        seen = set()
        for a in soup.find_all("a", onclick=re.compile(r"paprDetail")):
            m = re.search(r"paprDetail\('(\w+)',\s*'(\w+)'\)", a.get("onclick", ""))
            if not m or m[2] in seen:
                continue
            seen.add(m[2])
            card = a
            for _ in range(5):
                if card.parent is None or len(card.get_text(" ")) > 250:
                    break
                card = card.parent
            txt = clean(card.get_text(" "))
            if "모집마감" in txt:
                continue
            title = clean(a.get_text(" ")) or txt[:60]
            school = re.search(r"학교명\s*(\S+)", txt)
            area = re.search(r"지역\s*(\S+\s?\S*?)\s+학교명", txt)
            kind = re.search(r"분류명\s*(\S+)", txt)
            rng = RANGE_RE.search(txt)
            out.append({
                "title": f"[늘봄 {kind[1] if kind else '프로그램'}] {title}",
                "org": (school[1] if school else "") + (f" ({area[1]})" if area else ""),
                "kind": "늘봄 강사 요청",
                "via": src["name"],
                "url": base + "paprDetail.do?" + urlencode({"prgrm_pbanc_mng_no": m[2]}),
                "posted": to_date(rng[1]).isoformat() if rng and to_date(rng[1]) else None,
                "deadline": to_date(rng[2]).isoformat() if rng and to_date(rng[2]) else None,
            })
    return out


def parse_g2b(src):
    """나라장터(g2b.go.kr) 입찰공고 목록: 공고명 검색어별로 올해 게시분 조회 (로그인 없이 보는 공개 목록)."""
    import html as _html
    base = "https://www.g2b.go.kr"
    s = requests.Session()
    s.headers.update({"User-Agent": session.headers.get("User-Agent", "Mozilla/5.0"), "Accept-Language": "ko-KR,ko;q=0.9"})
    s.get(base + "/", timeout=30)  # 익명 세션 쿠키 받기
    hdr = {
        "Content-Type": "application/json;charset=UTF-8", "Accept": "application/json",
        "Origin": base, "Referer": base + "/",
        "Menu-Info": '{"menuNo":"01175","menuCangVal":"PNPE001_01","bsneClsfCd":"%EC%97%85130026","scrnNo":"00941"}',
        "Target-Id": "btnS0004", "submissionid": "mf_wfm_container_tacBidPbancLst_contents_tab2_body_sbmPbancBidPbancLst", "Usr-Id": "",
    }
    region = re.compile(src.get("region_re", "인천"))
    anywhere = re.compile(src.get("nationwide_re", "$^"))
    kinds = set(src.get("kinds", ["물품", "일반용역"]))
    start = src.get("from", f"{TODAY.year}0101")
    out, seen = [], set()
    for kw in src["keywords"]:
        for page in range(src.get("pages", 3)):
            body = {"dlBidPbancLstM": {
                "untyBidPbancNo": "", "bidPbancNo": "", "bidPbancOrd": "", "prcmBsneUntyNoOrd": "",
                "prcmBsneSeCd": "0000 조070001 조070002 조070003 조070004 조070005 민079999",
                "bidPbancNm": kw, "pbancPstgDt": "", "ldocNoVal": "", "bidPrspPrce": "", "ctrtDmndRcptNo": "", "dmstcOvrsSeCd": "",
                "pbancKndCd": "공440002", "ctrtTyCd": "", "bidCtrtMthdCd": "", "scsbdMthdCd": "",
                "fromBidDt": start, "toBidDt": TODAY.strftime("%Y%m%d"), "minBidPrspPrce": "", "maxBidPrspPrce": "",
                "bsneAllYn": "Y", "frcpYn": "Y", "rsrvYn": "Y", "laseYn": "Y", "untyGrpGb": "", "dmstNm": "", "pbancPicNm": "",
                "odnLmtLgdngCd": "", "odnLmtLgdngNm": "", "intpCd": "", "intpNm": "", "dtlsPrnmNo": "", "dtlsPrnmNm": "",
                "slprRcptDdlnYn": "", "lcrtTyCd": "", "isMas": "", "isElpdt": "", "oderInstUntyGrpNo": "", "instSearchRangeYn": "",
                "esdacYn": "", "infoSysCd": "정010029", "contxtSeCd": "콘010006", "bidDateType": "R", "brcoOrgnCd": "", "deptOrgnCd": "",
                "isShop": "", "srchTy": "0", "cangParmVal": "", "currentPage": str(page + 1), "recordCountPerPage": "100",
                "startIndex": page * 100 + 1, "endIndex": page * 100 + 100}}
            r = s.post(base + "/pn/pnp/pnpe/BidPbac/selectBidPbacScrollTypeList.do",
                       data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=hdr, timeout=40)
            r.raise_for_status()
            rows = r.json().get("result") or []
            for x in rows:
                no, ordn = x.get("bidPbancUntyNo") or x.get("untyBidPbancNo"), x.get("bidPbancUntyOrd") or "000"
                if not no or no in seen:
                    continue
                seen.add(no)
                if x.get("prcmBsneSeCdNm") not in kinds:
                    continue
                if "취소" in (x.get("pbancSttsNm") or ""):
                    continue
                title = clean(_html.unescape(x.get("bidPbancNm") or ""))
                org = _html.unescape(x.get("dmstNm") or x.get("oderInstUntyGrpNm") or "").strip()
                place = org + " " + _html.unescape(x.get("oderInstUntyGrpNm") or "")
                local = bool(region.search(place))
                if not local and not anywhere.search(title):
                    continue
                dt = _html.unescape(x.get("pbancPstgDt") or "")
                ds = re.findall(r"(\d{4})/(\d{2})/(\d{2})", dt)
                posted = "-".join(ds[0]) if ds else None
                deadline = "-".join(ds[1]) if len(ds) > 1 else None
                price = x.get("prspPrce") or x.get("alotBgtAmt")
                won = f"{int(float(price)) // 10000:,}만원" if price and str(price).replace(".", "").isdigit() and float(price) > 0 else ""
                out.append({
                    "title": title,
                    "org": org,
                    "kind": " · ".join(v for v in [x.get("prcmBsneSeCdNm"), won, "인천·근교" if local else "전국", x.get("pbancSttsNm") if x.get("pbancSttsNm") != "등록공고" else ""] if v),
                    "via": src["name"],
                    "url": f"{base}/link/PNPE027_01/single/?bidPbancNo={no}&bidPbancOrd={ordn}",
                    "posted": posted,
                    "deadline": deadline,
                    "extra": org,
                })
            if len(rows) < 100:
                break
            time.sleep(0.5)
        time.sleep(0.5)
    return out


PARSERS = {"g2b": parse_g2b, "neulbom": parse_neulbom, "zighang": parse_zighang, "eminwon": parse_eminwon, "platform": parse_platform, "links": parse_links, "gojobs": parse_gojobs, "ice": parse_ice, "rss": parse_rss}


# ---------- 분류 ----------
def classify(item, kw):
    t = item["title"]
    t_ex = t + " " + item.get("extra", "") if kw.get("exclude_in_row") else t
    if any(x in t_ex for x in kw["exclude"]):
        return None
    kind = item.get("kind", "")
    if not any(x in t for x in kw["include"]) and not any(x in kind for x in kw.get("include_kinds", [])):
        return None
    hay = t + " " + item.get("org", "")
    for c in kw["categories"]:
        if c.get("need") and not any(n in hay + " " + kind for n in c["need"]):
            continue
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
PROFILES = {
    "instructors": dict(sources="sources.yaml", keywords="keywords.yaml", db="data/postings.json", reports="reports",
                        page_data="docs/data.json", out="out", prefix="[인천 강사공고]",
                        heading="인천 강사 구인공고 일일 요약", mine_label="🎨 내 분야 (미술·공예)"),
    "grants": dict(sources="grants/sources.yaml", keywords="grants/keywords.yaml", db="data/grants.json", reports="reports/grants",
                   page_data="docs/grants.json", out="out/grants", prefix="[지원사업 공고]",
                   heading="인천 지원사업·공모 일일 요약", mine_label="⭐ 잘 맞는 분야"),
    "bids": dict(sources="bids/sources.yaml", keywords="bids/keywords.yaml", db="data/bids.json", reports="reports/bids",
                 page_data="docs/bids.json", out="out/bids", prefix="[나라장터 입찰]",
                 heading="나라장터 입찰공고 일일 요약 (할 만한 일)", mine_label="⭐ 인천·근교 + 내 분야"),
}
PROFILE = PROFILES[os.environ.get("PROFILE", "instructors")]


def main():
    sources = load_yaml(PROFILE["sources"])
    only = [x for x in os.environ.get("ONLY_SOURCES", "").split(",") if x]
    if only:  # 예고 기간 집중 확인: 해당 게시판만
        sources = [x for x in sources if x["id"] in only]
    kw = load_yaml(PROFILE["keywords"])
    db_path = ROOT / PROFILE["db"]
    db = json.loads(db_path.read_text(encoding="utf-8")) if db_path.exists() else {}
    first_run = not db

    status, found = [], {}
    for src in sources:
        print(f"수집: {src['name']}", flush=True)
        try:
            items = PARSERS[src["type"]](src)
            hits = 0
            src_ex = src.get("exclude") or []
            for it in items:
                if any(x in it["title"] for x in src_ex):
                    continue
                cat = classify(it, kw)
                if not cat:
                    continue
                dl = to_date(it.get("deadline"))
                if dl and dl < TODAY:  # 이미 마감된 공고는 건너뜀
                    continue
                ps = to_date(it.get("posted"))
                if not dl and ps and (TODAY - ps).days > 45:  # 마감일 모르는 오래된 글은 건너뜀
                    continue
                it.pop("extra", None)
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
        [p for p in db.values() if to_date(p.get("deadline")) and 0 <= (to_date(p["deadline"]) - TODAY).days <= URGENT_DAYS],
        key=lambda p: p["deadline"],
    )
    new.sort(key=lambda p: (not p["mine"], p.get("deadline") or "9999"))
    mine = [p for p in db.values() if p["mine"] and (not to_date(p.get("deadline")) or to_date(p["deadline"]) >= TODAY)]

    ok = sum(s["ok"] for s in status)
    title = f"{PROFILE['prefix']} {TODAY.month}/{TODAY.day}({WEEK[TODAY.weekday()]}) 신규 {len(new)}건 · 마감임박 {len(urgent)}건"
    md = [
        f"## {PROFILE['heading']} — {today} ({WEEK[TODAY.weekday()]})",
        "",
        f"신규 {len(new)}건 · 마감 임박 {len(urgent)}건 · 변경 {len(changed)}건 · 수집 소스 {ok}/{len(status)} 정상",
        "",
    ]
    if first_run:
        md += ["> 첫 실행이라 지금 올라와 있는 공고가 모두 신규로 잡혔어요. 내일부터는 새로 올라온 것만 나와요.", ""]
    md += [f"### {PROFILE['mine_label']}", ""]
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

    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_text(json.dumps(db, ensure_ascii=False, indent=1), encoding="utf-8")
    (ROOT / PROFILE["reports"]).mkdir(parents=True, exist_ok=True)
    (ROOT / PROFILE["reports"] / f"{today}.md").write_text(f"# {title}\n\n{body}\n", encoding="utf-8")
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / PROFILE["page_data"]).write_text(
        json.dumps(
            {"updated": NOW.strftime("%Y-%m-%d %H:%M"), "status": status, "postings": sorted(db.values(), key=lambda p: p.get("deadline") or "9999")},
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    out = ROOT / PROFILE["out"]
    out.mkdir(parents=True, exist_ok=True)
    (out / "issue_title.txt").write_text(title, encoding="utf-8")
    (out / "issue.md").write_text(body, encoding="utf-8")
    (out / "should_post").write_text("yes" if (new or urgent or changed) else "no")
    print(title)
    print(json.dumps(status, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

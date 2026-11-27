"""지원사업 예고 캘린더.

근거 두 가지로 내년(다가오는 1년) 공고를 예고한다.
  1. 올해 실제 공고: 지원사업 게시판의 지난 글(최근 13개월)을 모아, 같은 사업이 1년 뒤 비슷한 날 다시 뜬다고 본다.
  2. 이미 나온 계획: K-Startup '중앙부처 및 지자체 창업지원사업 통합공고' 표의 공고시기(예: '26.3~4월).
     새 해 통합공고가 올라오면 자동으로 받아 그 해 계획으로 바꾼다.

공고가 실제로 뜨면(지원사업 수집 결과와 이름이 맞으면) 예고를 '공고 떴음'으로 바꾸고 마감일도 캘린더에 넣는다.
결과: docs/forecast.ics (구글 캘린더 구독용), docs/forecast.json, data/grants_history.json, data/plan_rows.json
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

os.environ.setdefault("PROFILE", "grants")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect  # noqa: E402

ROOT = collect.ROOT
TODAY = collect.TODAY
HISTORY = ROOT / "data" / "grants_history.json"
PLAN = ROOT / "data" / "plan_rows.json"
OUT_ICS = ROOT / "docs" / "forecast.ics"
OUT_JSON = ROOT / "docs" / "forecast.json"
HIST_BACK_DAYS = 400          # 지난 글을 이만큼 거슬러 모음 (13개월)
HIST_REFRESH_DAYS = 7         # 지난 글 전체 다시 모으기 주기
WATCH_BEFORE, WATCH_AFTER = 10, 25   # 예고일 전후 이 기간엔 '집중 확인'
KSTARTUP_LIST = "https://www.k-startup.go.kr/web/contents/bizpbanc-ongoing.do"
KSTARTUP_VIEW = KSTARTUP_LIST + "?schM=view&pbancSn={sn}"


# ---------- 이름 맞추기 ----------
def prog_key(title: str) -> str:
    t = title or ""
    t = re.sub(r"[\[(（【<][^\])）】>]*(결과|발표|연장|변경|정정|재공고|추가)[^\])）】>]*[\])）】>]", " ", t)
    t = re.sub(r"(20\d\d|[’‘']\d\d)\s*(년도?|학년도)?", " ", t)
    t = re.sub(r"(제?\s*\d+\s*(차|기|회))|(상|하)반기|1분기|2분기|3분기|4분기", " ", t)
    t = re.sub(r"\(\s*~?[\d./~\s]+\)", " ", t)  # (11/16~12/1) 같은 기간
    for w in ["공개모집", "모집공고", "모집 공고", "공모 공고", "공고문", "공고", "모집", "공모", "안내", "신청", "접수",
              "재공고", "추가", "연장", "참여기업", "참가기업", "참여자", "대상자", "알림", "사전"]:
        t = t.replace(w, " ")
    return re.sub(r"[\W_]+", "", t).lower()


def same_program(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if len(a) >= 6 and len(b) >= 6 and (a in b or b in a):
        return True
    return SequenceMatcher(None, a, b).ratio() >= 0.78


# ---------- 1. 올해 실제 공고 ----------
def crawl_history(sources, kw):
    cutoff = TODAY - timedelta(days=HIST_BACK_DAYS)
    items = []
    for src in sources:
        tmpl = src.get("history")
        if not tmpl:
            continue
        got = 0
        for page in range(1, 60):
            try:
                rows = collect._parse_links_page({**src, "url": tmpl.format(page=page)}, set())
            except Exception as e:  # noqa: BLE001
                print("history fail", src["name"], page, e)
                break
            dated = [r for r in rows if collect.to_date(r.get("posted"))]
            if not dated:
                break
            for r in dated:
                if collect.to_date(r["posted"]) >= cutoff:
                    r.pop("extra", None)
                    r["source_id"] = src["id"]
                    items.append(r)
                    got += 1
            # 공지(상단 고정) 글을 빼고 가장 오래된 글이 기준보다 오래되면 멈춤
            oldest = sorted(collect.to_date(r["posted"]) for r in dated)[len(dated) // 3]
            if oldest < cutoff:
                break
            collect.time.sleep(0.4)
        print("history", src["name"], got)
    # 같은 글 중복 제거
    uniq = {}
    for it in items:
        uniq[(it["source_id"], collect.norm_title(it["title"]))] = it
    return list(uniq.values())


def load_history(sources, kw, force=False):
    if HISTORY.exists() and not force:
        d = json.loads(HISTORY.read_text(encoding="utf-8"))
        if (TODAY - date.fromisoformat(d["crawled"])).days < HIST_REFRESH_DAYS:
            return d["items"]
    items = crawl_history(sources, kw)
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text(json.dumps({"crawled": TODAY.isoformat(), "items": items}, ensure_ascii=False, indent=1),
                       encoding="utf-8")
    return items


# ---------- 2. 통합공고 계획 ----------
def find_integrated_notices():
    """K-Startup 모집중 목록에서 'OO년 중앙부처 및 지자체 창업지원사업 통합공고'를 찾는다."""
    found = {}
    for page in (1, 2, 3):
        url = KSTARTUP_LIST + (f"?page={page}" if page > 1 else "")
        html = collect.fetch(url)
        for m in re.finditer(r"go_view\((\d+)\)[^<]*</?[^>]*>[^<]*?((20\d\d)\s*년(?:도)?\s*중앙부처\s*및\s*지자체\s*창업지원사업\s*통합공고)", html):
            found[int(m[3])] = m[1]
        for a in collect.BeautifulSoup(html, "html.parser").find_all("a", href=re.compile(r"go_view")):
            t = collect.clean(a.get_text(" "))
            mm = re.search(r"(20\d\d)\s*년(?:도)?\s*중앙부처\s*및\s*지자체\s*창업지원사업\s*통합공고", t)
            sn = re.search(r"go_view\((\d+)\)", a.get("href", ""))
            if mm and sn:
                found[int(mm[1])] = sn[1]
    return found  # {2026: '175783', 2027: ...}


def parse_plan_pdf(blob: bytes, year: int):
    import pdfplumber  # noqa: PLC0415

    rows = []
    with pdfplumber.open(io.BytesIO(blob)) as pdf:
        for pg in pdf.pages:
            for tb in pg.extract_tables():
                for r in tb:
                    c = [(x or "").replace("\n", " ").strip() for x in r]
                    if not c or not re.fullmatch(r"\d+", c[0] or ""):
                        continue
                    if len(c) == 11 and c[1]:          # 지자체: 번호, 지역, 사업명, 목적, 내용, 대상, 예산, 시기, 소관, 수행, 비고
                        region, name, aim, what, who, timing, owner, runner, note = c[1], c[2], c[3], c[4], c[5], c[7], c[8], c[9], c[10]
                    elif len(c) == 10:                  # 중앙부처: 번호, 사업명, 목적, 내용, 대상, 예산, 시기, 소관, 수행, 비고
                        region, name, aim, what, who, timing, owner, runner, note = "전국", c[1], c[2], c[3], c[4], c[6], c[7], c[8], c[9]
                    else:
                        continue
                    if not re.search(r"\d+\s*월", timing):
                        continue
                    rows.append({"year": year, "region": region, "name": re.sub(r"^ㆍ\s*", "", name), "aim": aim,
                                 "what": what, "who": who, "timing": timing, "owner": owner, "runner": runner,
                                 "note": note})
    return rows


def load_plan():
    plan = json.loads(PLAN.read_text(encoding="utf-8")) if PLAN.exists() else {"years": {}, "checked": ""}
    if plan.get("checked") == TODAY.isoformat():
        return plan
    try:
        notices = find_integrated_notices()
    except Exception as e:  # noqa: BLE001
        print("통합공고 찾기 실패", e)
        notices = {}
    for year, sn in notices.items():
        if str(year) in plan["years"]:
            continue
        try:
            html = collect.fetch(KSTARTUP_VIEW.format(sn=sn))
            soup = collect.BeautifulSoup(html, "html.parser")
            pdf_link = None
            for a in soup.select("a[href*='/afile/fileDownload/']"):
                node, label = a, ""
                for _ in range(5):
                    node = node.parent
                    if node is None:
                        break
                    label = collect.clean(node.get_text(" "))
                    if ".pdf" in label.lower() or ".hwp" in label.lower():
                        break
                if "공고문" in label and ".pdf" in label.lower() and label.lower().count(".pdf") == 1:
                    pdf_link = a["href"]
                    break
            if not pdf_link:
                raise RuntimeError("공고문 PDF를 못 찾음")
            r = collect.session.get(collect.urljoin(KSTARTUP_LIST, pdf_link), timeout=120)
            r.raise_for_status()
            rows = parse_plan_pdf(r.content, year)
            plan["years"][str(year)] = {"sn": sn, "url": KSTARTUP_VIEW.format(sn=sn), "rows": rows}
            print("통합공고", year, "사업", len(rows))
        except Exception as e:  # noqa: BLE001
            print("통합공고 받기 실패", year, e)
    plan["checked"] = TODAY.isoformat()
    PLAN.parent.mkdir(parents=True, exist_ok=True)
    PLAN.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    return plan


def plan_relevant(row, kw):
    """인천·전국 사업 중 우리에게 맞을 만한 것만 (청년·예비·바이오 등 제외)."""
    if row["region"] not in ("인천", "전국"):
        return False
    text = " ".join([row["name"], row["who"], row["note"], row["aim"]])
    if "청년" in row["note"] or re.search(r"만\s*39세\s*이하", row["who"]):
        return False
    if re.search(r"예비\s*(창업|재창업)", row["who"]) and not re.search(r"창업\s*\d+\s*년|기업", row["who"]):
        return False
    if re.search(r"(3|2|1)\s*년\s*(이내|미만)", row["who"]) and "7년" not in row["who"]:
        return False
    if any(x in text for x in kw["exclude"] if x not in ("공고", "모집", "신청", "접수")):
        return False
    if any(x in text for x in ["농식품", "재창업", "재도전", "초격차", "딥테크", "스포츠", "ICT", "신산업", "반도체", "바이오"]):
        return False
    if row["region"] == "전국":
        # 전국 사업은 업종이 맞는 것만
        good = ["콘텐츠", "디자인", "문화", "관광", "로컬", "여성", "판로", "수출", "글로벌", "소상공인", "공예", "제품화",
                "브랜드", "마케팅", "중장년", "시니어", "1인", "지식재산", "IP", "특허", "전시회"]
        return any(g in text for g in good)
    return True


def timing_months(timing: str, plan_year: int):
    """''26.3~4월' → (2026, 3, 4), '’25.12월' → (2025, 12, 12), ''26년 2월, 5월' → (2026, 2, 2)"""
    ym = re.search(r"[’‘'`]\s*(\d\d)", timing)
    year = 2000 + int(ym[1]) if ym else plan_year
    t = re.sub(r"[’‘'`]\s*\d\d\s*(년|\.)?", " ", timing)
    nums = [int(x) for x in re.findall(r"(\d{1,2})\s*(?=~|월|-|\s*~)", t) if 1 <= int(x) <= 12]
    if not nums:
        return None
    m1, m2 = nums[0], nums[1] if len(nums) > 1 and "~" in t else nums[0]
    return year, m1, max(m1, m2)


FORECAST_NEED = re.compile(r"모집|공모|지원사업|참여기업|참가기업|셀러|입주|공급업체|참가자|운영기관|운영단체|기획자")
FORECAST_SKIP = ["견적", "수의", "임대", "심사위원", "위원 후보", "선정", "간담회", "설명회 개최", "특강", "강좌", "워크숍",
                 "사례공유", "공연팀", "공연단체", "음악회", "합창단", "단원", "콘서트", "사진 공모", "영상 공모", "펜타포트",
                 "고용", "경영안정", "융자", "이음카드", "스크랩", "수장고", "공사", "심의일정", "사전 질의", "홍보단",
                 "서포터즈", "자원봉사", "수상자", "SNS", "의견 수렴", "참여자 모집", "수강생", "관람", "체험객", "청년",
                 "포상관광", "컨벤션", "기업회의", "마이스", "MICE", "이사(", "감사(", "대표이사", "노무사", "정보보호",
                 "사이버보안", "블록체인", "의료", "푸드트럭", "정부포상", "연구장비", "연구조직", "회계검증", "중복지원",
                 "교육생", "취업", "재창업", "인터뷰 심의", "어린이 모집"]


def forecast_worthy(title: str) -> bool:
    return bool(FORECAST_NEED.search(title)) and not any(x in title for x in FORECAST_SKIP)


# ---------- 예고 만들기 ----------
def build(kw, history, plan, current):
    events = {}
    horizon = TODAY + timedelta(days=365)

    # 1) 올해 실제 공고 → 1년 뒤 예고
    for it in history:
        if not forecast_worthy(it["title"]):
            continue
        cat = collect.classify({**it, "extra": ""}, kw)
        if not cat or cat.get("drop"):
            continue
        posted = collect.to_date(it["posted"])
        guess = posted.replace(year=posted.year + 1) if not (posted.month == 2 and posted.day == 29) else posted + timedelta(days=365)
        while guess < TODAY - timedelta(days=WATCH_AFTER):
            guess = guess.replace(year=guess.year + 1)
        if guess > horizon:
            continue
        key = prog_key(it["title"])
        if not key:
            continue
        ev = events.get(key)
        basis = {"type": "작년 공고", "title": it["title"], "date": it["posted"], "url": it["url"], "via": it.get("via", ""),
                 "source_id": it.get("source_id", "")}
        if ev:
            ev["basis"].append(basis)
            ev["date"] = min(ev["date"], guess.isoformat())
            continue
        name = re.sub(r"^\s*(\[[^\]]*\]\s*)?(20\d\d|[’‘']\d\d)\s*(년도?|학년도)?\s*", "", collect.tidy_title(it["title"])) or it["title"]
        events[key] = {"key": key, "kind": "history", "name": name, "org": it.get("org", ""),
                       "date": guess.isoformat(), "category": cat["name"], "mine": bool(cat.get("mine")),
                       "basis": [basis], "status": "예고"}

    # 2) 통합공고 계획 (가장 최근 해 것)
    years = sorted(int(y) for y in plan.get("years", {}))
    if years:
        latest = years[-1]
        info = plan["years"][str(latest)]
        for row in info["rows"]:
            if not plan_relevant(row, kw):
                continue
            tm = timing_months(row["timing"], latest)
            if not tm:
                continue
            start, estimated = date(tm[0], tm[1], 1), False
            while start < TODAY - timedelta(days=WATCH_AFTER):  # 이미 지난 시기면 다음 해 같은 달로 추정
                start, estimated = start.replace(year=start.year + 1), True
            if start > horizon:
                continue
            key = prog_key(row["name"])
            basis = {"type": f"{latest} 통합공고 계획" + (" → 다음 해 같은 시기로 추정" if estimated else ""),
                     "title": row["name"], "date": row["timing"], "url": info["url"],
                     "via": f"{row['owner']} / {row['runner']}", "who": row["who"], "what": row["what"],
                     "source_id": "itp" if "테크노파크" in (row["runner"] + row["owner"]) else "kstartup"}
            hit = next((e for k, e in events.items() if same_program(k, key)), None)
            if hit:
                hit["basis"].append(basis)
                hit.setdefault("plan_months", f"{tm[1]}~{tm[2]}월" if tm[2] != tm[1] else f"{tm[1]}월")
                continue
            cat = collect.classify({"title": row["name"] + " 모집", "org": row["runner"], "extra": ""}, kw) or {"name": "기타"}
            events[key] = {"key": key, "kind": "plan", "name": row["name"], "org": row["runner"] or row["owner"],
                           "date": start.isoformat(),
                           "plan_months": f"{tm[1]}~{tm[2]}월" if tm[2] != tm[1] else f"{tm[1]}월",
                           "category": cat["name"], "mine": bool(cat.get("mine")), "basis": [basis],
                           "status": "예고" if estimated else "계획", "who": row["who"]}

    # 3) 실제로 뜬 공고와 맞추기
    for p in current:
        pk = prog_key(p["title"])
        first = collect.to_date(p.get("first_seen")) or collect.to_date(p.get("posted"))
        for e in events.values():
            if e["status"] == "공고 떴음" or not same_program(e["key"], pk):
                continue
            if first and abs((first - date.fromisoformat(e["date"])).days) > 120:
                continue
            e.update(status="공고 떴음", actual={"title": p["title"], "url": p["url"], "deadline": p.get("deadline"),
                                               "found": (first or TODAY).isoformat()})
    return sorted(events.values(), key=lambda e: e["date"])


# ---------- 캘린더 파일 ----------
def ics_escape(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line: str) -> str:
    out, cur = [], ""
    for ch in line:
        if len((cur + ch).encode("utf-8")) > 73:
            out.append(cur)
            cur = " " + ch
        else:
            cur += ch
    out.append(cur)
    return "\r\n".join(out)


def vevent(uid, day, summary, desc, url=None, days=1):
    d0 = date.fromisoformat(day)
    lines = ["BEGIN:VEVENT", f"UID:{uid}@incheon-instructor-alerts", f"DTSTAMP:{datetime.utcnow():%Y%m%dT%H%M%SZ}",
             f"DTSTART;VALUE=DATE:{d0:%Y%m%d}", f"DTEND;VALUE=DATE:{d0 + timedelta(days=days):%Y%m%d}",
             f"SUMMARY:{ics_escape(summary)}", f"DESCRIPTION:{ics_escape(desc)}", "TRANSP:TRANSPARENT"]
    if url:
        lines.append(f"URL:{url}")
    lines.append("END:VEVENT")
    return [fold(x) for x in lines]


def write_ics(events):
    cal = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//artable//grants-forecast//KO", "CALSCALE:GREGORIAN",
           "METHOD:PUBLISH", fold("X-WR-CALNAME:지원사업 예고 (아트에이블)"), "X-WR-TIMEZONE:Asia/Seoul",
           fold("X-WR-CALDESC:올해 공고와 통합공고 계획으로 만든 인천 지원사업 예고. 공고가 뜨면 ✅로 바뀝니다."),
           "REFRESH-INTERVAL;VALUE=DURATION:PT6H", "X-PUBLISHED-TTL:PT6H"]
    for e in events:
        uid = hashlib.sha1(e["key"].encode()).hexdigest()[:16]
        star = "⭐" if e["mine"] else ""
        basis = "\n".join(f"- {b['type']}: {b['title']} ({b['date']}) {b.get('url', '')}" for b in e["basis"][:4])
        if e["status"] == "공고 떴음":
            a = e["actual"]
            desc = f"공고가 떴어요: {a['title']}\n{a['url']}\n\n[예고 근거]\n{basis}"
            cal += vevent(uid, a["found"], f"✅{star} 공고 떴음: {e['name']}", desc, a["url"])
            if a.get("deadline"):
                cal += vevent(uid + "-dl", a["deadline"], f"⏰{star} 마감: {e['name']}", desc, a["url"])
        else:
            mark = "🗓 계획" if e["status"] == "계획" else "🔮 예고"
            when = f" ({e['plan_months']})" if e.get("plan_months") else ""
            extra = f"\n대상: {e['who']}" if e.get("who") else ""
            desc = (f"{e['org']} · {e['category']}{extra}\n\n이 시기쯤 공고가 뜰 것으로 예상해요. 공고가 뜨면 ✅로 바뀝니다."
                    f"\n\n[근거]\n{basis}")
            cal += vevent(uid, e["date"], f"{mark}{star} {e['name']}{when}", desc, e["basis"][0].get("url"))
    cal.append("END:VCALENDAR")
    OUT_ICS.write_text("\r\n".join(cal) + "\r\n", encoding="utf-8")


def watch_sources(events):
    """예고일 전후 기간인 사업의 게시판 id 목록 (집중 확인용)."""
    ids = set()
    for e in events:
        if e["status"] == "공고 떴음":
            continue
        d = date.fromisoformat(e["date"])
        if -WATCH_BEFORE <= (TODAY - d).days <= WATCH_AFTER:
            for b in e["basis"]:
                ids.add(b.get("source_id", ""))
    return sorted(i for i in ids if i)


def main():
    force = "--refresh-history" in sys.argv
    sources = collect.load_yaml("grants/sources.yaml")
    kw = collect.load_yaml("grants/keywords.yaml")
    history = load_history(sources, kw, force=force)
    plan = load_plan()
    db = ROOT / "data" / "grants.json"
    current = list(json.loads(db.read_text(encoding="utf-8")).values()) if db.exists() else []
    events = build(kw, history, plan, current)
    write_ics(events)
    watch = watch_sources(events)
    OUT_JSON.write_text(json.dumps({"updated": collect.NOW.strftime("%Y-%m-%d %H:%M"), "watch": watch,
                                    "events": events}, ensure_ascii=False, indent=1), encoding="utf-8")
    n = {s: sum(1 for e in events if e["status"] == s) for s in ("예고", "계획", "공고 떴음")}
    print(f"예고 {n['예고']} · 계획 {n['계획']} · 공고 떴음 {n['공고 떴음']} · 지금 집중 확인 {len(watch)}곳")


if __name__ == "__main__":
    main()

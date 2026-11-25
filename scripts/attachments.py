"""⭐지원예정 공고의 첨부파일(공고문·지원서)을 받아 구글드라이브에 저장한다.

표시 목록은 Apps Script 웹앱(MARKS_API)에서 읽고, 받은 파일은 같은 웹앱으로 올린다.
웹앱이 드라이브 「강사지원DB/공고첨부/<공고>」 폴더에 저장하고 시트에 기록한다.

환경변수
  MARKS_API     Apps Script 웹앱 주소 (없으면 docs/config.js 에서 읽음)
  UPLOAD_TOKEN  Apps Script setup 이 만든 업로드 비밀번호 (GitHub Secrets)

시험용: python scripts/attachments.py --test <공고주소> [...]  → 찾은 첨부만 출력, 업로드 안 함
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests
import urllib3
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
EXT_RE = re.compile(r"\.(hwpx?|hwtx|pdf|docx?|xlsx?|zip|jpe?g|png|pptx?|txt|odt)(?![A-Za-z0-9])", re.I)
MAX_TRIES = 3
MAX_BATCH = 20 * 1024 * 1024  # 한 번에 올릴 최대 크기(원본 기준)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def new_session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})
    return s


def req(s, method, url, **kw):
    kw.setdefault("timeout", 30)
    last = None
    for attempt in range(2):
        try:
            try:
                r = s.request(method, url, **kw)
            except requests.exceptions.SSLError:
                r = s.request(method, url, verify=False, **kw)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise last


def page(s, url, **kw):
    r = req(s, "GET", url, **kw)
    if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
        r.encoding = r.apparent_encoding
    return r.text


def js_args(s: str) -> list[str]:
    return [a.replace("\\'", "'") for a in re.findall(r"'((?:[^'\\]|\\.)*)'", s)]


def form_fields(soup, selector):
    f = soup.select_one(selector)
    if not f:
        return {}
    return {i.get("name"): i.get("value", "") for i in f.select("input[name]")}


def link_name(a) -> str:
    t = re.sub(r"\s+", " ", (a.get("title") or "") + " " + a.get_text(" ")).strip()
    t = re.sub(r"^(새창|새 창|첨부파일)\s*", "", t)
    m = re.search(r"([^\s/][^/]*?" + EXT_RE.pattern + ")", t, re.I)
    return re.sub(r"^(새창|새 창)\s*", "", m[1].strip()) if m else ""


# ---------- 사이트별 첨부 찾기: [{name, method, url, data}] ----------
def find_files(s, url, depth=0):
    host = urlparse(url).netloc
    html = page(s, url)
    soup = BeautifulSoup(html, "html.parser")
    out = []

    # 인천일자리플랫폼: 원문(나라일터·교육청) 링크를 따라간다
    if "incheon.go.kr" in host and "/jobs/" in url and depth == 0:
        for a in soup.select("a[href]"):
            h = a["href"]
            if "gojobs.go.kr/apmView" in h or "ice.go.kr/ice/na/ntt/selectNttInfo" in h:
                return find_files(s, urljoin(url, h), depth + 1)

    # 인천광역시교육청: goFileDown('키') → /comm/nttFileDownload.do?fileKey=키 (GET 으로 받아야 함, POST 는 403)
    if "ice.go.kr" in host:
        for a in soup.select("a[href*=goFileDown], a[onclick*=goFileDown]"):
            args = js_args(a.get("href", "") + a.get("onclick", ""))
            if args:
                out.append({"name": link_name(a), "method": "GET", "data": None,
                            "url": urljoin(url, "/comm/nttFileDownload.do?fileKey=" + args[0])})

    # 나라일터: gfn_fileDown('파일명','uuid','저장구분')
    for a in soup.select("a[href*=gfn_fileDown], a[onclick*=gfn_fileDown]"):
        args = js_args(a.get("href", "") + a.get("onclick", ""))
        if len(args) >= 3:
            data = form_fields(soup, "form[name=fileFrm]")
            data.update({"filenm": args[0], "uuid": args[1], "saveGbn": args[2]})
            out.append({"name": args[0], "method": "POST", "data": data, "url": urljoin(url, "/downFile.do")})

    # 미추홀구 등: fnFileDown(downloadUrl,userFileNm,sysFileNm,filePath,fileOrigin) → formCrawling POST
    for a in soup.select("a[href*=fnFileDown], a[onclick*=fnFileDown]"):
        args = js_args(a.get("href", "") + a.get("onclick", ""))
        if len(args) >= 5:
            f = soup.select_one("form[name=formCrawling]")
            action = urljoin(url, f.get("action") if f and f.get("action") else "/other/crawling_file_down.do")
            out.append({"name": args[4] or link_name(a), "method": "POST", "url": action,
                        "data": dict(zip(["downloadUrl", "userFileNm", "sysFileNm", "filePath", "fileOrigin"], args[:5]))})

    # 일반 다운로드 링크 (연수·부평 eminwon FileDown.jsp, 영종 process.file.do 등)
    for a in soup.select("a[href]"):
        h = a["href"]
        if h.strip().lower().startswith(("javascript", "#")) or re.search(r"viewer|preview|미리보기", h + a.get_text(), re.I):
            continue
        if not re.search(r"filedown|file\.do|download|atchfile|attach|fileDn|dn\.do", h, re.I):
            continue
        name = link_name(a) or (EXT_RE.search(unquote(h)) and unquote(h).rsplit("=", 1)[-1])
        if not (name or "다운" in a.get_text()):
            continue
        out.append({"name": name or "", "method": "GET", "url": urljoin(url, h), "data": None})

    # 중복 제거
    seen, uniq = set(), []
    for f in out:
        k = (f["url"], json.dumps(f["data"], sort_keys=True, ensure_ascii=False))
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    return uniq


def filename_from(r, fallback):
    cd = r.headers.get("Content-Disposition", "")
    m = re.search(r"filename\*\s*=\s*([\w-]+)''([^;]+)", cd, re.I)
    if m:
        return unquote(m[2], encoding=m[1] or "utf-8").strip('" ')
    m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.I)
    if m:
        raw = m[1].strip()
        if "%" in raw:
            for enc in ("utf-8", "cp949"):
                try:
                    return unquote(raw, encoding=enc, errors="strict")
                except UnicodeDecodeError:
                    pass
        b = raw.encode("latin-1", errors="ignore")
        for enc in ("utf-8", "cp949"):
            try:
                return b.decode(enc)
            except UnicodeDecodeError:
                pass
        return raw
    return fallback


def download(s, f, referer):
    kw = {"headers": {"Referer": referer}}
    if f["method"] == "POST":
        r = req(s, "POST", f["url"], data=f["data"], **kw)
    else:
        r = req(s, "GET", f["url"], **kw)
    ctype = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
    body = r.content
    if ctype.startswith("text/html") or (len(body) < 2048 and b"<html" in body[:500].lower()):
        raise RuntimeError(f"파일 대신 웹페이지가 옴 ({ctype}, {len(body)}B)")
    name = filename_from(r, f["name"] or "첨부파일")
    if not EXT_RE.search(name) and f["name"] and EXT_RE.search(f["name"]):
        name = f["name"]
    name = unicodedata.normalize("NFC", name)
    mime = ctype if ctype and ctype != "application/octet-stream" else (mimetypes.guess_type(name)[0] or "application/octet-stream")
    return {"name": name, "mime": mime, "bytes": body}


# ---------- 표시 목록·업로드 ----------
def api_url():
    if os.environ.get("MARKS_API"):
        return os.environ["MARKS_API"]
    cfg = (ROOT / "docs" / "config.js").read_text(encoding="utf-8")
    m = re.search(r"https://script\.google\.com/[^'\"]+", cfg)
    if not m:
        raise SystemExit("MARKS_API 가 설정되지 않았어요 (docs/config.js)")
    return m[0]


def key_of(title: str) -> str:
    t = re.sub(r"\[[^\]]*공고[^\]]*\]", "", title or "")
    t = "".join(ch for ch in t if not (ch.isspace() or ch == "_" or unicodedata.category(ch)[0] in "PS"))
    return t.lower()


def post(api, payload):
    r = requests.post(api, data=json.dumps(payload, ensure_ascii=False).encode(), headers={"Content-Type": "text/plain;charset=utf-8"}, timeout=120)
    r.raise_for_status()
    res = r.json()
    if not res.get("ok"):
        raise RuntimeError(f"웹앱 오류: {res}")
    return res


def folder_name(m):
    t = re.sub(r"\s+", " ", m.get("title") or "").strip()
    parts = [str(m.get("deadline") or "").strip(), (m.get("org") or "").strip()]
    head = " ".join(p for p in parts if p)
    return (f"{head} - {t}" if head else t)[:120]


def run():
    api = api_url()
    token = os.environ.get("UPLOAD_TOKEN", "")
    if not token:
        raise SystemExit("UPLOAD_TOKEN 이 없어요 (GitHub Secrets)")
    marks = requests.get(api, params={"action": "marks"}, timeout=60).json()["marks"]
    postings = json.loads((ROOT / "data" / "postings.json").read_text(encoding="utf-8"))
    by_key = {key_of(p["title"]): p for p in postings.values()}

    todo = [(k, m) for k, m in marks.items() if m.get("state") == "plan" and not m.get("saved") and int(m.get("tries") or 0) < MAX_TRIES]
    print(f"지원예정 {sum(m.get('state') == 'plan' for m in marks.values())}건 · 받을 것 {len(todo)}건")
    summary = ["## 첨부 자동 저장", "", "| 공고 | 결과 |", "|---|---|"]
    for k, m in todo:
        p = by_key.get(k, {})
        m = {**{x: p.get(x) for x in ("title", "url", "org", "deadline")}, **{x: v for x, v in m.items() if v}}
        title = m.get("title") or k
        if not m.get("url"):
            post(api, {"action": "result", "token": token, "key": k, "note": "공고 주소를 못 찾음", "done": True})
            summary.append(f"| {title} | 주소 없음 |")
            continue
        s = new_session()
        try:
            found = find_files(s, m["url"])
            files, errors = [], []
            for f in found:
                try:
                    files.append(download(s, f, m["url"]))
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{f['name'] or f['url']}: {e}")
            if not files:
                note = "첨부파일 없음" if not found else "다운로드 실패: " + "; ".join(errors)[:300]
                post(api, {"action": "result", "token": token, "key": k, "note": note, "done": not found})
                summary.append(f"| {title} | {note} |")
                continue
            # 이름 겹치면 번호 붙이기
            names = {}
            for f in files:
                n = f["name"]
                if n in names:
                    names[n] += 1
                    stem, dot, ext = n.rpartition(".")
                    f["name"] = f"{stem} ({names[n]}).{ext}" if dot else f"{n} ({names[n]})"
                else:
                    names[n] = 1
            batch, size, saved = [], 0, []
            def flush():
                if batch:
                    res = post(api, {"action": "upload", "token": token, "key": k, "url": m["url"], "folderName": folder_name(m),
                                     "files": [{"name": f["name"], "mime": f["mime"], "b64": base64.b64encode(f["bytes"]).decode()} for f in batch]})
                    saved.extend(res.get("saved", []))
            for f in files:
                if batch and size + len(f["bytes"]) > MAX_BATCH:
                    flush()
                    batch, size = [], 0
                batch.append(f)
                size += len(f["bytes"])
            flush()
            summary.append(f"| {title} | {len(saved)}개 저장{' (일부 실패 ' + str(len(errors)) + ')' if errors else ''} |")
            print(f"저장: {title} → {saved}")
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}: {str(e)[:200]}"
            print(f"실패: {title} — {msg}")
            try:
                post(api, {"action": "result", "token": token, "key": k, "note": msg, "done": False})
            except Exception:  # noqa: BLE001
                pass
            summary.append(f"| {title} | 실패: {msg} |")
        time.sleep(1)

    if len(summary) == 4:
        summary.append("| (받을 공고 없음) | |")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        Path(os.environ["GITHUB_STEP_SUMMARY"]).write_text("\n".join(summary) + "\n", encoding="utf-8")
    print("\n".join(summary))


def test(urls):
    for u in urls:
        s = new_session()
        print(f"\n=== {u}")
        try:
            found = find_files(s, u)
        except Exception as e:  # noqa: BLE001
            print("  페이지 읽기 실패:", e)
            continue
        if not found:
            print("  첨부 못 찾음")
        for f in found:
            try:
                d = download(s, f, u)
                print(f"  OK  {d['name']}  ({d['mime']}, {len(d['bytes']):,}B)")
            except Exception as e:  # noqa: BLE001
                print(f"  ERR {f['name'] or f['url']}: {e}")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--test":
        test(sys.argv[2:])
    else:
        run()

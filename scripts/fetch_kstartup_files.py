"""K-Startup 공고 상세 페이지의 첨부파일을 plans/raw/ 에 받아 둔다 (예고 캘린더 근거 자료용).
사용: python scripts/fetch_kstartup_files.py 175783"""
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


def main(sn):
    url = f"https://www.k-startup.go.kr/web/contents/bizpbanc-ongoing.do?schM=view&pbancSn={sn}"
    s = requests.Session()
    s.headers["User-Agent"] = UA
    soup = BeautifulSoup(s.get(url, timeout=30).text, "html.parser")
    out = ROOT / "plans" / "raw" / str(sn)
    out.mkdir(parents=True, exist_ok=True)
    for a in soup.select("a[href*='/afile/fileDownload/']"):
        li = a.find_parent(["li", "div", "p"])
        name = re.sub(r"\s*(바로보기|다운로드)\s*", " ", li.get_text(" ") if li else "").strip()
        name = re.sub(r'[\\/:*?"<>|]', "_", name)[:120] or a["href"].rsplit("/", 1)[-1]
        r = s.get(urljoin(url, a["href"]), timeout=120)
        r.raise_for_status()
        (out / name).write_bytes(r.content)
        print("saved", name, len(r.content))


if __name__ == "__main__":
    for sn in sys.argv[1:]:
        main(sn)

"""ニュースの取得・重複判定・Discord 送信。外部のキーも AI も使わない（Google ニュースの RSS だけ）。"""

import datetime as dt
import email.utils
import html
import json
import os
import pathlib
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"
DOCS_DIR = ROOT / "docs"
USER_AGENT = "Mozilla/5.0 (miyuki-university; +https://github.com/CxElegy/MiyukiUniversity)"
TIMEOUT = 20
JA = ("ja", "JP", "JP:ja")  # Google ニュースの hl / gl / ceid


@dataclass
class Entry:
    title: str
    url: str
    published: dt.datetime | None
    source: str
    summary: str = ""


def _clean(text, limit=300):
    text = re.sub(r"<[^>]+>", " ", html.unescape(text or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _parse_date(text):
    if not text:
        return None
    try:
        d = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def google_news(query, lookback_days, lang=JA):
    """Google ニュース検索の RSS。失敗したら空リスト（ほかのテーマには影響させない）。"""
    hl, gl, ceid = lang
    q = urllib.parse.quote(f"{query} when:{lookback_days}d")
    try:
        root = ET.fromstring(_get(f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"))
    except Exception as e:
        print(f"  [警告] Google ニュース取得失敗（{query}）：{e}", file=sys.stderr)
        return []
    out = []
    for it in root.iter("item"):
        src = it.find("source")
        source = src.text if src is not None and src.text else "Google ニュース"
        title = _clean(it.findtext("title"), 200)
        if title.endswith(f" - {source}"):  # 見出しの末尾の「 - 媒体名」を外す
            title = title[: -len(source) - 3]
        url = (it.findtext("link") or "").strip()
        if title and url:
            out.append(Entry(title, url, _parse_date(it.findtext("pubDate")), source))
    return out


# ---------- 重複判定 ----------

def title_key(title):
    return "t:" + re.sub(r"\W+", "", title.lower())


# 見出しの文字バイグラムの重なりがこれを超えたら同じ記事（転載・見出しの末尾違い）とみなす
SIMILAR = 0.75


def title_grams(title):
    t = re.sub(r"\W+", "", title.rstrip(". …").lower())
    return {t[i:i + 2] for i in range(len(t) - 1)}


def similar(a, b):
    return len(a & b) / max(1, min(len(a), len(b))) > SIMILAR


# ---------- 状態ファイル ----------

def load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_json(path, data):
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")


# ---------- Discord ----------

def cut(text, n):
    return text if len(text) <= n else text[: n - 1] + "…"


def send(payload, url=None):
    """Discord Webhook に送る。URL がなければ内容を表示するだけ（ローカル確認用）。"""
    payload.setdefault("allowed_mentions", {"parse": []})
    url = url or os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        print("DISCORD_WEBHOOK_URL が未設定なので、送る内容を表示します：", file=sys.stderr)
        print(json.dumps(payload, ensure_ascii=False, indent=1))
        return False
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        # 失敗しても止めない（ページの更新は続ける）。送信済みにしないので次回また送る
        print(f"[警告] Discord 送信失敗：{e}", file=sys.stderr)
        return False

"""受験通信：美幸の大学受験ページ（docs/index.html）を作る。

ニュースは Google ニュース（日本語）から無料で集め、juken.yaml のテーマで絞り込み・重複を除く（AI は使わない）。
日程（共通テスト・国公立・私立・志望校……）は juken.yaml に手で書き、ページでは月カレンダーとガントチャートにする。
近づいた日程は remind_days に合わせて Discord でリマインド、urgent に当たったニュースはその日のうちに送る。

使い方：
  python scripts/juken.py            ニュース収集 → リマインド → ページ生成
  python scripts/juken.py collect    ニュース収集だけ
  python scripts/juken.py notify     リマインド・重要ニュースの送信だけ
  python scripts/juken.py render     ページの再生成だけ
  python scripts/juken.py --dry-run  集まるニュースを表示するだけ（ファイルも Discord も触らない）

Discord：環境変数 DISCORD_WEBHOOK_URL（GitHub の Secrets）に送る。
"""

import argparse
import calendar
import datetime as dt
import html
import re
import sys
import zoneinfo

import yaml

import sources

CONFIG_FILE = sources.ROOT / "juken.yaml"
NEWS_FILE = sources.STATE_DIR / "news.json"
NOTIFIED_FILE = sources.STATE_DIR / "notified.json"
PAGE_FILE = sources.DOCS_DIR / "index.html"
WEEK = "月火水木金土日"
NEWS_ON_PAGE = 5  # テーマごとに最初から見せる件数（残りは「もっと見る」に畳む）

# 日程の種類 → 色（カレンダーの印、ガントチャートのバー、リマインドの色帯）
KINDS = {
    "共通テスト": "#C8102E",
    "国公立": "#1F4E8C",
    "私立": "#2E7D4F",
    "推薦・総合型": "#7A3E9D",
    "英検・模試": "#D9822B",
    "手続き": "#5B6170",
    "志望校": "#D6336C",
}
DEFAULT_COLOR = "#5B6170"

e = html.escape


# ---------- 設定と日程 ----------

def load():
    cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    cfg.setdefault("settings", {})
    for key in ("schedule", "targets", "topics", "timetable", "checklist", "links", "changes", "recommend", "plan"):
        cfg[key] = cfg.get(key) or []
    return cfg


def _date(v):
    if isinstance(v, dt.date):
        return v
    return dt.date.fromisoformat(str(v))


def events(cfg):
    """公式日程と志望校の日程を {id, title, start, end, kind, color, school, ...} にそろえ、開始日順に並べる。"""
    out = []
    for i, ev in enumerate(cfg["schedule"]):
        out.append(_event(ev, f"s{i}", ev.get("kind", "手続き")))
    for t in cfg["targets"]:
        for i, ev in enumerate(t.get("dates") or []):
            x = _event(ev, f"{t['name']}#{i}", ev.get("kind", "志望校"))
            x["school"] = t["name"]
            x["title"] = f"{t['name']}　{ev['title']}"
            x["short"] = ev.get("short") or f"{t.get('short') or t['name']} {ev['title']}"
            x["url"] = x["url"] or t.get("url", "")
            out.append(x)
    out.sort(key=lambda x: (x["start"], x["end"], x["id"]))
    return out


def _event(ev, eid, kind):
    start = _date(ev["date"])
    end = _date(ev.get("end") or start)
    return {
        "id": str(ev.get("id") or eid),
        "title": ev["title"],
        "short": ev.get("short") or ev["title"],
        "start": start,
        "end": end,
        "kind": kind,
        "color": ev.get("color") or KINDS.get(kind, DEFAULT_COLOR),
        "important": bool(ev.get("important")),
        "remind": ev.get("remind", bool(ev.get("important"))),
        "tentative": bool(ev.get("tentative")),
        "note": ev.get("note", ""),
        "url": ev.get("source") or ev.get("url") or "",
        "school": "",
    }


def plan(cfg):
    """逆算プラン：本番から逆算した期間ごとの「やること」。{start, end, title, goal, todo, color} を日付順に。"""
    out = []
    for x in cfg.get("plan") or []:
        out.append({"start": _date(x["from"]), "end": _date(x["to"]), "title": x["title"],
                    "goal": x.get("goal", ""), "todo": x.get("todo") or [], "color": x.get("color", "#1A1C22")})
    return sorted(out, key=lambda x: x["start"])


def today(cfg):
    tz = zoneinfo.ZoneInfo(cfg["settings"].get("timezone", "Asia/Tokyo"))
    return dt.datetime.now(tz).date()


def fmt_day(d, year=False):
    s = f"{d.month}/{d.day}（{WEEK[d.weekday()]}）"
    return f"{d.year}/{s}" if year else s


def fmt_span(ev):
    if ev["start"] == ev["end"]:
        return fmt_day(ev["start"])
    return f"{fmt_day(ev['start'])}〜{fmt_day(ev['end'])}"


def days_label(n):
    if n == 0:
        return "今日"
    if n == 1:
        return "明日"
    return f"あと{n}日"


# ---------- ニュース収集 ----------

def topics(cfg):
    """テーマ = juken.yaml の topics ＋ 志望校ごとに1つ（検索語は省略すると「大学名 入試」）。"""
    out = [dict(t) for t in cfg["topics"]]
    for t in cfg["targets"]:
        out.append({
            "name": t["name"],
            "queries": t.get("keywords") or [f"{t['name']} 入試"],
            "match": t.get("match") or [t.get("short") or t["name"], t["name"]],
            "urgent": t.get("urgent") or [r"入試|選抜|出願|変更|日程|合格"],
            "exclude": t.get("exclude") or [],
            "school": True,
        })
    return out


def _matches(text, words):
    low = text.lower()
    return any(w.lower() in low for w in words)


def collect(cfg, now=None, dry_run=False, fetch=None):
    """テーマごとに Google ニュースを取り、絞り込み・重複除去して state/news.json に足す（keep_days 日分残す）。"""
    s = cfg["settings"]
    now = now or dt.datetime.now(dt.timezone.utc)
    lookback = s.get("lookback_days", 7)
    keep = s.get("keep_days", 120)
    exclude = s.get("exclude") or []
    fetch = fetch or (lambda q: sources.google_news(q, lookback))

    store = sources.load_json(NEWS_FILE)
    news = store.get("news", {})
    seen_titles = {sources.title_key(x["title"]) for x in news.values()}
    grams = [sources.title_grams(x["title"]) for x in news.values()]
    added = []
    for t in topics(cfg):
        got = 0
        for q in t.get("queries") or []:
            for en in fetch(q):
                text = f"{en.title} {en.summary}"
                if not en.published or now - en.published > dt.timedelta(days=lookback):
                    continue
                if t.get("match") and not _matches(text, t["match"]):
                    continue
                if any(re.search(p, en.title) for p in exclude + (t.get("exclude") or [])):
                    continue
                key = sources.title_key(en.title)
                g = sources.title_grams(en.title)
                if en.url in news or key in seen_titles or any(sources.similar(g, x) for x in grams):
                    continue
                seen_titles.add(key)
                grams.append(g)
                urgent = any(re.search(p, en.title) for p in t.get("urgent") or [])
                news[en.url] = {
                    "title": en.title, "url": en.url, "source": en.source, "topic": t["name"],
                    "published": en.published.isoformat(), "urgent": urgent,
                }
                added.append(news[en.url])
                got += 1
        print(f"  {t['name']}：新着 {got} 件")

    cutoff = now - dt.timedelta(days=keep)
    news = {u: x for u, x in news.items() if dt.datetime.fromisoformat(x["published"]) >= cutoff}
    if dry_run:
        for x in added:
            print(f"  [{x['topic']}] {'★' if x['urgent'] else ' '} {x['title']} — {x['source']}")
    else:
        sources.save_json(NEWS_FILE, {"news": news})
    return added


# ---------- Discord ----------

def due_reminders(cfg, evs, day):
    """今日リマインドする日程：開始日まであと remind_days のどれかちょうどの日数。[(key, 日数, 日程)] を返す。"""
    steps = cfg["settings"].get("remind_days", [30, 14, 7, 3, 1, 0])
    out = []
    for ev in evs:
        if not ev["remind"]:
            continue
        n = (ev["start"] - day).days
        if n in steps:
            out.append((f"{ev['id']}|{ev['start']}|{n}", n, ev))
    return out


def run_notify(cfg, now=None):
    """日程のリマインドと重要ニュースを送る。送ったものは state/notified.json に記録。
    初回（このファイルがない）は記録だけして送らない（動かし始めた瞬間に大量に届かないように）。"""
    s = cfg["settings"]
    day = now.date() if now else today(cfg)
    first = not NOTIFIED_FILE.exists()
    sent = sources.load_json(NOTIFIED_FILE)
    done_rem = set(sent.get("reminders", []))
    done_news = set(sent.get("news", []))
    page = s.get("page_url", "")

    rem = [r for r in due_reminders(cfg, events(cfg), day) if r[0] not in done_rem]
    news = sources.load_json(NEWS_FILE).get("news", {})
    hot = [x for x in news.values() if x.get("urgent") and x["url"] not in done_news]
    hot.sort(key=lambda x: x["published"])

    if not first:
        for key, n, ev in rem:
            who = s.get("name", "")
            head = f"⏰ {ev['title']}　{days_label(n)}" if n else f"📣 今日は {ev['title']}"
            desc = [fmt_span(ev)]
            if ev["note"]:
                desc.append(ev["note"])
            if who and n == 0 and ev["important"]:
                desc.append(f"{who}、がんばれ！🌸")
            payload = {"embeds": [{
                "title": sources.cut(head, 250), "description": "\n".join(desc),
                "color": int(ev["color"].lstrip("#"), 16), "url": ev["url"] or page or None,
            }]}
            if _send(payload):
                done_rem.add(key)
        if hot:
            lines = [f"**[{x['topic']}]** [{sources.cut(x['title'], 90)}]({x['url']}) — {x['source']}" for x in hot[:10]]
            if _send({"embeds": [{"title": "📰 受験ニュース", "description": "\n".join(lines),
                                  "color": KINDS["共通テスト"], "url": page or None}]}):
                done_news.update(x["url"] for x in hot)
    else:
        done_rem.update(r[0] for r in rem)
        done_news.update(x["url"] for x in hot)

    keep = {u for u in news}
    sources.save_json(NOTIFIED_FILE, {"reminders": sorted(done_rem)[-500:],
                                     "news": sorted(done_news & keep)})


def _send(payload):
    for emb in payload.get("embeds", []):
        if not emb.get("url"):
            emb.pop("url", None)
    return sources.send(payload)


# ---------- ページ ----------

def _months(start, end):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def _chip(ev, cls="ev"):
    tip = f"{ev['title']}　{fmt_span(ev)}" + (f"　{ev['note']}" if ev["note"] else "")
    return (f'<span class="{cls}{" imp" if ev["important"] else ""}" style="--c:{ev["color"]}" title="{e(tip)}">'
            f'{e(ev["short"])}</span>')


def render_calendar(evs, day, start, end):
    """月カレンダー：月ごとに7列の表、日程はマスの中に。最初は今月を表示し、前後にめくれる。"""
    months = []
    cur = None
    for y, m in _months(start, end):
        cells = []
        first = dt.date(y, m, 1)
        for _ in range(first.weekday()):
            cells.append('<div class="cd pad"></div>')
        for d in range(1, calendar.monthrange(y, m)[1] + 1):
            date = dt.date(y, m, d)
            todays = [ev for ev in evs if ev["start"] <= date <= ev["end"]]
            cls = ["cd"]
            if date == day:
                cls.append("today")
            if date < day:
                cls.append("past")
            if date.weekday() >= 5:
                cls.append("we" if date.weekday() == 5 else "su")
            chips = "".join(_chip(ev) for ev in todays[:4])
            more = f'<span class="more">+{len(todays) - 4}</span>' if len(todays) > 4 else ""
            cells.append(f'<div class="{" ".join(cls)}"><b>{d}</b>{chips}{more}</div>')
        key = f"{y}-{m:02d}"
        if (y, m) == (day.year, day.month):
            cur = key
        head = "".join(f'<div class="wd{" su" if i == 6 else " we" if i == 5 else ""}">{w}</div>'
                       for i, w in enumerate(WEEK))
        months.append(f'<div class="month" data-m="{key}"><h3><span>{y}</span>{m}月</h3>'
                      f'<div class="cgrid">{head}{"".join(cells)}</div></div>')
    cur = cur or (f"{start.year}-{start.month:02d}" if day < start else f"{end.year}-{end.month:02d}")
    tabs = "".join(f'<button data-m="{y}-{m:02d}">{m}月</button>' for y, m in _months(start, end))
    return (f'<div class="cal" data-cur="{cur}"><div class="ctabs">'
            f'<button class="prev" aria-label="前の月">‹</button>{tabs}'
            f'<button class="next" aria-label="次の月">›</button></div>{"".join(months)}</div>')


def render_gantt(evs, day, start, end, phases=()):
    """ガントチャート：横軸は受験シーズン全体（start〜end）、1行1日程で種類ごとにまとめる。縦の赤線が今日。"""
    total = (end - start).days + 1

    def pos(d):
        return max(0.0, min(100.0, (d - start).days / total * 100))

    heads = []
    for y, m in _months(start, end):
        a = max(dt.date(y, m, 1), start)
        b = min(dt.date(y, m, calendar.monthrange(y, m)[1]), end)
        heads.append(f'<div class="gm" style="left:{pos(a):.3f}%;width:{pos(b + dt.timedelta(1)) - pos(a):.3f}%">'
                     f'{m}月</div>')
    grid = "".join(f'<i style="left:{pos(dt.date(y, m, 1)):.3f}%"></i>' for y, m in _months(start, end))
    now_line = (f'<em class="now" style="left:{pos(day):.3f}%"><span>今日</span></em>'
                if start <= day <= end else "")

    rows = []
    if phases:
        rows.append('<div class="gk" style="--c:#1A1C22">逆算プラン</div>')
        for ph in phases:
            left = pos(ph["start"])
            width = pos(ph["end"] + dt.timedelta(1)) - left
            span = f"{fmt_day(ph['start'])}〜{fmt_day(ph['end'])}"
            now = " nowph" if ph["start"] <= day <= ph["end"] else ""
            rows.append(
                f'<div class="gr{" past" if ph["end"] < day else ""}{now}"><div class="gl" title="{e(ph["goal"])}">'
                f'{e(ph["title"])}<small>{span}</small></div><div class="gt">{grid}'
                f'<span class="bar ph" title="{e(ph["goal"])}" style="--c:{ph["color"]};left:{left:.3f}%;'
                f'width:{width:.3f}%"></span></div></div>')
    order = list(KINDS) + sorted({ev["kind"] for ev in evs} - set(KINDS))
    for kind in order:
        group = [ev for ev in evs if ev["kind"] == kind and ev["end"] >= start and ev["start"] <= end]
        if not group:
            continue
        rows.append(f'<div class="gk" style="--c:{KINDS.get(kind, DEFAULT_COLOR)}">{e(kind)}</div>')
        for ev in group:
            left = pos(ev["start"])
            width = pos(ev["end"] + dt.timedelta(1)) - left
            past = " past" if ev["end"] < day else ""
            dot = " dot" if ev["start"] == ev["end"] else ""
            tip = f"{ev['title']}　{fmt_span(ev)}" + (f"　{ev['note']}" if ev["note"] else "")
            rows.append(
                f'<div class="gr{past}"><div class="gl" title="{e(ev["title"])}">{e(ev["short"])}'
                f'<small>{fmt_span(ev)}</small></div><div class="gt">{grid}'
                f'<span class="bar{dot}{" imp" if ev["important"] else ""}" title="{e(tip)}" '
                f'style="--c:{ev["color"]};left:{left:.3f}%;width:{width:.3f}%"></span></div></div>')
    return (f'<div class="gwrap"><div class="gantt"><div class="gr ghead"><div class="gl"></div>'
            f'<div class="gt">{"".join(heads)}{now_line}</div></div>{"".join(rows)}'
            f'<div class="gr gfoot"><div class="gl"></div><div class="gt">{now_line}</div></div></div></div>')


def render_now(phases, day):
    """いまの期間の「やること」を一番上に出す（逆算プランのうち今日を含む期間）。"""
    cur = next((ph for ph in phases if ph["start"] <= day <= ph["end"]), None)
    if not cur:
        return ""
    left = (cur["end"] - day).days
    todo = "".join(f'<li><label><input type="checkbox" data-k="now-{e(cur["title"])}-{e(x)}"><span>{e(x)}</span></label></li>'
                   for x in cur["todo"])
    return (f'<div class="nowbox" style="--c:{cur["color"]}"><div class="nh">'
            f'<b>{e(cur["title"])}</b><span>{fmt_day(cur["start"])}〜{fmt_day(cur["end"])}・この期間あと{left + 1}日</span></div>'
            f'<div class="nb">{"<p>🎯 " + e(cur["goal"]) + "</p>" if cur["goal"] else ""}<ul class="ckl">{todo}</ul></div></div>')


def render_plan(phases, day):
    """逆算プラン：期間ごとのカード。今の期間と次の期間だけ開いて見せ、済んだ期間・その先の期間は畳む。"""
    done, shown, later = [], [], []
    for ph in phases:
        state = "done" if ph["end"] < day else "cur" if ph["start"] <= day else "todo"
        badge = {"done": "済", "cur": "いまここ", "todo": f"あと{(ph['start'] - day).days}日で開始"}[state]
        todo = "".join(f"<li>{e(x)}</li>" for x in ph["todo"])
        if state == "cur":  # 今の期間のやることはページ上部の「いまやること」と同じなので繰り返さない
            todo = '<li class="see"><a href="#now">↑ ページ上部の「いまやること」でチェック</a></li>'
        card = (f'<div class="pl {state}" style="--c:{ph["color"]}"><div class="pd">'
                f'<b>{fmt_day(ph["start"])}</b><span>〜{fmt_day(ph["end"])}</span><i>{badge}</i></div>'
                f'<div class="pc"><h3>{e(ph["title"])}</h3>{"<p>🎯 " + e(ph["goal"]) + "</p>" if ph["goal"] else ""}'
                f'<ul>{todo}</ul></div></div>')
        (done if state == "done" else shown if len(shown) < 2 else later).append(card)
    if not (done or shown or later):
        return ""
    fold = lambda cards, label: (f'<details class="plf"><summary>{label}（{len(cards)}）</summary>{"".join(cards)}</details>'
                                 if cards else "")
    return (f'<div class="pls">{fold(done, "終わった期間")}{"".join(shown)}'
            f'{fold(later, "その先の期間")}</div>')


def render_weekly(cfg, evs, day):
    """今週のまとめ：今週（月〜日）の締切は日程から自動で、「新しく分かったこと」「気になること」は
    juken.yaml の weekly（seiri スキルが毎週書く）から。weekly が先週より古ければ締切だけ出す。"""
    mon = day - dt.timedelta(days=day.weekday())
    sun = mon + dt.timedelta(days=6)
    due = [ev for ev in evs if mon <= ev["end"] <= sun or mon <= ev["start"] <= sun]
    w = cfg.get("weekly") or {}
    fresh = w.get("week") and 0 <= (mon - _date(w["week"])).days < 7

    def item(x):
        text = e(x["text"] if isinstance(x, dict) else x)
        url = x.get("source") if isinstance(x, dict) else ""
        link = f' <a href="{e(url)}" target="_blank" rel="noopener">出典</a>' if url else ""
        return f"<li>{text}{link}</li>"

    cols = []
    rows = "".join(f'<li><b>{fmt_span(ev) if ev["start"] >= mon else "〜" + fmt_day(ev["end"])}</b>'
                   f'{e(ev["title"])}{"<small>" + e(ev["note"]) + "</small>" if ev["note"] else ""}</li>' for ev in due)
    cols.append(("📌 今週の締切・予定", rows or "<li>今週の締切はありません</li>"))
    if fresh:
        if w.get("learned"):
            cols.append(("💡 新しく分かったこと", "".join(item(x) for x in w["learned"])))
        if w.get("concerns"):
            cols.append(("⚠️ 気になること", "".join(item(x) for x in w["concerns"])))
    body = "".join(f'<div class="wk"><h3>{h}</h3><ul>{li}</ul></div>' for h, li in cols)
    return (f'<p class="hint">{fmt_day(mon)}〜{fmt_day(sun)}</p><div class="wks">{body}</div>')


def render_upcoming(evs, day, limit=5):
    rest = [ev for ev in evs if ev["end"] >= day]
    soon = rest[:limit]
    if not soon:
        return '<p class="empty">今シーズンの日程はすべて終わりました。おつかれさま！</p>'
    out = []
    for ev in soon:
        n = (ev["start"] - day).days
        label = "期間中" if n < 0 else days_label(n)
        hot = " hot" if 0 <= n <= 7 or n < 0 else ""
        link = f' href="{e(ev["url"])}" target="_blank" rel="noopener"' if ev["url"] else ""
        tag = "a" if ev["url"] else "div"
        tent = '<i class="tent">予定</i>' if ev["tentative"] else ""
        out.append(f'<{tag} class="up{hot}" style="--c:{ev["color"]}"{link}><span class="n">{label}</span>'
                   f'<span class="t"><b>{e(ev["title"])}{tent}</b><small>{fmt_span(ev)}'
                   f'{"　" + e(ev["note"]) if ev["note"] else ""}</small></span></{tag}>')
    if len(rest) > limit:
        out.append(f'<a class="up more" href="#cal" data-go="sched">ほか {len(rest) - limit} 件 → カレンダー・ガントチャートで見る</a>')
    return "".join(out)


def render_targets(cfg, day):
    if not cfg["targets"]:
        return ('<div class="tg empty"><b>志望校はまだ登録されていません</b>'
                '<p>juken.yaml の <code>targets</code> に大学名・学部・日程を書くと、ここにカードが出て、'
                '月カレンダー・ガントチャート・リマインダーにも自動で入ります。'
                'Claude に「志望校に〇〇大学〇〇学部を追加して」と頼んでも OK。</p></div>')
    out = []
    for t in cfg["targets"]:
        dates = sorted((_event(ev, "", ev.get("kind", "志望校")) for ev in t.get("dates") or []),
                       key=lambda x: x["start"])
        nxt = next((ev for ev in dates if ev["end"] >= day), None)
        rows = "".join(f'<li class="{"past" if ev["end"] < day else ""}"><span>{fmt_span(ev)}</span>'
                       f'{e(ev["title"])}</li>' for ev in dates)
        meta = " · ".join(e(str(t[k])) for k in ("faculty", "type", "method") if t.get(k))
        head = f'<a href="{e(t["url"])}" target="_blank" rel="noopener">{e(t["name"])}</a>' if t.get("url") else e(t["name"])
        badge = f'<span class="nx">次：{e(nxt["title"])} {days_label(max(0, (nxt["start"] - day).days))}</span>' if nxt else ""
        note = f'<p>{e(t["note"])}</p>' if t.get("note") else ""
        out.append(f'<div class="tg"><h3>{head}</h3><div class="meta">{meta}</div>{badge}<ul>{rows}</ul>{note}</div>')
    return "".join(out)


def render_recommend(cfg):
    """おすすめ学部：系統 → 理由・共テ / 二次の科目 → 具体例（難易度ラベル：チャレンジ / 実力相応 / 安全寄り）。"""
    out = []
    for r in cfg.get("recommend") or []:
        ex = []
        for x in r.get("examples") or []:
            name = (f'<a href="{e(x["url"])}" target="_blank" rel="noopener">{e(x["name"])}</a>'
                    if x.get("url") else e(x["name"]))
            lv = f'<i class="lv lv-{e(x["level"])}">{e(x["level"])}</i>' if x.get("level") else ""
            meta = " · ".join(e(str(x[k])) for k in ("schedule", "border") if x.get(k))
            ex.append(f'<li>{lv}<b>{name}</b><small>{meta}</small>'
                      f'{"<span>" + e(x["note"]) + "</span>" if x.get("note") else ""}</li>')
        subj = "".join(f'<dt>{k}</dt><dd>{e(r[f])}</dd>' for k, f in (("共テ", "kyotsu"), ("二次", "niji")) if r.get(f))
        out.append(f'<div class="rc" style="--c:{r.get("color", "#1F4E8C")}"><h3>{e(r["field"])}</h3>'
                   f'<p>{e(r.get("why", ""))}</p>{"<dl>" + subj + "</dl>" if subj else ""}'
                   f'{"<ul>" + "".join(ex) + "</ul>" if ex else ""}</div>')
    note = cfg["settings"].get("recommend_note", "")
    tips = "".join(f'<li>{e(x)}</li>' for x in cfg.get("recommend_tips") or [])
    return ((f'<p class="hint">{e(note)}</p>' if note else "") + f'<div class="rcs">{"".join(out)}</div>'
            + (f'<div class="tips"><h3>出願の組み方</h3><ol>{tips}</ol></div>' if tips else "")) if out else ""


def render_compare(cfg):
    """押さえ候補（MARCH など）の比較表：一般の試験日・共テ利用・ボーダー・手続締切・延納。"""
    rows = []
    for x in cfg.get("compare") or []:
        name = (f'<a href="{e(x["url"])}" target="_blank" rel="noopener">{e(x["name"])}</a>'
                if x.get("url") else e(x["name"]))
        ennou = {True: '<i class="ok">できる</i>', False: '<i class="ng">できない</i>'}.get(
            x.get("ennou"), '<i class="uk">不明</i>')
        rows.append(f'<tr><th>{name}<small>{e(x.get("faculty", ""))}</small></th>'
                    f'<td>{"<br>".join(e(t) for t in x.get("exams") or [])}</td>'
                    f'<td>{e(x.get("kyotesu", ""))}</td><td>{e(x.get("border", ""))}</td>'
                    f'<td>{e(x.get("deadline", ""))}</td><td>{ennou}<small>{e(x.get("ennou_note", ""))}</small></td></tr>')
    if not rows:
        return ""
    note = cfg["settings"].get("compare_note", "")
    return ((f'<p class="hint">{e(note)}</p>' if note else "") +
            '<div class="cmpw"><table class="cmp"><tr><th>大学・学部</th><th>一般選抜の試験日</th><th>共テ利用</th>'
            f'<th>ボーダー</th><th>入学手続（1次）</th><th>延納<small>国公立の発表まで待てる？</small></th></tr>{"".join(rows)}</table></div>')


def render_roles(cfg, day):
    """役割分担のレーン図：行＝人（美幸・家族・先生・大学）、列＝時期。今の時期の列を強調し、済んだ列は薄く。"""
    r = cfg.get("roles") or {}
    lanes, steps = r.get("lanes") or [], r.get("steps") or []
    if not (lanes and steps):
        return ""
    head = ""
    for st in steps:
        a, b = _date(st["from"]), _date(st["to"])
        state = "done" if b < day else "cur" if a <= day else ""
        mark = '<i>いまここ</i>' if state == "cur" else ""
        head += f'<th class="{state}">{mark}<b>{e(st["label"])}</b><small>{a.month}/{a.day}〜{b.month}/{b.day}</small></th>'
    rows = ""
    for ln in lanes:
        cells = ""
        for st in steps:
            a, b = _date(st["from"]), _date(st["to"])
            state = "done" if b < day else "cur" if a <= day else ""
            items = "".join(f"<li>{e(x)}</li>" for x in st.get(ln["key"]) or [])
            cells += f'<td class="{state}"><ul>{items}</ul></td>'
        rows += (f'<tr style="--c:{ln.get("color", "#1A1C22")}"><th class="who"><span>{e(ln.get("icon", ""))}</span>'
                 f'<b>{e(ln["name"])}</b><small>{e(ln.get("role", ""))}</small></th>{cells}</tr>')
    note = r.get("note", "")
    return ((f'<p class="hint">{e(note)}</p>' if note else "")
            + f'<div class="rlw"><table class="rl"><tr><th class="who"></th>{head}</tr>{rows}</table></div>')


def render_flow(cfg):
    """出願の流れ：ルート（国公立・私大一般・共テ利用・合格後）ごとに、いつ・何をするかを順番に。"""
    out = []
    for r in cfg.get("flow") or []:
        steps = "".join(
            f'<li><span class="fw">{e(x.get("when", ""))}</span><b>{e(x["what"])}</b>'
            f'{"<small>" + e(x["detail"]) + "</small>" if x.get("detail") else ""}</li>' for x in r.get("steps") or [])
        out.append(f'<div class="fl" style="--c:{r.get("color", "#1A1C22")}"><h3>{e(r["title"])}</h3>'
                   f'{"<p>" + e(r["lead"]) + "</p>" if r.get("lead") else ""}<ol>{steps}</ol></div>')
    common = "".join(f"<li>{e(x)}</li>" for x in cfg.get("flow_common") or [])
    if not out:
        return ""
    return (f'<div class="fls">{"".join(out)}</div>'
            + (f'<div class="tips"><h3>どのルートでも用意するもの</h3><ul>{common}</ul></div>' if common else ""))


def render_news(cfg, day):
    news = list(sources.load_json(NEWS_FILE).get("news", {}).values())
    tz = zoneinfo.ZoneInfo(cfg["settings"].get("timezone", "Asia/Tokyo"))
    out = []
    for t in topics(cfg):
        items = sorted((x for x in news if x["topic"] == t["name"]), key=lambda x: x["published"], reverse=True)
        if not items:
            continue

        def li(x):
            d = dt.datetime.fromisoformat(x["published"]).astimezone(tz).date()
            new = '<i class="new">NEW</i>' if (day - d).days <= 1 else ""
            star = '<i class="hotn">重要</i>' if x.get("urgent") else ""
            return (f'<li><a href="{e(x["url"])}" target="_blank" rel="noopener">{new}{star}{e(x["title"])}</a>'
                    f'<small>{d.month}/{d.day} · {e(x["source"])}</small></li>')

        more = ""
        if len(items) > NEWS_ON_PAGE:
            more = (f'<details><summary>もっと見る（{len(items) - NEWS_ON_PAGE}件）</summary>'
                    f'<ul>{"".join(li(x) for x in items[NEWS_ON_PAGE:])}</ul></details>')
        out.append(f'<div class="nt"><h3>{e(t["name"])}</h3>'
                   f'<ul>{"".join(li(x) for x in items[:NEWS_ON_PAGE])}</ul>{more}</div>')
    return "".join(out) or '<p class="empty">まだニュースを集めていません（2時間ごとに自動で集めます）。</p>'


def render_timetable(cfg):
    out = []
    for d in cfg["timetable"]:
        rows = "".join(f'<tr><th>{e(r["subject"])}</th><td class="mono">{e(r["time"])}</td>'
                       f'<td>{e(r.get("note", ""))}</td></tr>' for r in d.get("rows") or [])
        out.append(f'<div class="tt"><h3>{e(d["day"])}</h3><table>{rows}</table></div>')
    return "".join(out)


def render_checklist(cfg):
    out = []
    for gi, g in enumerate(cfg["checklist"]):
        lis = "".join(
            f'<li><label><input type="checkbox" data-k="{gi}-{i}-{e(x)}"><span>{e(x)}</span></label></li>'
            for i, x in enumerate(g.get("items") or []))
        out.append(f'<div class="ck"><h3>{e(g["group"])}</h3><ul>{lis}</ul></div>')
    return "".join(out)


def render_changes(cfg):
    out = []
    for c in cfg["changes"]:
        src = f' <a href="{e(c["source"])}" target="_blank" rel="noopener">出典</a>' if c.get("source") else ""
        out.append(f'<li><b>{e(c["title"])}</b><p>{e(c.get("body", ""))}{src}</p></li>')
    return f'<ul class="chg">{"".join(out)}</ul>' if out else ""


def render_links(cfg):
    return "".join(f'<a class="lk" href="{e(x["url"])}" target="_blank" rel="noopener"><b>{e(x["name"])}</b>'
                   f'<small>{e(x.get("note", ""))}</small></a>' for x in cfg["links"])


def countdowns(cfg, evs, day):
    out = []
    for cid in cfg["settings"].get("countdowns") or []:
        ev = next((x for x in evs if x["id"] == cid), None)
        if not ev or ev["end"] < day:
            continue
        n = (ev["start"] - day).days
        num = "本番" if n <= 0 else str(n)
        unit = "" if n <= 0 else "日"
        out.append(f'<div class="cnt" style="--c:{ev["color"]}"><small>{e(ev["short"])}まで</small>'
                   f'<b>{num}<span>{unit}</span></b><small>{fmt_day(ev["start"])}</small></div>')
    return "".join(out)


def render_page(cfg, day, updated):
    s = cfg["settings"]
    evs = events(cfg)
    phases = plan(cfg)
    start, end = _date(s.get("season_start", "2026-10-01")), _date(s.get("season_end", "2027-03-31"))
    legend = "".join(f'<span style="--c:{c}">{k}</span>' for k, c in KINDS.items()
                     if any(ev["kind"] == k for ev in evs))
    title = s.get("title", "受験通信")
    who = s.get("name", "")
    top = [
        ("now", "いまやること", render_now(phases, day)),
        ("week", "今週のまとめ", render_weekly(cfg, evs, day)),
        ("next", "次の締切", f'<div class="ups">{render_upcoming(evs, day)}</div>'),
    ]
    # タブ：(id, ボタン名, ひとこと, [(section id, 見出し, 中身)])
    tabs = [
        ("sched", "📅 スケジュール", "逆算プラン・カレンダー・ガントチャート", [
            ("plan", "逆算プラン", render_plan(phases, day)),
            ("cal", "月カレンダー", f'<div class="legend">{legend}</div>{render_calendar(evs, day, start, end)}'),
            ("gantt", "ガントチャート", f'<div class="legend">{legend}</div>{render_gantt(evs, day, start, end, phases)}'),
        ]),
        ("univ", "🎓 大学えらび", "志望校・おすすめ学部", [
            ("targets", "志望校・併願校", f'<div class="tgs">{render_targets(cfg, day)}</div>'),
            ("compare", "MARCH（押さえ候補）", render_compare(cfg)),
            ("recommend", "おすすめ学部（理系 × 地方国公立）", render_recommend(cfg)),
        ]),
        ("info", "📰 ニュース", "受験ニュース・今年の変更点", [
            ("changes", "今年の変更点（これだけは知っておく）", render_changes(cfg)),
            ("news", "受験ニュース", f'<div class="nts">{render_news(cfg, day)}</div>'),
        ]),
        ("prep", "✏️ 出願・準備", "役割分担・出願の流れ・時間割", [
            ("roles", "だれが・いつまでに・なにをする（役割分担）", render_roles(cfg, day)),
            ("flow", "出願の流れ（どうやって出願するの？）", render_flow(cfg)),
            ("tt", "共通テストの時間割", f'<div class="tts">{render_timetable(cfg)}</div>'),
            ("check", "やることリスト", f'<p class="hint">チェックはこの端末のブラウザに保存されます。</p>'
                                      f'<div class="cks">{render_checklist(cfg)}</div>'),
            ("links", "公式サイト・リンク", f'<div class="lks">{render_links(cfg)}</div>'),
        ]),
    ]

    def sec(sid, name, inner):
        return f'<section id="{sid}"><h2>{name}</h2>{inner}</section>' if inner else ""

    body = "".join(sec(*x) for x in top)
    nav = "".join(f'<button data-tab="{tid}"><b>{label}</b><small>{e(hint)}</small></button>'
                  for tid, label, hint, _ in tabs)
    body += (f'<nav class="tabs" id="tabs">{nav}</nav>' + "".join(
        f'<div class="panel" id="tab-{tid}" data-tab="{tid}"><h2 class="ptitle">{label}</h2>'
        f'{"".join(sec(*x) for x in parts)}</div>' for tid, label, _, parts in tabs))
    return PAGE.format(
        title=e(f"{who}の{title}" if who else title), year=e(str(s.get("year_label", ""))),
        heading=e(title), who=e(who), updated=e(updated), today=fmt_day(day, year=True),
        countdowns=countdowns(cfg, evs, day), body=body, css=CSS, js=JS,
    )


def write_page(cfg, now=None):
    tz = zoneinfo.ZoneInfo(cfg["settings"].get("timezone", "Asia/Tokyo"))
    now = now or dt.datetime.now(tz)
    PAGE_FILE.write_text(render_page(cfg, now.date(), now.strftime("%Y-%m-%d %H:%M")), encoding="utf-8")
    print(f"生成しました：{PAGE_FILE.relative_to(sources.ROOT)}")


PAGE = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@500;700;900&family=Noto+Serif+JP:wght@500;700;900&family=JetBrains+Mono:wght@500;700&display=swap">
<style>{css}</style>
</head>
<body>
<div class="page">
<header class="mast"><div>
 <div class="logo">🌸</div>
 <div class="h"><small>{year}</small><h1>{heading}</h1><span class="for">{who}の大学受験</span></div>
 <div class="meta"><span class="mono">TODAY</span><b>{today}</b><span class="mono">更新 {updated}</span></div>
</div></header>
<div class="cnts">{countdowns}</div>
{body}
<footer class="mono">ニュースは Google ニュースから2時間ごとに自動収集 · 日程は juken.yaml（公式発表をもとに手入力。必ず各大学の募集要項で確認してください）</footer>
</div>
<script>{js}</script>
</body>
</html>
"""

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#EDEBE6;color:#1A1C22;font-family:'Noto Serif JP',serif}
a{color:inherit}
.mono,small.mono{font-family:'JetBrains Mono',monospace;font-size:12px;letter-spacing:.08em}
.page{max-width:1200px;margin:0 auto;padding:32px 16px 48px;display:flex;flex-direction:column;gap:24px}
.mast{border:3px solid #1A1C22;padding:5px;background:#FBF8F1;box-shadow:6px 6px 0 #1A1C22}
.mast>div{border:1px solid #1A1C22;display:flex;flex-wrap:wrap}
.mast .logo{width:96px;background:#1A1C22;display:flex;align-items:center;justify-content:center;font-size:44px}
.mast .h{flex:1;padding:14px 24px;display:flex;flex-direction:column;gap:4px;min-width:220px}
.mast .h small{font:700 12px 'JetBrains Mono',monospace;letter-spacing:.3em;color:#C8102E}
.mast h1{margin:0;font:900 clamp(36px,7vw,60px)/1 'Noto Sans JP',sans-serif;letter-spacing:.06em}
.mast .for{font-size:15px;letter-spacing:.2em}
.mast .meta{width:210px;border-left:1px solid #1A1C22;display:flex;flex-direction:column;justify-content:center;align-items:center;gap:4px;padding:10px}
.mast .meta b{font:900 22px 'Noto Sans JP',sans-serif}
.cnts{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px}
.cnt{background:#FBF8F1;border:3px solid #1A1C22;border-top:10px solid var(--c);padding:14px 18px;display:flex;flex-direction:column;gap:4px}
.cnt small{font:700 13px 'Noto Sans JP',sans-serif;letter-spacing:.1em}
.cnt b{font:900 64px/1 'Noto Sans JP',sans-serif;color:var(--c)}
.cnt b span{font-size:22px;margin-left:4px;color:#1A1C22}
.tabs{position:sticky;top:0;z-index:5;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:0;
 border:3px solid #1A1C22;background:#1A1C22;margin-top:8px;box-shadow:0 6px 0 #EDEBE6}
.tabs button{appearance:none;border:none;border-right:2px solid #1A1C22;background:#FBF8F1;color:#1A1C22;cursor:pointer;
 padding:10px 8px;display:flex;flex-direction:column;align-items:center;gap:2px;font-family:inherit}
.tabs button:last-child{border-right:none}
.tabs button b{font:900 16px 'Noto Sans JP',sans-serif;letter-spacing:.05em}
.tabs button small{font-size:11px;opacity:.7}
.tabs button.on{background:#C8102E;color:#fff}
.tabs button.on small{opacity:.9}
.panel{display:flex;flex-direction:column;gap:24px}
.ptitle{display:none}
.js-tabs .panel{display:none}.js-tabs .panel.on{display:flex}
html:not(.js-tabs) .ptitle{display:block;margin:12px 0 -8px;font:900 24px 'Noto Sans JP',sans-serif}
.up.more{justify-content:center;font:700 13px 'Noto Sans JP',sans-serif;border-left-width:2px;background:#F4F1EA}
.plf{margin:4px 0 8px 18px}
.plf summary{font:700 13px 'Noto Sans JP',sans-serif;padding:6px 0;opacity:.75}
section{background:#FBF8F1;border:3px solid #1A1C22;padding:0 0 20px;scroll-margin-top:12px}
section>h2{margin:0 0 18px;padding:10px 20px;background:#1A1C22;color:#FBF8F1;font:900 20px 'Noto Sans JP',sans-serif;letter-spacing:.25em}
section>*:not(h2){margin-left:20px;margin-right:20px}
.empty{opacity:.7}
.legend{display:flex;flex-wrap:wrap;gap:6px 14px;margin-bottom:12px;font:700 12px 'Noto Sans JP',sans-serif}
.legend span::before{content:"";display:inline-block;width:12px;height:12px;background:var(--c);margin-right:5px;vertical-align:-1px}
/* 次の予定 */
.ups{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:10px}
.up{display:flex;gap:14px;align-items:center;padding:10px 14px;border:2px solid #1A1C22;border-left:10px solid var(--c);text-decoration:none;background:#fff}
.up .n{flex:0 0 88px;font:900 18px 'Noto Sans JP',sans-serif;color:var(--c);text-align:center}
.up.hot{background:color-mix(in srgb,var(--c) 10%,#fff)}
.up .t{display:flex;flex-direction:column;gap:2px;min-width:0}
.up b{font:700 15px/1.4 'Noto Sans JP',sans-serif}
.up small{font-size:12.5px;line-height:1.5;opacity:.8}
.tent{display:inline-block;font:700 10px/1.4 'Noto Sans JP',sans-serif;font-style:normal;border:1px solid currentColor;padding:0 4px;margin-left:6px;opacity:.6;vertical-align:2px}
a.up:hover b{text-decoration:underline}
/* いまやること・逆算プラン */
.nowbox{display:flex;flex-wrap:wrap;border:3px solid var(--c);background:#fff}
.nowbox .nh{flex:0 0 260px;background:var(--c);color:#fff;padding:14px 18px;display:flex;flex-direction:column;gap:4px}
.nowbox .nh small{font:700 12px 'JetBrains Mono','Noto Sans JP',monospace;letter-spacing:.2em;opacity:.85}
.nowbox .nh b{font:900 22px/1.3 'Noto Sans JP',sans-serif}
.nowbox .nh span{font-size:13px}
.nowbox .nb{flex:1;min-width:260px;padding:12px 18px}
.nowbox .nb p{margin:0 0 6px;font:700 14px/1.6 'Noto Sans JP',sans-serif}
.ckl{list-style:none;padding:0;margin:0}
.ckl li{padding:4px 0;font-size:14.5px;line-height:1.55}
.ckl label{display:flex;gap:8px;cursor:pointer}
.ckl input{width:18px;height:18px;accent-color:#C8102E;flex:0 0 auto;margin-top:2px}
.ckl input:checked+span{text-decoration:line-through;opacity:.5}
.pls{display:flex;flex-direction:column;gap:0;border-left:4px solid #1A1C22;margin-left:28px!important}
.pl{display:flex;gap:16px;padding:10px 0 14px 18px;position:relative}
.pl::before{content:"";position:absolute;left:-12px;top:16px;width:16px;height:16px;border-radius:50%;background:var(--c);border:3px solid #FBF8F1}
.pl.done{opacity:.45}
.pl.cur .pc{border:3px solid var(--c);box-shadow:4px 4px 0 var(--c)}
.pd{flex:0 0 110px;display:flex;flex-direction:column;font:700 13px 'JetBrains Mono',monospace;padding-top:8px}
.pd span{opacity:.7}
.pd i{font:900 11px 'Noto Sans JP',sans-serif;font-style:normal;margin-top:4px;color:var(--c)}
.pl.cur .pd i{background:var(--c);color:#fff;padding:1px 6px;align-self:flex-start}
.pc{flex:1;min-width:0;background:#fff;border:2px solid #1A1C22;padding:10px 14px}
.pc h3{margin:0;font:900 17px 'Noto Sans JP',sans-serif;color:var(--c)}
.pc p{margin:4px 0;font:700 13.5px/1.6 'Noto Sans JP',sans-serif}
.pc ul{margin:4px 0 0;padding-left:1.2em;font-size:13.5px;line-height:1.7}
.pc li.see{list-style:none;margin-left:-1.2em;font-weight:700}
.bar.ph{top:5px;height:20px;background:color-mix(in srgb,var(--c) 30%,#fff);border-width:2px}
.nowph .bar.ph{background:var(--c)}
.nowph .gl{background:#FFF4F4}
/* 今週のまとめ */
.wks{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}
.wk{background:#fff;border:2px solid #1A1C22;padding:10px 14px}
.wk h3{margin:0 0 6px;font:900 15px 'Noto Sans JP',sans-serif;letter-spacing:.05em}
.wk ul{margin:0;padding-left:1.1em;font-size:14px;line-height:1.7}
.wk li{margin-bottom:6px}
.wk li b{display:block;font:700 12px 'JetBrains Mono',monospace;color:#C8102E}
.wk li small{display:block;font-size:12.5px;opacity:.75;line-height:1.5}
.wk a{font-size:12px}
/* 比較表 */
.cmpw{overflow-x:auto}
.cmp{width:100%;min-width:760px;border-collapse:collapse;font-size:13.5px;background:#fff}
.cmp th,.cmp td{border:1px solid #1A1C22;padding:7px 9px;text-align:left;vertical-align:top;line-height:1.55}
.cmp tr:first-child th{background:#1A1C22;color:#FBF8F1;font:700 12.5px 'Noto Sans JP',sans-serif}
.cmp th small,.cmp td small{display:block;font-size:11.5px;font-weight:400;opacity:.75}
.cmp tr+tr th{font:900 14px 'Noto Sans JP',sans-serif;background:#F4F1EA;white-space:nowrap}
.cmp i{font:900 11px 'Noto Sans JP',sans-serif;font-style:normal;color:#fff;padding:1px 6px}
.cmp .ok{background:#2E7D4F}.cmp .ng{background:#C8102E}.cmp .uk{background:#5B6170}
/* 出願の流れ */
.fls{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}
.fl{background:#fff;border:2px solid #1A1C22;border-top:8px solid var(--c);padding:12px 16px}
.fl h3{margin:0;font:900 17px 'Noto Sans JP',sans-serif;color:var(--c)}
.fl>p{margin:4px 0 0;font-size:13px;line-height:1.6;opacity:.8}
.fl ol{list-style:none;counter-reset:st;margin:10px 0 0;padding:0}
.fl li{counter-increment:st;position:relative;padding:0 0 12px 34px;font-size:13.5px;line-height:1.55}
.fl li::before{content:counter(st);position:absolute;left:0;top:0;width:24px;height:24px;border-radius:50%;
 background:var(--c);color:#fff;font:900 12px/24px 'Noto Sans JP',sans-serif;text-align:center}
.fl li:not(:last-child)::after{content:"";position:absolute;left:11px;top:26px;bottom:2px;border-left:2px solid color-mix(in srgb,var(--c) 35%,#fff)}
.fl .fw{display:block;font:700 12px 'JetBrains Mono','Noto Sans JP',monospace;color:var(--c)}
.fl b{display:block;font:700 14px/1.5 'Noto Sans JP',sans-serif}
.fl small{display:block;font-size:12.5px;opacity:.8}
/* 役割分担 */
.rlw{overflow-x:auto;border:2px solid #1A1C22}
.rl{border-collapse:collapse;min-width:1500px;background:#fff;font-size:12.5px}
.rl th,.rl td{border:1px solid #CFC9BD;vertical-align:top;padding:7px 8px;width:170px}
.rl tr:first-child th{background:#1A1C22;color:#FBF8F1;text-align:left;border-color:#444}
.rl tr:first-child th b{display:block;font:900 13px 'Noto Sans JP',sans-serif}
.rl tr:first-child th small{font:500 11px 'JetBrains Mono',monospace;opacity:.8}
.rl tr:first-child th i{display:inline-block;font:900 10px 'Noto Sans JP',sans-serif;font-style:normal;background:#C8102E;padding:0 5px;margin-bottom:2px}
.rl tr:first-child th.cur{background:#C8102E}
.rl th.who{position:sticky;left:0;z-index:1;width:110px;background:var(--c,#1A1C22);color:#fff;text-align:left}
.rl th.who span{font-size:20px}
.rl th.who b{display:block;font:900 15px 'Noto Sans JP',sans-serif}
.rl th.who small{font-size:11px;opacity:.85;line-height:1.4;display:block}
.rl td ul{margin:0;padding-left:1.05em;line-height:1.55}
.rl td li{margin-bottom:4px}
.rl td.cur{background:color-mix(in srgb,var(--c) 9%,#fff);box-shadow:inset 0 0 0 2px #C8102E}
.rl td.done{opacity:.4}
.rl tr:first-child th.done{opacity:.55}
/* 月カレンダー */
.ctabs{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:12px}
.ctabs button{font:700 13px 'Noto Sans JP',sans-serif;border:2px solid #1A1C22;background:#FBF8F1;padding:4px 12px;cursor:pointer;color:#1A1C22}
.ctabs button.on{background:#1A1C22;color:#FBF8F1}
.month h3{margin:0 0 8px;font:900 28px 'Noto Sans JP',sans-serif}
.month h3 span{font:700 14px 'JetBrains Mono',monospace;margin-right:8px;opacity:.6}
.cal.js .month{display:none}.cal.js .month.on{display:block}
.cal:not(.js) .month{margin-bottom:24px}
.cgrid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));border-top:2px solid #1A1C22;border-left:2px solid #1A1C22}
.cgrid>div{border-right:2px solid #1A1C22;border-bottom:2px solid #1A1C22}
.wd{padding:4px;text-align:center;font:700 12px 'Noto Sans JP',sans-serif;background:#1A1C22;color:#FBF8F1}
.wd.su{color:#FF8A8A}.wd.we{color:#8AB4FF}
.cd{min-height:96px;padding:4px;display:flex;flex-direction:column;gap:3px;background:#fff;min-width:0}
.cd b{font:700 13px 'JetBrains Mono',monospace}
.cd.su b{color:#C8102E}.cd.we b{color:#1F4E8C}
.cd.pad{background:transparent}
.cd.past{background:#F1EFEA}.cd.past .ev{opacity:.55}
.cd.today{outline:3px solid #C8102E;outline-offset:-3px;background:#FFF4F4}
.ev{display:block;font:700 11px/1.3 'Noto Sans JP',sans-serif;padding:2px 4px;background:color-mix(in srgb,var(--c) 14%,#fff);border-left:3px solid var(--c);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ev.imp{background:var(--c);color:#fff}
.cd .more{font:700 11px 'JetBrains Mono',monospace;opacity:.7}
/* ガントチャート */
.gwrap{overflow-x:auto}
.gantt{min-width:820px;border:2px solid #1A1C22;background:#fff}
.gr{display:flex;border-bottom:1px solid #DDD8CE;min-height:30px}
.gr.past{opacity:.45}
.gl{flex:0 0 220px;padding:4px 10px;font:700 12.5px/1.3 'Noto Sans JP',sans-serif;border-right:2px solid #1A1C22;display:flex;flex-direction:column;justify-content:center;min-width:0}
.gl small{font:500 11px 'JetBrains Mono',monospace;opacity:.7}
.gt{flex:1;position:relative}
.gt i{position:absolute;top:0;bottom:0;border-left:1px dashed #CFC9BD}
.ghead{background:#1A1C22;color:#FBF8F1;min-height:30px;border-bottom:none}
.ghead .gl{border-right-color:#FBF8F1}
.gm{position:absolute;top:0;bottom:0;display:flex;align-items:center;justify-content:center;font:900 13px 'Noto Sans JP',sans-serif;border-left:1px solid #555}
.gfoot{min-height:0;border:none}
.gk{padding:4px 10px;font:900 13px 'Noto Sans JP',sans-serif;letter-spacing:.2em;background:color-mix(in srgb,var(--c) 14%,#fff);border-left:8px solid var(--c);border-bottom:1px solid #DDD8CE}
.bar{position:absolute;top:7px;height:16px;min-width:6px;background:color-mix(in srgb,var(--c) 55%,#fff);border:2px solid var(--c)}
.bar.imp{background:var(--c)}
.bar.dot{width:14px!important;min-width:14px;height:14px;top:8px;transform:translateX(-4px) rotate(45deg);background:var(--c)}
.now{position:absolute;top:0;bottom:-2000px;border-left:2px solid #C8102E;z-index:2;pointer-events:none}
.now span{position:absolute;top:6px;left:3px;font:900 11px 'Noto Sans JP',sans-serif;color:#fff;background:#C8102E;padding:1px 4px;white-space:nowrap}
.gantt{position:relative;overflow:hidden}
.gfoot .now{display:none}
/* 志望校 */
.tgs{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}
.tg{border:2px solid #1A1C22;border-top:8px solid #D6336C;padding:14px 16px;background:#fff}
.tg h3{margin:0;font:900 20px 'Noto Sans JP',sans-serif}
.tg .meta{font-size:13px;opacity:.8;margin-top:2px}
.tg .nx{display:inline-block;margin-top:8px;font:700 12px 'Noto Sans JP',sans-serif;background:#D6336C;color:#fff;padding:2px 8px}
.tg ul{list-style:none;padding:0;margin:10px 0 0;font-size:14px}
.tg li{display:flex;gap:10px;padding:3px 0;border-bottom:1px dotted #CCC}
.tg li span{flex:0 0 150px;font:700 12px 'JetBrains Mono',monospace;padding-top:2px}
.tg li.past{opacity:.45}
.tg.empty{border-top-color:#999}
.tg.empty p{font-size:14px;line-height:1.7}
code{font-family:'JetBrains Mono',monospace;background:#EDEBE6;padding:0 4px}
/* おすすめ学部 */
.rcs{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px}
.rc{border:2px solid #1A1C22;border-top:8px solid var(--c);padding:14px 16px;background:#fff;display:flex;flex-direction:column;gap:8px}
.rc h3{margin:0;font:900 19px 'Noto Sans JP',sans-serif;color:var(--c)}
.rc p{margin:0;font-size:14px;line-height:1.75}
.rc dl{display:grid;grid-template-columns:auto 1fr;gap:2px 10px;margin:0;font-size:13px}
.rc dt{font:700 12px 'Noto Sans JP',sans-serif;background:var(--c);color:#fff;padding:0 6px;align-self:start}
.rc dd{margin:0;line-height:1.6}
.rc ul{list-style:none;padding:0;margin:0;border-top:2px solid #1A1C22}
.rc li{padding:6px 0;border-bottom:1px dotted #BBB;display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 8px;font-size:14px}
.rc li small{font:500 11.5px 'JetBrains Mono','Noto Sans JP',monospace;opacity:.7}
.rc li span{flex-basis:100%;font-size:12.5px;line-height:1.55;opacity:.85}
.lv{font:900 10px 'Noto Sans JP',sans-serif;font-style:normal;color:#fff;padding:1px 5px;background:#5B6170}
.lv-チャレンジ{background:#C8102E}.lv-実力相応{background:#1F4E8C}.lv-安全寄り{background:#2E7D4F}
.tips{margin-top:18px;border:2px solid #1A1C22;background:#fff;padding:12px 16px}
.tips h3{margin:0 0 6px;font:900 16px 'Noto Sans JP',sans-serif;letter-spacing:.1em}
.tips ol{margin:0;padding-left:1.4em;font-size:14px;line-height:1.8}
/* ニュース */
.nts{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:18px}
.nt h3{margin:0 0 6px;padding-bottom:4px;border-bottom:3px double #1A1C22;font:900 16px 'Noto Sans JP',sans-serif;letter-spacing:.15em}
.nt ul{list-style:none;padding:0;margin:0}
.nt li{padding:7px 0;border-bottom:1px dotted #BBB;display:flex;flex-direction:column;gap:2px}
.nt li a{text-decoration:none;font-weight:700;font-size:14.5px;line-height:1.5}
.nt li a:hover{text-decoration:underline}
.nt li small{font:500 11px 'JetBrains Mono',monospace;opacity:.65}
.new,.hotn{font:900 10px 'JetBrains Mono',monospace;font-style:normal;color:#fff;background:#C8102E;padding:1px 4px;margin-right:6px;vertical-align:2px}
.hotn{background:#1A1C22;font-family:'Noto Sans JP',sans-serif}
details summary{cursor:pointer;font:700 13px 'Noto Sans JP',sans-serif;padding:6px 0}
/* 時間割 */
.tts{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:18px}
.tt h3{margin:0 0 8px;font:900 17px 'Noto Sans JP',sans-serif}
.tt table{width:100%;border-collapse:collapse;font-size:14px;background:#fff}
.tt th,.tt td{border:1px solid #1A1C22;padding:6px 8px;text-align:left;vertical-align:top}
.tt th{font-family:'Noto Sans JP',sans-serif;white-space:nowrap;background:#F4F1EA}
.tt td.mono{white-space:nowrap}
/* 変更点 */
.chg{list-style:none;padding:0;display:flex;flex-direction:column;gap:10px}
.chg li{border-left:6px solid #C8102E;padding:4px 14px;background:#fff}
.chg b{font:900 15px 'Noto Sans JP',sans-serif}
.chg p{margin:4px 0 0;font-size:14px;line-height:1.75}
/* やること */
.hint{font-size:13px;opacity:.7;margin-top:0}
.cks{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px}
.ck h3{margin:0 0 6px;font:900 15px 'Noto Sans JP',sans-serif;letter-spacing:.1em}
.ck ul{list-style:none;padding:0;margin:0}
.ck li{padding:4px 0;font-size:14px;line-height:1.5}
.ck label{display:flex;gap:8px;cursor:pointer}
.ck input{width:18px;height:18px;accent-color:#C8102E;flex:0 0 auto;margin-top:2px}
.ck input:checked+span{text-decoration:line-through;opacity:.5}
/* リンク */
.lks{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:10px}
.lk{border:2px solid #1A1C22;padding:10px 12px;text-decoration:none;background:#fff;display:flex;flex-direction:column;gap:2px}
.lk b{font:700 14px 'Noto Sans JP',sans-serif}
.lk small{font-size:12px;opacity:.7}
.lk:hover{background:#1A1C22;color:#FBF8F1}
footer{font-size:11px;opacity:.7;line-height:1.7}
@media (max-width:640px){
 .page{padding:16px 16px 40px}
 .mast .logo{width:64px;font-size:30px}
 .mast .meta{width:100%;border-left:none;border-top:1px solid #1A1C22;flex-direction:row;flex-wrap:wrap;gap:10px}
 .ups{grid-template-columns:1fr}
 .up .n{flex-basis:64px;font-size:14px;white-space:nowrap}
 .gl{flex-basis:130px;font-size:11.5px}
 .gantt{min-width:720px}
 .cd{min-height:64px;padding:2px}
 .ev{font-size:9.5px;padding:1px 2px}
 .nts,.tts,.tgs,.rcs{grid-template-columns:1fr}
 .nowbox .nh{flex-basis:100%}
 .tabs button{padding:8px 2px}
 .tabs button b{font-size:13px}
 .tabs button small{display:none}
 .pd{flex-basis:76px;font-size:11.5px}
 .pls{margin-left:16px!important}
 section>*:not(h2){margin-left:12px;margin-right:12px}
 .tg li span{flex-basis:120px}
}
"""

JS = """
(function(){
 var panels=[].slice.call(document.querySelectorAll('.panel')),btns=[].slice.call(document.querySelectorAll('.tabs button'));
 if(panels.length){
  document.documentElement.classList.add('js-tabs');
  function open(t,scroll){
   panels.forEach(function(p){p.classList.toggle('on',p.dataset.tab===t)});
   btns.forEach(function(b){b.classList.toggle('on',b.dataset.tab===t)});
   try{localStorage.setItem('juken-tab',t)}catch(_){}
   if(scroll)document.getElementById('tabs').scrollIntoView({behavior:'smooth'});
  }
  btns.forEach(function(b){b.onclick=function(){open(b.dataset.tab,true)}});
  function fromHash(){
   var el=location.hash&&document.getElementById(location.hash.slice(1));
   var p=el&&el.closest('.panel');
   if(p){open(p.dataset.tab);el.scrollIntoView();return true}
  }
  window.addEventListener('hashchange',fromHash);
  var saved=null;try{saved=localStorage.getItem('juken-tab')}catch(_){}
  if(!fromHash())open(panels.some(function(p){return p.dataset.tab===saved})?saved:panels[0].dataset.tab);
 }
 var cal=document.querySelector('.cal');
 if(cal){
  cal.classList.add('js');
  var ms=[].slice.call(cal.querySelectorAll('.month')),tabs=[].slice.call(cal.querySelectorAll('.ctabs [data-m]'));
  var keys=ms.map(function(m){return m.dataset.m});
  function show(k){
   ms.forEach(function(m){m.classList.toggle('on',m.dataset.m===k)});
   tabs.forEach(function(b){b.classList.toggle('on',b.dataset.m===k)});
   cal.dataset.cur=k;
  }
  tabs.forEach(function(b){b.onclick=function(){show(b.dataset.m)}});
  cal.querySelector('.prev').onclick=function(){var i=keys.indexOf(cal.dataset.cur);if(i>0)show(keys[i-1])};
  cal.querySelector('.next').onclick=function(){var i=keys.indexOf(cal.dataset.cur);if(i<keys.length-1)show(keys[i+1])};
  show(cal.dataset.cur);
 }
 var store={};
 try{store=JSON.parse(localStorage.getItem('juken-check')||'{}')}catch(_){}
 document.querySelectorAll('.ck input,.ckl input').forEach(function(c){
  c.checked=!!store[c.dataset.k];
  c.onchange=function(){store[c.dataset.k]=c.checked;try{localStorage.setItem('juken-check',JSON.stringify(store))}catch(_){}};
 });
})();
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", default="all", choices=["all", "collect", "notify", "render"])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = load()
    if args.dry_run:
        collect(cfg, dry_run=True)
        for key, n, ev in due_reminders(cfg, events(cfg), today(cfg)):
            print(f"  [提醒] {ev['title']} {days_label(n)}")
        return
    if args.cmd in ("all", "collect"):
        print("受験通信：ニュース収集")
        collect(cfg)
    if args.cmd in ("all", "notify"):
        run_notify(cfg)
    if args.cmd in ("all", "render"):
        write_page(cfg)


if __name__ == "__main__":
    sys.exit(main())

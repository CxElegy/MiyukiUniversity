# 受験通信 · 美幸の大学受験 2027

美幸（高3・理系・地方国公立志望）の大学受験ページ：**https://cxelegy.github.io/MiyukiUniversity/**

- **いまやること**：逆算プランのうち、今の期間の「やること」（チェックできる）
- **カウントダウン**：共通テスト・国公立前期まであと何日
- **次の予定**：近い締切から順に
- **逆算プラン**：本番から逆算した、期間ごとの目標とやること
- **月カレンダー**／**受験ガントチャート**（2026/10〜2027/3、種類ごとに色分け、今日の線つき）
- **志望校**・**おすすめ学部**（理系×地方国公立、Kei-Net 2027 ボーダー、チャレンジ／実力相応／安全寄り）
- **受験ニュース**：Google ニュースから2時間ごとに自動で集める（共通テスト・国公立・私立・推薦・制度変更・英検・志望校）
- 共通テストの時間割、今年の変更点、やることリスト、公式サイトへのリンク

日程が近づくと Discord でリマインド（30・14・7・3・1・0日前）、重要なニュースはその日のうちに届く。

## はじめの設定

1. **GitHub Pages**：Settings → Pages → Source を *Deploy from a branch*、ブランチ `main`、フォルダ `/docs`。
2. **Discord（任意）**：チャンネルの設定 → 連携サービス → ウェブフック → URL をコピーして、
   Settings → Secrets and variables → Actions → **Secrets** に `DISCORD_WEBHOOK_URL` として追加。
   設定しなければ通知は送らない（ページの更新だけ）。
3. **実行**：Actions → 受験通信 → Run workflow。以後は2時間ごとに自動で動く。

## 更新のしかた

ぜんぶ [`juken.yaml`](juken.yaml) に書く（項目の説明はファイル冒頭）。Claude に
「志望校に〇〇大学〇〇学部を追加して」と頼めば、日程を調べて書き込む（`.claude/skills/juken/SKILL.md`）。

ページは公開されるので、学校名・クラス・フルネームなど本人が特定できる情報は書かないこと。

### Claude に頼めること（`.claude/skills/`）

| スキル | 頼み方の例 |
|---|---|
| `juken` | 「志望校に〇〇大学〇〇学部を追加して」「逆算プランを今の成績に合わせて直して」 |
| `daigaku` | 「〇〇大と△△大の工学部を比べて」「共テ60%で行ける理系の国公立は？」「MARCH の延納制度を調べて」 |
| `seiri` | 「このプリント（写真・PDF）を整理してページに入れて」「今週のまとめ」「ニュースにいらないのが混ざってる」 |

## ローカルで動かす

```bash
pip install -r requirements.txt
python scripts/juken.py             # 収集 → リマインド → ページ生成
python scripts/juken.py render      # ページだけ作り直す
python scripts/juken.py --dry-run   # 集まるニュースを見るだけ
python -m unittest discover tests   # テスト（ネットにつながない）
```

## ファイル

| パス | 内容 |
|---|---|
| `juken.yaml` | 日程・志望校・逆算プラン・おすすめ学部・ニュースのテーマ |
| `scripts/juken.py` | ニュース収集、リマインド、ページ生成 |
| `scripts/sources.py` | Google ニュースの取得、重複判定、Discord 送信 |
| `docs/index.html` | 公開ページ（自動生成） |
| `state/` | 集めたニュース・送信済みの記録（自動で更新、手で触らない） |
| `tests/` | テスト |

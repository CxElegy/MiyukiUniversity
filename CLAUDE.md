# 受験通信（MiyukiUniversity）

美幸（高3・理系）の 2027 年大学受験ページ。公開URL：https://cxelegy.github.io/MiyukiUniversity/

- 第一志望は**地方の国公立（理系）**。私大は東洋大・MARCH を押さえ（滑り止め）として検討中。
- データは全部 `juken.yaml`。ページ `docs/index.html` は `scripts/juken.py` が生成する（手で編集しない）。
- GitHub Actions が2時間ごとにニュース収集・Discord リマインド・ページ更新をして、`state/` と `docs/` をコミットする。
  push する前に `git pull --rebase` すること。

## 絶対に守ること

- **ページは公開される**。学校名・クラス・フルネーム・住所・顔写真など、本人が特定される情報は
  `juken.yaml`・コミットメッセージ・ページのどこにも書かない（名前は「美幸」だけ）。
- 日付・数字には出典（`source` / `url`）。確定していないものは `tentative: true`。曜日は計算で検算する。
- 美幸本人向けのページなので、ページの文言は**日本語・やさしく・短く**。

## スキル

| スキル | いつ使う |
|---|---|
| `juken` | 志望校の追加、日程・逆算プランの更新 |
| `daigaku` | 大学・学部を調べる、比べる、候補を探す |
| `seiri` | プリント・PDF・ニュースなどを整理してページに振り分ける、今週のまとめ |

## 確認

```bash
python scripts/juken.py render
python -m unittest discover tests
```
変えたらスクリーンショットで見た目を確認してから commit・push。

# Cover Studio — グラビア誌表紙制作環境

グラビア誌の表紙を制作するためのツール一式です。用途に応じて2つの方法が使えます。

## 1. ブラウザデザイナー（`index.html`）

`index.html` をブラウザで開くだけで使えます。インストール不要。

- 左パネルで誌名ロゴ・号数・メイン特集・モデル名・サブ見出し・価格・テーマカラーを編集
- 表紙写真はファイル選択またはドラッグ&ドロップで配置
- 表紙上のテキストは直接クリックしても編集可能
- 「PDFとして書き出す」でブラウザの印刷ダイアログから **B5実寸（182×257mm）のPDF** を保存

## 2. Python バッチ生成（`generate.py`）

JSON設定から **300dpi・B5判のPNG（2150×3035px）** を書き出します。入稿用や大量生成向け。

```bash
pip install -r requirements.txt
cp config.sample.json my-cover.json   # 設定を編集
python generate.py my-cover.json
```

### 設定項目（`config.sample.json`）

| キー | 内容 |
|---|---|
| `logo` | 誌名ロゴ |
| `issue` | 号数・発行日 |
| `main_copy` | メイン特集（`\n` で改行） |
| `model_name` | モデル名 |
| `sublines` | サブ見出しの配列 |
| `price` | 価格表記 |
| `accent_color` / `sub_color` | テーマカラー / サブカラー |
| `photo` | 表紙写真のパス（`null` なら無地） |
| `font_path` | 日本語フォント（TTF/TTC/OTF）のパス。`null` なら Noto Sans CJK 等を自動検出 |
| `output` | 出力ファイル名 |

日本語を描画するため、環境に日本語フォントが必要です（Ubuntu なら `sudo apt install fonts-noto-cjk`）。

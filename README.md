# PONTE SEO改善アプリ

登録済みの3サイトから1つ選ぶと、次の処理を自動実行するStreamlitアプリです。

- ぽんて鍼灸整骨院（https://ponte-nene.jp/）
- ぽんてアロマサロン（https://ponte-aroma.jp/）
- ぽんておすすめブログ（https://ponte-nene.net/）

1. Search Console・GA4からダウンロードしたCSV／Excelを読み込み
2. 最大15ページの公開HTMLを技術SEO監査
3. Geminiが改善優先度、リライト対象、構成、完成本文、30日計画を作成

Googleの管理者権限、サービスアカウント、Google Cloud設定は不要です。データをアップロードしない場合も、公開ページのSEO監査だけで分析を続行します。

## 1. GitHubへアップロード

このフォルダ内のファイルを、そのままGitHubリポジトリへアップロードします。`.streamlit/secrets.toml.example`には秘密情報を記入せず、見本のまま置いてください。

## 2. Streamlit Community Cloudへ公開

- Main file path: `app.py`
- Advanced settings > Secrets: `secrets.toml.example`を参考に、本物の値を設定

## 3. Search Consoleデータの準備

1. Search Consoleを開く
2. 左上で分析したいサイトを選ぶ
3. 「検索結果」または「検索パフォーマンス」を開く
4. 期間を「過去3か月」などに設定
5. 右上の「エクスポート」を押す
6. 「Excelをダウンロード」または「CSVをダウンロード」を選ぶ

Excelはクエリ・ページなど複数のシートを一度に読み込めるためおすすめです。CSVを使う場合は、クエリとページの両方をアップロードすると分析精度が上がります。

## 4. GA4データの準備

1. Googleアナリティクスを開く
2. 分析したいプロパティを選ぶ
3. 「レポート」→「エンゲージメント」→「ランディングページ」を開く
4. 分析期間をSearch Consoleと同じ期間にする
5. 右上の共有アイコンから「ファイルをダウンロード」→「CSVをダウンロード」を選ぶ

アプリのサイドバーで、Search ConsoleとGA4のファイルを選びます。その後、3サイトから分析対象を選んで「分析する」を押してください。URLの手入力は不要です。

## 5. Gemini APIキー

Google AI StudioでAPIキーを発行し、Streamlit Secretsの`GEMINI_API_KEY`へ設定します。料金・無料枠・利用上限はGoogle側の最新表示を確認してください。

モデルは`gemini-3.6-flash`を使用します。以前のSecretsに`gemini-2.5-flash`が残っていても、アプリが自動的に新しいモデルへ切り替えます。

## ローカル起動

```bash
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
streamlit run app.py
```

macOS/Linuxでは有効化コマンドを`source .venv/bin/activate`へ変更してください。

## 安全上の注意

- Gemini APIキーをGitHubへ直接アップロードしないでください。
- 公開アプリではサイドバー入力よりStreamlit Secretsを推奨します。
- 医療・健康記事はAI原稿をそのまま公開せず、施術者が事実確認してください。
- ダウンロードした期間・行数が分析範囲になります。Search ConsoleとGA4は同じ期間に揃えるのがおすすめです。

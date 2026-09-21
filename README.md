# PONTE SEO改善アプリ

URLを1つ入力すると、次の処理を自動実行するStreamlitアプリです。

1. Search Console・GA4の直近90日データを取得
2. 最大15ページの公開HTMLを技術SEO監査
3. Geminiが改善優先度、リライト対象、構成、完成本文、30日計画を作成

Google連携が未設定・取得失敗の場合も、公開ページのSEO監査だけで分析を続行します。

## 1. GitHubへアップロード

このフォルダ内のファイルを、そのままGitHubリポジトリへアップロードします。`.streamlit/secrets.toml.example`には秘密情報を記入せず、見本のまま置いてください。

## 2. Streamlit Community Cloudへ公開

- Main file path: `app.py`
- Advanced settings > Secrets: `secrets.toml.example`を参考に、本物の値を設定

## 3. Google側の初回設定

1. Google Cloudで「Google Search Console API」と「Google Analytics Data API」を有効化
2. サービスアカウントを作り、JSONキーを取得
3. JSON内の`client_email`をコピー
4. Search Consoleの各プロパティで、そのメールアドレスを閲覧ユーザーとして追加
5. GA4の「管理 > プロパティのアクセス管理」で、そのメールアドレスを閲覧者として追加
6. 各GA4プロパティIDをStreamlit Secretsの`GA4_PROPERTY_MAP`へ登録

以後はトップ画面にURLを入れ、「分析する」を押すだけです。

## 4. Gemini APIキー

Google AI StudioでAPIキーを発行し、Streamlit Secretsの`GEMINI_API_KEY`へ設定します。料金・無料枠・利用上限はGoogle側の最新表示を確認してください。

## ローカル起動

```bash
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
streamlit run app.py
```

macOS/Linuxでは有効化コマンドを`source .venv/bin/activate`へ変更してください。

## 安全上の注意

- サービスアカウントJSONやAPIキーをGitHubへ直接アップロードしないでください。
- 公開アプリではサイドバー入力よりStreamlit Secretsを推奨します。
- 医療・健康記事はAI原稿をそのまま公開せず、施術者が事実確認してください。
- Search Console APIは仕様上、すべての行を返すとは限りません。


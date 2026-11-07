# PONTE SEO改善アプリ

## 毎月10日の自動レポート

`.github/workflows/monthly-report.yml`をGitHubリポジトリに置き、GitHub Actionsを有効にすると、日本時間の**毎月10日9:17**に前月末までの完了した3か月を分析します。例：4月10日には1月1日〜3月31日。3サイトと2店舗のGBPについて、それぞれPDFとMarkdownをGoogle Driveに保存します。`REPORT_EMAIL_TO`を設定した場合は、保存先リンクと失敗した対象の一覧をメールでも通知します。GitHubのActions画面から手動実行もできます。スケジュールは多少遅れる場合があります。

このジョブはStreamlitのログインとは独立しています。無人運転には各サービスの閲覧・GBP管理権限を持つGoogleアカウントの**オフラインOAuth更新トークン**が必要です。画面用の一時アクセストークンは使えません。認証する際は `access_type=offline` と次のスコープを要求します：

- `https://www.googleapis.com/auth/analytics.readonly`
- `https://www.googleapis.com/auth/webmasters.readonly`
- `https://www.googleapis.com/auth/business.manage`（GBP API承認後）
- `https://www.googleapis.com/auth/drive.file`（ジョブが作るDriveファイルの保存）
- `https://www.googleapis.com/auth/gmail.send`（メール通知を使う場合）

OAuth同意画面が外部の「テスト」状態では、これらのスコープを含む更新トークンが7日で失効する場合があります。長期の無人運用にはGoogleの同意画面の公開・必要な審査・GBP API承認を完了し、**このジョブ用のOAuthクライアントで許可したすべてのスコープを含む更新トークン**を取得してください。GmailやDriveの利用承認・スコープ検証が別途必要になることがあります。Google CloudでDrive API、Gmail API（通知を使う場合）、Analytics Data API、Search Console API、Business Profile Performance APIも有効にします。StreamlitのSecretsにあるOAuthクライアントを自動転用するわけではありません。`drive.file` スコープで既存フォルダを指定する場合は、そのフォルダにアプリからアクセスできることを確認してください。まずは `DRIVE_FOLDER_ID` を空欄にしてDrive直下への保存を試すのが簡単です。

GitHubリポジトリの Settings → Secrets and variables → Actions に以下を設定します。秘密情報をリポジトリのファイルへ記載しないでください。

| 種類 | 名前 | 設定内容 |
|---|---|---|
| Secret | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` | 上記スコープで承認したOAuthクライアントとオフライン更新トークン |
| Secret | `GEMINI_API_KEY` | GeminiのAPIキー |
| Variable | `GSC_NENE`, `GSC_AROMA`, `GSC_BLOG` | 各サイトのSearch Consoleプロパティ。例：`sc-domain:ponte-nene.jp`（閲覧可能なプロパティを指定） |
| Variable | `GA4_NENE`, `GA4_AROMA`, `GA4_BLOG` | 各サイトに対応する数字のGA4プロパティID |
| Variable | `GBP_NENE`, `GBP_AROMA` | 2店舗のGBP店舗リソース名。例：`locations/123456789` |
| Variable | `DRIVE_FOLDER_ID` | 保存先フォルダID（省略時は認証アカウントのDrive直下） |
| Variable | `REPORT_EMAIL_TO` | 通知先メールアドレス（省略時は送信しない） |
| Variable | `GEMINI_MODEL` | 使用可能なGeminiモデル名（省略時はアプリの既定値） |

まずActionsの「Run workflow」で試し、実行結果のリンクと5対象のPDF・Markdownを確認します。取得できない対象は推測で補わず、ジョブを失敗とし、完了した分だけ保存します。メールを設定した場合、失敗した対象も通知します。取得範囲の末日や未確定の数値はレポートに明記する指示になっています。GitHub Actionsの成果物にも30日間残りますが、長期保存先はDriveです。GBP APIが未承認なら2店舗の自動レポートは生成できません。GBPの手動ファイル分析は従来通り使えます。

登録済みの3サイトと2つのGoogleビジネスプロフィール（GBP）から、分析対象を1つ選べるStreamlitアプリです。

- ぽんて鍼灸整骨院（https://ponte-nene.jp/）
- ぽんてアロマサロン（https://ponte-aroma.jp/）
- ぽんておすすめブログ（https://ponte-nene.net/）
- ぽんて鍼灸整骨院（GBP）
- ぽんてアロマサロン（GBP）

1. サイト選択時は、Googleアカウント経由でSearch Console・GA4を直接取得するか、CSV／Excelをアップロードし、公開ページと一緒に分析
2. GBP選択時は、承認済みのGoogle Business Profile APIから直接取得するか、対応店舗のGBPパフォーマンスCSV／Excelを専用分析
3. Geminiが改善優先度と30日計画を作成（サイト分析ではリライト原稿も作成）

サイトの直接取得には、使用するGoogleアカウントの各サイト・GA4プロパティへの閲覧権限、Google CloudのOAuth初回設定、3つのAPIの有効化が必要です。サービスアカウントとGA4・Search Consoleの管理者権限は不要です。GBPの直接取得には、さらにGoogleによるGBP APIの利用承認、対象店舗の管理権限と追加設定が必要です。従来のファイルアップロードも利用できます。

## Googleから直接取得する初回設定

1. Google Cloudでプロジェクトを作成または選択し、「APIとサービス」→「ライブラリ」で **Google Analytics Data API / Google Analytics Admin API / Search Console API** を有効にします。以前のプロジェクトでData APIとSearch Console APIだけ有効にした場合は、Admin APIも追加してください。
2. 「Google Auth Platform」または「OAuth同意画面」でアプリ情報を登録します。外部・テスト運用の場合は、自分のGoogleアカウントをテストユーザーに追加します。読み取り専用スコープ `analytics.readonly` と `webmasters.readonly` を許可する構成にします。
3. 「クライアント」→「OAuthクライアントを作成」→種類「ウェブアプリケーション」を選び、承認済みリダイレクトURIに `https://実際のアプリURL/oauth2callback` を登録します。URLはStreamlit Community Cloudの実際のアプリURLを使い、末尾に `/oauth2callback` を付けます。
4. クライアントIDとクライアントシークレットを取得し、Streamlit Community Cloudのアプリ管理画面 → Settings → Secrets に、`.streamlit/secrets.toml.example` の `[auth]` 部分を本物の値に置き換えて追加します。既存の `GEMINI_API_KEY` を消さないでください。`cookie_secret` は任意の長いランダム文字列を設定します。秘密情報をGitHubへ置かないでください。
5. アプリ再起動後、サイドバーで「Googleから直接取得」を選び、「Googleアカウントで接続」→「接続できるプロパティを表示」を押します。分析するサイトに対応するSearch ConsoleプロパティとGA4プロパティを選び、「分析する」を押します。直近90日間のうち最新3日を除く日付を両サービスへ指定して取得します。

GA4プロパティ一覧が見えない場合は、対象プロパティの数字のIDを入力できます。GA4を閲覧できるGoogleアカウントでも、誤ったプロパティIDや別アカウントでの接続は取得できません。Google接続には期限があるため、期限切れの際はログアウトして再接続してください。権限がないサイトは所有者に閲覧者として追加してもらう必要があります。Google Cloudを設定できない場合は、以下のファイルアップロード方式を使えます。

## GBPもGoogleから直接取得する（API承認後）

1. 前にGoogleへ提出したGBP API申請の結果を確認します。Google Cloudで **Business Profile Performance API** の割当が0 QPMなら未承認です。承認後、そのプロジェクトで **My Business Business Information API** と **Business Profile Performance API** を有効にします。
2. Streamlit Community CloudのSecrets内、`[auth]` にある `client_kwargs` の `scope` に `https://www.googleapis.com/auth/business.manage` を追加します。ZIP内の `.streamlit/secrets.toml.example` の最新版を参照してください。変更後はGoogleからログアウトしてログインし直し、新しい権限に同意します。GBPの公式APIが要求するスコープは管理権限を含む名称ですが、このアプリは店舗一覧とパフォーマンス取得のGETリクエストのみを実行し、プロフィールを変更しません。
3. アプリで「ぽんて鍼灸整骨院（GBP）」または「ぽんてアロマサロン（GBP）」を選び、サイドバーで「Googleから直接取得」を選択します。「Googleアカウントで接続」→「接続できるプロパティを表示」の順に進み、対象店舗の名称・ウェブサイトを見て店舗を選択し、「分析する」を押します。

日別の表示・電話ボタン・ルート・サイトクリックを直近90日間から最新3日を除いて取得し、月ごとに集計します。検索語句は完了済みの直近3か月・最大300件を取得します。検索語句の「しきい値」は正確な表示回数ではありません。クリックは電話や来店が成立した件数を意味しません。API申請が受付段階や未承認なら、これまで通りGBPファイルをアップロードして分析できます。

直接取得はGSCの検索語句・ページ、GA4のランディングページについて、それぞれ上位1000件までの分析用データです。全行の完全バックアップではありません。両サービスで数値の定義も異なります。

## 1. GitHubへアップロード

このフォルダ内のファイルを、そのままGitHubリポジトリへアップロードします。`.streamlit/secrets.toml.example`には秘密情報を記入せず、見本のまま置いてください。

## 2. Streamlit Community Cloudへ公開

- Main file path: `app.py`
- Advanced settings > Secrets: `secrets.toml.example`を参考に、本物の値を設定

## 3. Search Consoleデータの準備（ファイル方式）

1. Search Consoleを開く
2. 左上で分析したいサイトを選ぶ
3. 「検索結果」または「検索パフォーマンス」を開く
4. 期間を「過去3か月」などに設定
5. 右上の「エクスポート」を押す
6. 「Excelをダウンロード」または「CSVをダウンロード」を選ぶ

Excelはクエリ・ページなど複数のシートを一度に読み込めるためおすすめです。CSVを使う場合は、クエリとページの両方をアップロードすると分析精度が上がります。

## 4. GA4データの準備（ファイル方式）

1. Googleアナリティクスを開く
2. 分析したいプロパティを選ぶ
3. 「レポート」→「エンゲージメント」→「ランディングページ」を開く
4. 分析期間をSearch Consoleと同じ期間にする
5. 右上の共有アイコンから「ファイルをダウンロード」→「CSVをダウンロード」を選ぶ

アプリのサイドバーで、Search ConsoleとGA4のファイルを選びます。その後、3サイトから分析対象を選んで「分析する」を押してください。URLの手入力は不要です。サイト分析ではGBPデータを使用しません。

## 5. GBPデータの準備

ファイル方式を使う場合は、対応するGoogleビジネスプロフィールのパフォーマンス画面から、検索語句・表示・電話・ルート・ウェブサイトクリックなどのデータをCSV／Excelでダウンロードし、サイドバーの「Googleビジネスプロフィール（GBP）データ」へ追加します。

分析対象のボックスには、3サイトと2店舗のGBPを合わせた5項目が表示されます。GBPを選択した場合はSEO記事を生成せず、GBPの強み・課題・改善策・投稿案・30日計画を表示します。

## 6. Gemini APIキー

Google AI StudioでAPIキーを発行し、Streamlit Secretsの`GEMINI_API_KEY`へ設定します。料金・無料枠・利用上限はGoogle側の最新表示を確認してください。

モデルは`gemini-3.6-flash`を使用します。以前のSecretsに`gemini-2.5-flash`が残っていても、アプリが自動的に新しいモデルへ切り替えます。

Gemini側が混雑して`503 UNAVAILABLE`になった場合は、`gemini-3.5-flash`、続いて`gemini-3.5-flash-lite`へ自動的に切り替えて分析します。すべて混雑している場合は、5〜10分後に再度「分析する」を押してください。

## 7. レポートのダウンロード

新しい分析では、最初に行う3つの作業、具体的な実行手順、文案例、担当者への依頼文、完了確認、集客の測定方法を出力します。実行手順は画面・PDF・Markdownで共通です。取得できていない情報は「要確認」とし、順位や予約数の改善を保証せず、同じ条件のデータで検証します。以前に作ったレポートは自動変更されないため、更新後に再度分析してください。

分析後に「PDFでダウンロード」または「Markdownでダウンロード」を押します。分析結果はセッション内に保持されるため、PDFをダウンロードした後も画面は初期化されず、続けてMarkdownもダウンロードできます。両形式とも選択したサイト名・店舗名、分析種別、日本の日付がファイル名に入ります（例：`ぽんて鍼灸整骨院_サイト_2026-10-05.pdf`）。PDFはA4で日本語の本文・改善計画・分析根拠を含みます。

PDF用の日本語フォントは、アプリと同じ階層に置く`NotoSansJP-Regular.ttf`です。GitHubへ更新する際は`app.py`、`requirements.txt`、`NotoSansJP-Regular.ttf`を同じ場所へアップロードしてください。フォントがない場合もアプリは停止せず、内蔵の代替フォントでPDFを作成します。フォントのライセンスは`OFL.txt`にあります。

## ローカル起動

```bash
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
streamlit run app.py
```

macOS/Linuxでは有効化コマンドを`source .venv/bin/activate`へ変更してください。

## 安全上の注意

- Gemini APIキーとGoogle OAuthのクライアントシークレットをGitHubへ直接アップロードしないでください。
- 公開アプリではサイドバー入力よりStreamlit Secretsを推奨します。
- 医療・健康記事はAI原稿をそのまま公開せず、施術者が事実確認してください。
- ダウンロードした期間・行数が分析範囲になります。Search ConsoleとGA4は同じ期間に揃えるのがおすすめです。

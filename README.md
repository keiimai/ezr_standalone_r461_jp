# EZR と RStudio の実行環境・standalone 版（R 4.6.1）

ブラウザーの中で [EZR](https://www.jichi.ac.jp/usr/hema/EZR/statmed.html) と RStudio を使える環境を、Zip で配るためのリポジトリです（日本語版）。使う人のパソコンには何も入れません。Zip の中の Python と QEMU が小さな Linux の仮想マシンを動かし、その中の Docker Engine で R・RStudio・EZR を動かします。

> この環境は、EZR の開発元とは関係の無い、非公式の配布環境です。EZR の開発元が作ったり、確かめたり、勧めたりしたものではありません。この環境についての問い合わせは、EZR の開発元には送らず、この環境を配った人に送ってください。

## 使い方

配られた Zip（`EZR_Standalone_jp_r461_<版>.zip`）を展開し、中の手順書を開きます。起動から、EZR を開いて解析し、終わるまでを、写真付きで説明しています。

| パソコン | 手順書 |
|---|---|
| Mac | `HOW_TO_USE_Mac.html` |
| Windows | `HOW_TO_USE_Windows.html` |

## できること

- クリックだけで EZR を開けます。
- 研究ごとに「プロジェクト」を作り、データ（CSV・Excel）と記録を分けて置けます。
- EZR の操作の記録（R スクリプト・R マークダウン・出力・グラフ）が自動で残り、EZR を閉じると、図の入った報告書（`markdown.html`）ができます。使ったデータの指紋（SHA-256）と、R とパッケージの版も残ります。

## 仕組み

EZR（R のパッケージ RcmdrPlugin.EZR）は、Linux の仮想マシンの中の Docker のコンテナで、公式の手順と同じく、RStudio とは別の普通の R で開きます。EZR の画面はコンテナの中の仮想ディスプレイに描かれ、RStudio Server の中継を通して、ブラウザーの別のタブに出ます。

```
ブラウザ ─ http://localhost:<番号>（RStudio Server）
launcher/standalone.py（Zip の中の Python）
  └─ QEMU
      Linux（runtime/linux/engine-<arm64|amd64>.qcow2 ＋ .state/disk.qcow2）─ Docker Engine
        /mnt/project ＝ app/（SMB で共有し、パソコンのフォルダに直接書く）
        コンテナ（/app ＝ app/）
          ├─ RStudio の R ─「EZR を開く」の窓 → ezr_open()
          ├─ Xvnc :1 ＋ fluxbox ─ EZR の R（普通の R）─ R Commander + EZR
          ├─ websockify :6080（noVNC）─ RStudio の中継 /p/<鍵>/ でブラウザへ
          └─ projects/<名前>/ ─ data/・outputs/<YYYYMMDDHHMM>_<4桁>/
```

初めての起動では、仮想マシンの中で EZR のイメージを作る（インターネットが要る）。2 回目からは作ったイメージを使い、環境の材料（`app/` の `Dockerfile`・`DESCRIPTION`・`renv.lock`・`R/` など）が変わったときだけ作り直す。作ったあとに `renv.lock` が `DESCRIPTION` と合っているかを確かめ、合わなければ作り直す。

## プロジェクトと記録

プロジェクトは `app/projects/<名前>/` で、窓で選んだデータは `data/` に写す（CSV・Excel、1 つ 15 MiB まで。上書きしない）。EZR を開くたびに、記録のフォルダ `outputs/<YYYYMMDDHHMM>_<4桁>/` を 1 つ作る。画面の「記録のフォルダ」で `app/projects` が開く。

| ファイル | 中身 |
|---|---|
| `script.R`・`markdown.Rmd`・`output.txt` | EZR の欄の写し（書き換わるたびに書き出す） |
| `plots/` | グラフの PNG |
| `console.log` | EZR の R の出力 |
| `meta.json` | 環境・データの SHA-256・乱数の状態・記録のハッシュ |
| `markdown.html` | EZR を閉じると作る報告書（作れなければ `markdown.log`） |

「停止」は、記録のハッシュと報告書ができるまで待つ。「停止」の前に切れた回（電源を切った・「待たずに止める」）は、書き出したファイルは残るが、ハッシュは確定せず、報告書も作り直さない。

## 配る

| 項目 | 中身 |
|---|---|
| 作る | `ezr_dist/tools/standalone/make_zip.py bundle r461 jp`（Git 外）が、コミット済みの HEAD から Zip を作る（道具の説明 → `ezr_dist/tools/README.md`） |
| 入る | このリポジトリと同じ形（一番上：起動用のファイル・`launcher/`・手順書・`README.md`・`CITATION.cff`・`LICENSE`・`LICENSE-docs`。`app/`：解析のフォルダ）に、`runtime/`（Python・QEMU・Linux の基盤と、ファイルの SHA-256 の一覧）・`Start EZR.app`・`THIRD_PARTY.md`（ライセンスの文は `runtime/licenses/`）・`app/TEMPLATE_VERSION.txt` を足す |
| 入らない | `tests/test_standalone.py`・`launcher/MacLauncher.swift`・開発の設定（`.github/`・`.githooks/`・`.devcontainer/`・`.vscode/`・`.editorconfig`・一番上の `.gitignore`・`app/tools/precommit_checks.R`） |
| 渡す | ダウンロードか USB |

## 起動の記録

| 記録 | 中身 |
|---|---|
| `.state/console.log` | 仮想マシンの Linux の出力 |
| `.state/image.json` | 作ったイメージと、元にしたコミット |
| `.state/session.json` | RStudio のアドレス、準備にかかった時間、仮想マシンのメモリ |
| `.state/engine.sha256` | 仮想マシンのディスクの土台（`runtime/linux/engine-<arm64|amd64>.qcow2`）の SHA-256。土台が変わると、ディスクを作り直す |

## リポジトリから直接動かす

リポジトリは Zip と同じ形で、一番上に起動用のファイル・`launcher/`・手順書・`README.md`・`CITATION.cff`、`app/` に解析のフォルダがある。`runtime/`・`Start EZR.app`・`THIRD_PARTY.md` は Git に載せず、`ezr_dist/tools/standalone/make_zip.py local r461 jp`（Git 外）が置く。`runtime/` は、4 つのリポジトリで共通の `ezr_dist/tools/standalone/runtime/` へのリンク。置いたあとは、Zip と同じく起動用のファイルから起動できる。コンテナから先だけを試すときは、`app/` で、パソコンの Docker の `docker build --target dev -t ezr-standalone-r461:dev .` と `docker compose up -d dev` を使える（保守者のコマンドは `app/` で `make help`）。

## 試験

`app/` の `make test`（`check`・`verify` にも入る）が `app/tests/testthat` をすべて走らせる。`test-project_ezr.R` のうち 13 個は、一番上の起動のファイル（`start_*`・`launcher/`）を試す（コンテナの中では見えないので飛ばす。ホストの R で走らせると試す）。standalone の起動の流れは `make ezr-test-launcher`（一番上の `tests/test_standalone.py`。仮想マシンは起こさない）。

## コードの説明

コメントは `app/R/project_ezr.R`・`launcher/standalone.py` と、その試験（`app/tests/testthat/test-project_ezr.R`・`tests/test_standalone.py`）にだけ、関数とテストごとに 1 行置く。

| ファイル | 役割 |
|---|---|
| `start_mac.command`・`start_windows.bat` | 起動用のファイル。画面を出し、画面から呼ばれると `launcher/standalone.py` を動かす |
| `launcher/standalone.py` | 仮想マシン・共有・コンテナの起動と停止。`runtime/` の SHA-256 を起動のたびに照らす |
| `launcher/MacLauncher.swift` | Mac の画面（`Start EZR.app`）の元。Zip を作るときにアプリにする |
| `launcher/windows_gui.ps1` | Windows の画面 |
| `launcher/start_messages.txt` | 起動と画面の文の一覧 |
| `app/ezr_start.R` | 「EZR を開く」の窓を出す |
| `app/R/project_ezr.R` | EZR を開き、記録を残す関数 |
| テンプレートの余白（`app/` の `docker/project_*`・`project.mk`・`.Rprofile` の末尾など） | EZR に要るパッケージ・設定・命令 |
| `app/tests/testthat/test-project_ezr.R` | `app/R/project_ezr.R` の試験 |
| `tests/test_standalone.py` | standalone の起動の流れの試験 |

## ライセンスと引用

- コードは [MIT](https://opensource.org/license/mit)（`LICENSE`）です。
- 利用者向けの手順書（`HOW_TO_USE_*.html` とその中の写真）は [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/deed.ja)（`LICENSE-docs`）です。写真に写るソフトウェアの画面の権利は、それぞれの作者にあります。
- Zip の `runtime/` には、Python・QEMU・Linux（Alpine）・Docker Engine が入っています。それぞれの作者の条件に従います。部品ごとの版とライセンスは Zip の `THIRD_PARTY.md`、ライセンスの文は `runtime/licenses/` にあります。GPL・LGPL・MPL などの部品のソースは、この環境を配った人が保管していて、求めがあれば渡します。初めての起動で取ってくるもの（R、EZR を含む R のパッケージ、RStudio Server など）は、配るものに入れていません。
- この環境を引用するときは、`CITATION.cff` の情報を使ってください。作者への問い合わせ先も、`CITATION.cff` にあります。

## AI の使用

コードと文書は、AI のコーディング支援を道具として使って書いた。standalone の起動の道具は、OpenAI の Codex で作り始め、Anthropic の [Claude Opus 5.5](https://www.anthropic.com/claude/opus)（[Claude Code](https://code.claude.com/docs/en/overview) で使った）で作り替えた。

## 困ったとき

| こんなとき | こうします |
|---|---|
| EZR のタブが開かず、RStudio に「Popup Blocked」と出た | ブラウザーが新しいタブを止めました。窓の「Try Again」を押すと、EZR のタブが開きます。 |
| 準備中のまま、なかなか進まない | 初めての起動は、イメージを作るので時間がかかります。「EZR Standalone 実行環境」の画面を開いたまま待ちます。 |
| 「EZR の実行環境を作れませんでした」と出る | インターネットにつながっているか、ディスクの空きがあるかを確かめて、「起動」を押します（プロキシの要るネットワークでは、パソコンのプロキシの設定を使います。通信の中身を調べるネットワークでは作れないことがあります）。 |
| ブラウザーに RStudio が開かない | 「EZR Standalone 実行環境」の画面の「ブラウザーを開く」を押します。それでも開かないときは、「アドレスをコピー」を押し、ブラウザーのアドレス欄を右クリックして「貼り付けて移動」を押します（Safari では「ペーストして移動」）。 |
| RStudio や EZR のタブの表示が止まった | そのタブの「再読み込み」を押します。 |
| EZR の画面が固まった・窓が見当たらない | RStudio の Files から `ezr_start.R` を開いて「Source」を押し、同じプロジェクトを選びます。それでも直らないときは、「停止」で止めてから、「起動」で起動し直します（記録は残ります）。 |
| 別のプロジェクトを選んだら、RStudio の左下（Console）に、前の EZR を閉じられないと赤字で出た | 前の EZR で、ファイルを選ぶ窓などが開いたままか、計算の途中です。EZR のタブでその窓を閉じる（計算なら終わるのを待つ）と、前の EZR が閉じるので、もう一度選びます（記録は残ります）。 |
| 「EZR を開く」の窓に、「前の回の報告書（markdown.html）を作っています。…」と赤字で出た | 前の回の報告書ができるまで、少し待ってから、もう一度「EZR を開く」を押します。 |
| 別のプロジェクトを選んだら、RStudio の左下（Console）に「前の回の報告書を作っています。できたら「…」を開きます…」と出て、すぐには開かない | 前の回の報告書ができると、自動で開きます。そのまま待ちます。 |
| 準備や停止の途中に「EZR Standalone 実行環境」の画面を閉じようとしたら、「準備・停止処理が終わるまでお待ちください」と出た | Mac は「戻る」、Windows は「OK」を押し、終わるまで待ちます。 |
| 「停止」を押しても止まらない | EZR を × で閉じ、少し待ってから、もう一度「停止」を押します。 |
| 「停止できませんでした」と出る | 「EZR Standalone 実行環境」の画面の下の欄の表示を確かめてから、もう一度「停止」を押します。 |
| EZR の「ツール」→「オプション...」で「R Commanderの再起動」を押した | 「終了?」は OK、「スクリプトファイルを保存?」などは「No」を押します。新しい設定で開き直したら、データを「データセット:」のボタンで選び直します（データと記録は続きます）。 |
| Mac で、`start_mac.command` を初めて開いたときに止められた | 手順書の方法 A か B で開きます。2 回目からも出るときは、出た表示をそのまま、この環境を配った人に伝えてください。 |
| Mac で、「このアプリだけでは起動できません」と出る | `Start EZR.app` ではなく、`start_mac.command` から起動します。アプリだけを別の場所に写したときにも出ます。 |
| Windows で、「Windows によって PC が保護されました」と出る | 「詳細情報」を押し、出てきた「実行」を押します。 |
| Windows で、「このフォルダは、すでに起動しています。」と出る | 開いている「EZR Standalone 実行環境」の画面（タスクバーにあります）に戻ります。 |
| RStudio のタブに「R Session Disconnected」と出た | RStudio は、ブラウザーのタブ 1 つにしかつながりません。別のタブで RStudio を開くと、前のタブがこの表示になります。このタブは閉じます。「EZR を開く」の窓が出ないときは、`ezr_start.R` の「Source」を押します。Console が反応しないときは、先に Console の右上の赤い停止のボタンを押します。 |
| RStudio に「R is taking longer to start than usual」と出て進まない | 「Reload」を押します。 |
| 「EZR を開く」の窓が出たまま、RStudio のページを読み込み直したり、タブを閉じて開き直したりしたら、窓が出ず、Console も使えない | Console の右上の赤い停止のボタンを押してから、`ezr_start.R` の「Source」を押します。窓がもう一度出ます。 |
| ブラウザーが「翻訳しますか?」と尋ねる | 翻訳はせず、× で閉じます。 |
| 狭い画面で、RStudio の「Source」・［Render］の文字が見えない | ボタンが矢印のアイコンだけになっています。隣のチェック（「Source on Save」・「Render on Save」）ではなく、矢印のアイコンを押します。 |
| パッケージ（依存関係）を変えたい | 今の `EZR_Standalone_r461` フォルダは変えずに、配られた Zip をもう一度展開して新しい `EZR_Standalone_r461` フォルダを作り、そちらで変えます。起動すると、変えたものに合わせて環境を作り直します。今のフォルダの解析は、今までと同じパッケージのまま続けられます。 |
| 「起動または停止に失敗しました（終了コード…）」と出る | 下の欄に「「起動」をもう一度押してください」などと書いてあれば、そのとおりにします。書いていないとき、またはそれでも直らないときは、「EZR Standalone 実行環境」の画面の下の欄に出た表示をそのまま、この環境を配った人に伝えてください。 |

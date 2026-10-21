<h3 align="center">
  <img src="assets/Harness2-wordmark.png" alt="Harness²" width="360"><br>
  オープンワールドのための再帰的エージェント・ハーネシング
</h3>

<p align="center">
  <a href="https://arxiv.org/pdf/2610.14224"><img src="https://img.shields.io/badge/Paper-arXiv-EA4335?logo=arxiv&logoColor=white" alt="論文"></a>
  <a href="https://harness2.github.io"><img src="https://img.shields.io/badge/Website-harness2.github.io-4285F4?logo=googlechrome&logoColor=white" alt="Web サイト"></a>
  <a href="docs/BENCHMARKS.md"><img src="https://img.shields.io/badge/Docs-Benchmark%20guides-FBBC04" alt="ベンチマークガイド"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-34A853" alt="ライセンス"></a>
</p>

<p align="center">
  <a href="README.md">English</a> · <b>日本語</b>
</p>

> [!NOTE]
> この文書は [README.md](README.md) の日本語版です。内容が食い違う場合は英語版を正としてください。リンク先のガイド（`docs/`）は英語のみです。

## ひとことで

**ハーネス**とは、モデルをエージェントとして動かすための周辺一式（プロンプト、ツール、メモリ、スキル、オーケストレーションなど）のことです。**ハーネシング**はそのハーネスを最適化する作業を指します。Harness² はこれをタスクごとに自動で行います。**モデルの重みは変えず、正解データによるフィードバックも使いません。** ハーネスへの変更案を出し、同じタスクで実際に動かし、実行同士の振る舞いの違いからより良いハーネスを組み立て、うまくいったことを同じドメインの次のタスクのために記憶します。

## 目次

- [主要なアイデア](#主要なアイデア)
- [アーキテクチャ](#アーキテクチャ)
- [ハーネスの構成要素](#ハーネスの構成要素)
- [インストール](#インストール)
- [ベンチマークの実行](#ベンチマークの実行)
- [実験](#実験)
- [Web アクセス](#web-アクセス)
- [結果と再開](#結果と再開)
- [リポジトリ構成](#リポジトリ構成)
- [紹介動画](#紹介動画)
- [引用](#引用) · [コントリビュート](#コントリビュート) · [ライセンス](#ライセンス) · [免責事項](#免責事項)

## 主要なアイデア

Harness² が想定するのは**オープンワールド**、つまりハーネス設計時には見たことのないタスクにエージェントが出会う状況です。2 つの階層の再帰をつなぎます。

| 階層 | 改善する対象 | 方法 |
|---|---|---|
| **タスクレベルの再帰** | 現在のタスク用の*実行ハーネス* | 対照的な **Propose–Probe–Compose** ステップ。変更案を出し（Propose）、同じタスクでその効果を試し（Probe）、実行間の違いから改良版ハーネスを合成する（Compose） |
| **ハーネシングレベルの再帰** | 永続的な*改善ハーネス* | 各タスクの後、採用したハーネスと実行証拠に裏付けられた教訓をドメイン別ライブラリに保存し、以降の改良に役立てる |

<p align="center">
  <a href="assets/harness2_pipeline.png">
    <img src="assets/harness2_pipeline.png" alt="Harness² のパイプライン。タスクレベルの再帰では、エージェント実行器が初期ハーネスを実行し、ハーネスエディタが汎用（invariant）とドメイン固有の変更を提案し、候補プローブがそれらを実行し、ハーネスコンポーザが最終ハーネスに統合する。ハーネシングレベルの再帰では、経験リフレクタがそのステップを振り返り、次のタスクで使う改善ハーネスを更新する。" width="100%">
  </a>
</p>

**結果。** 3 つの専門業務ベンチマークと 4 つのモデル×ハーネス構成において、Harness² を 1 ステップ適用するだけで GLM-5 と Gemini 3.5 Flash がベースのハーネスより改善しました。WorkBuddy-Bench で最大 **+11.5 ポイント**、JobBench で **+4.5 ポイント**、LAB の部分合格率で **+11.8 パーセントポイント** です。タスクレベルの再帰予算を増やすとさらに伸びます。

**このリリースが対応するもの:** 並列・逐次の再帰、OpenCode と Codex の実行器、JobBench・LAB・WorkBuddy-Bench の各ベンチマーク。

## アーキテクチャ

コードは互いに独立した 3 つの軸に分かれています。**手法**（`harness2/core`、ベンチマークにも実行器にも依存しない）、ロールアウトを実行する**サブストレート**（OpenCode または Codex）、タスク・起動処理・採点を提供する**ベンチマーク**アダプタです。

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/architecture/architecture-dark.svg">
    <img src="assets/architecture/architecture-light.svg" alt="Harness² のアーキテクチャ図。CLI（harness2-setup/-check、harness2、harness2-demo、harness2-judge/-report）が harness2.core を動かす。core では schedule がタスクごとに k 回の共有ベースロールアウトを実行し、タスクレベルの再帰が c1..c8 のハーネスツリーと正解を含まない証拠ツリーを使って Propose・Probe・Compose を行い、selection が k 個の最終候補から 1 つを盲目的に選んで振り返る。採用されたハーネスと教訓はドメイン別ライブラリ（MemoryConnector。既定は FileConnector で、ほかのコネクターに差し替えられる）に入り、次のタスクを導く。core は実行・評価側を呼び出す。ベンチマークアダプタ（JobBench、LAB、WorkBuddy-Bench）がサブストレート（OpenCode または Codex CLI）上でロールアウトを起動し、サブストレートは Vertex AI のモデル（実行器・採点用の Gemini、改善用の Claude または Gemini）を呼ぶ。タスクの取り込みでは、任意のタスクコネクター（同梱のローカルのフォルダ、entry point の harness2.task_connectors に登録したインストール済みのパッケージ、または module:Attr で差し込むもの）が受け取ったタスクを正規化に渡して TaskSpec にし、IntakeAdapter がベンチマークのアダプターと同じように実行する（private_criteria は採点者だけが読む）。コードのタスクでは、ロールアウトはそれぞれ専用の git worktree で動き、コネクターの workspace policy の確認を通ったあと、選ばれた実行の diff だけをソースのリポジトリに当てる。出力は runs/ に保存される。" width="100%">
  </picture>
</p>

1 つのタスクで起きること:

1. **ベースロールアウト。** `schedule` が現在のベースハーネスでタスクを `k` 回実行します。この実行結果は両方の再帰モードで共有されます。
2. **Propose（提案）。** 改善セッションが証拠ツリー（`context`。タスク、省略なしの実行軌跡、成果物、ハーネスを含み、採点結果は決して含まない）を読み、8 つのハーネス構成要素（`tree`）への変更を出力します。変更は*汎用（invariant）*か*ドメイン固有*のどちらかです。
3. **Probe（試行）。** 各候補ハーネスを、ベンチマークアダプタと選んだ実行器を通して同じタスクで実行します。
4. **Compose（合成）。** コンポーザが、振る舞いの違いが裏付ける変更を改良版ハーネスにまとめます。逐次モードでは Propose–Probe–Compose を `k` 回繰り返します。
5. **選択と振り返り。** `selection` が `k` 個の最終候補から、正解やスコアを見ずに 1 つを選び、なぜうまくいったかを振り返ります。
6. **記憶。** `library` が採用ハーネスと教訓をドメインごとに保存します。同じドメインの後続タスクはこれを読みます。これがハーネシングレベルの再帰です。

採点（`harness2-judge`）はその後に実行され、選ばれた実行だけを採点します。手法側のコードがスコアを読むことはありません。予算はモード当たり `R = 4k` ロールアウト、`P = 3k` 改善ステージに固定されています（`core/ledger.py`）。完了したロールアウトにはフィンガープリントが付くので、再実行しても安全に再開できます（`core/runmark.py`）。

図のソースは [`assets/architecture/src`](assets/architecture/src) にあります。`python3 assets/architecture/src/render.py` で再生成できます（[D2](https://d2lang.com) が必要）。

## ハーネスの構成要素

Harness² はモデルの周りに 8 つの編集可能な構成要素を公開しています。**システムプロンプト、ガードレール、メモリ、ツールガイダンス、サブエージェント、スキル、スクリプト、プラグイン**です（コード上は `c1`..`c8`）。これらがコンテキストの組み立て、ツールのインターフェース、作業の委任、ツールループ内の振る舞いを制御します。

<p align="center">
  <a href="assets/components.png">
    <img src="assets/components.png" alt="実行時の役割ごとに分類した 8 つのハーネス構成要素。システムプロンプト・ガードレール・メモリ・スキルはコンテキストを構築し、ツールガイダンスとスクリプトはツールのインターフェースを定義し、サブエージェントは委任された作業を取りまとめ、プラグインはミドルウェアを制御する。" width="100%">
  </a>
</p>

## インストール

**Linux、Python 3.12、[uv](https://docs.astral.sh/uv/getting-started/installation/)** を使います。リポジトリのルートで:

```bash
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -e '.[render]'
harness2-demo --output runs/demo --k 2
```

ローカルデモは、決定的なエディタと実際の Python 実行を使って両方の再帰モードを動かします。**モデルの認証情報は不要**なので、ワークフロー全体を確かめる一番手軽な方法です。

モデルを使うベンチマークには、**Node.js 20 以上、Google Cloud CLI**、[ベンチマークの依存関係](docs/SETUP.md#requirements)をインストールしてください。LAB には Pandoc と Poppler、WorkBuddy-Bench には Docker が必要です。そのうえでモデルを設定します:

```bash
cp .env.example .env
# .env に Vertex AI のプロジェクトとモデル ID を書き込む
gcloud auth application-default login
```

モデル設定、別の認証方法、[Codex 実行器](docs/SETUP.md#codex-executor)については[セットアップガイド](docs/SETUP.md)を参照してください。

主な設定項目:

| 設定 | 意味 |
|---|---|
| `HARNESS2_SOLVER_MODEL` | 実行器のモデル（OpenCode の `provider/model` 形式） |
| `HARNESS2_IMPROVER_MODEL` | 改善役のモデル（同じ形式。省略時は実行器と同じ） |
| `HARNESS2_JUDGE_MODEL` | 採点モデルの ID（Vertex の接頭辞はアダプタが付ける） |
| `HARNESS2_RUNS_DIR` | 出力先。既定は `runs/` |

## ベンチマークの実行

どのベンチマークも同じ 5 段階で進みます:

| 段階 | コマンド | 内容 |
|---|---|---|
| 1. インストール | `harness2-setup <bench>` | 固定版のツール、ランナーの上書き、依存関係、データを導入 |
| 2. 確認 | `harness2-check --bench <bench>` | ベンチマークが実行可能か検査 |
| 3. 改良 | `harness2 --bench <bench> ...` | タスクレベルとハーネシングレベルの再帰を実行 |
| 4. 採点 | `harness2-judge --bench <bench> ...` | 各タスクで選ばれた実行を採点 |
| 5. レポート | `harness2-report --bench <bench> ...` | ベンチマークの代表指標を表示 |

例として、LAB を導入して 1 タスクを実行・採点・レポートする手順:

```bash
harness2-setup lab
harness2-check --bench lab
harness2 --bench lab --k 1 --mode parallel \
  --only-domains antitrust-competition --max-tasks 1
harness2-judge --bench lab --k 1 --mode parallel
harness2-report --bench lab --k 1 --mode parallel
```

> [!IMPORTANT]
> モデルを使う実行には API 料金がかかります。まずは少数のタスクから始めてください。`harness2` に `--dry-run` を付けると、モデルを呼ばずにタスクの選択結果だけを確認できます。採点とレポートは、改良時と同じベンチマーク・実行器・サブセット・`--k`・`--mode`・シードで実行する必要があります。

| ベンチマーク | タスク | インストール | ガイド |
|---|---|---|---|
| JobBench | 専門職の業務 | `harness2-setup jb` | [JobBench](docs/BENCHMARKS.md#jobbench) |
| LAB | 法務 | `harness2-setup lab` | [LAB](docs/BENCHMARKS.md#lab) |
| WorkBuddy-Bench | オフィス、Web、コード | `harness2-setup wb` | [WorkBuddy](docs/BENCHMARKS.md#workbuddy-bench) |

1 コマンドにつき 1 つのベンチマークを選びます。WorkBuddy ではセットアップ後のすべてのコマンドに `--subset office`、`--subset web`、`--subset code` のいずれかも指定します。各ベンチマークのデータ、ドメイン、採点方法は[ベンチマークガイド](docs/BENCHMARKS.md)にあります。

## 実験

`--k` はタスクレベルの再帰予算です。広げ方は 2 通りあります。**並列再帰**は複数の候補ハーネスを探索し、共有された実行証拠から改良を合成します。**逐次再帰**は改良したハーネスと蓄積した証拠を引き継ぎながら Propose–Probe–Compose を繰り返します。

<p align="center">
  <a href="assets/harness2_recursion_modes.png">
    <img src="assets/harness2_recursion_modes.png" alt="タスクレベルの再帰を広げる 2 つの方法。並列: k 個の汎用候補と k 個のドメイン固有候補を試し、共有された証拠から k 個の合成を作る。逐次: 改良したハーネスと蓄積した証拠を k 回の Propose–Probe–Compose ステップに引き継ぐ。" width="100%">
  </a>
</p>
<p align="center"><em>並列再帰はより広く探索し、逐次再帰はより深く磨き込みます。</em></p>

| オプション | 動作 |
|---|---|
| `--k 1` | Propose–Probe–Compose を 1 ステップ |
| `--k K --mode parallel` | 汎用候補 `K` 個とドメイン固有候補 `K` 個を試し、`K` 個の合成を作る |
| `--k K --mode sequential` | `K` 回の連続ステップでハーネスを磨き込む |
| `--substrate cx` | 既定の OpenCode の代わりに Codex を実行器にする |

各タスクの後、ハーネシングレベルの再帰が採用ハーネスと証拠に裏付けられた教訓を保持し、以降の改良に活かします。タスクの選び方、並行実行、再現性、ベースラインとの比較は[実験ガイド](docs/EXPERIMENTS.md)を参照してください。

## Web アクセス

> [!WARNING]
> 実行器のネットワークアクセスは制限されておらず、Codex はサンドボックスを無効にして動きます。公開されているベンチマークの採点基準にオンラインでアクセスできてしまう可能性があります。評価の前に[ネットワーク制限](docs/EXPERIMENTS.md#web-access)を設定し、ベンチマークの正解や採点基準に到達できないようにしてください。

## 結果と再開

結果は `runs/` 以下に、ベンチマーク（該当する場合はサブセット）ごとに保存されます。タスクの要約、成果物、採用されたハーネス、振り返りメモ、評価結果が含まれます。

同じコマンドをもう一度実行すると続きから再開します。モデルやタスクの選択を変えるときは新しい `HARNESS2_RUNS_DIR` を使ってください。詳しくは[結果と再開](docs/EXPERIMENTS.md#results-and-resume)を参照してください。各コマンドは `--help` にも対応しています。

## リポジトリ構成

| パス | 内容 |
|---|---|
| [`harness2/core/`](harness2/core) | 手法本体: スケジューリング、再帰、プロンプト、改善セッション、証拠ツリー、ハーネスツリー、選択、ライブラリ、予算台帳、採点とレポート |
| [`harness2/core/intake.py`](harness2/core/intake.py) | タスクの取り込み: RawTask、TaskSpec とそのスキーマ、TaskConnector の登録 |
| [`harness2/core/normalize.py`](harness2/core/normalize.py) | 受け取ったタスクを TaskSpec に直す正規化（passthrough と model） |
| [`harness2/core/intake_local.py`](harness2/core/intake_local.py) | ローカルのフォルダのタスクコネクター（inbox、outbox、failed） |
| [`harness2/substrate/`](harness2/substrate) | 共通インターフェースの背後にある実行器: OpenCode と Codex CLI |
| [`harness2/benchmarks/`](harness2/benchmarks) | JobBench、LAB、WorkBuddy-Bench のアダプタ |
| [`harness2/benchmarks/intake.py`](harness2/benchmarks/intake.py) | ベンチマーク以外のタスク向けの IntakeAdapter と `run_intake` |
| [`harness2/cli/intake.py`](harness2/cli/intake.py) | `harness2-intake`: 任意のタスクコネクターからタスクを取り込み、git worktree で回し、選ばれた実行の diff だけを戻す（[手順](docs/BENCHMARKS.md#connecting-a-task-source)） |
| [`harness2/cli/`](harness2/cli) | `harness2-*` コマンド群 |
| [`harness2/plugins/mcp/`](harness2/plugins/mcp) | 任意の MCP クライアント（標準ライブラリだけで書かれ、ループバックだけの HTTP と stdio に対応）。MCP のタスクサーバーにつなぐタスクコネクターが使う。core は import しない |
| [`harness2/testing/`](harness2/testing) | タスクコネクターの作者向けの契約テストの道具（`connector_contract`。[手順](docs/BENCHMARKS.md#writing-a-task-connector)） |
| [`harness2/demo.py`](harness2/demo.py) | `harness2-demo` が使う決定的なローカルアダプタ |
| [`docs/`](docs) | セットアップ、ベンチマーク、実験のガイド（英語） |
| [`scripts/`](scripts) | 一括実行スクリプト、セットアップと出所の検査 |
| [`third_party/`](third_party/README.md) | 上流のベンチマークと実行器向けのランナー上書きとパッチ |
| [`dependencies/`](dependencies/README.md) | 固定した依存バージョンとアセット |
| [`tests/`](tests) | ワークフローのテスト |

## 紹介動画

https://github.com/user-attachments/assets/76b10548-b14b-4c8f-9aa5-a93662df4d56

## 引用

```bibtex
@article{xu2026harness2,
  title   = {Harness$^2$: Recursive Agent Harnessing for an Open World},
  author  = {Xu, Ruiyao and Chen, Yanfei and CuiZhu, Zhongying and Dalvi Mishra, Bhavana and Ming, Yifei and Yu, Han and Han, Rujun and Lee, Chen-Yu and Pfister, Tomas},
  journal = {arXiv preprint arXiv:2610.14224},
  year    = {2026},
  url     = {https://arxiv.org/abs/2610.14224}
}
```

## コントリビュート

[CONTRIBUTING.md](CONTRIBUTING.md) を参照してください。

## ライセンス

Harness² は [Apache 2.0](LICENSE) ライセンスで提供されています。[`third_party/`](third_party/README.md) 以下のサードパーティコード、ベンチマーク、データセットはそれぞれ上流のライセンスに従います。[サードパーティに関する告知](dependencies/README.md)を参照してください。

## 免責事項

これは Google の公式サポート製品ではありません。このプロジェクトは [Google Open Source Software Vulnerability Rewards Program](https://bughunters.google.com/open-source-security) の対象外です。

# 評価の土台 (eval graph) — PR 2

元: 設計文書「harness2 自己改良ループ 設計案」の「評価者（多角評価）」「Graph の観点」「安全と既定値」節。Ryuki さんの回答 (2026-11-04〜07): Graph は両方 (実行のグラフと評価者のグラフ)、評価者は採点スコアを設定で明示したときだけ見る、初期の観点は 費用・予算の恒等式・変更の効果・柵と安全・再現性・汎化。短いエラー文は発生元の 2 か所で本文の引用をハッシュにする。前提は PR #14 (トレースの受け口、`docs/specs/trace-port.md`)。

## Problem Statement

PR #14 で各タスクの実行ディレクトリに `trace.jsonl` が残るようになったが、それを読んで問題を見つける仕組みがない。2026-10-24 の検証の不具合 4 件 (例えば ledger の金額が 0.0) は人が読んで見つけた。また PR #14 のレビューで、次の 2 件を持ち越した。

- memory のイベントに `mode` が無く、agg と seq の recall を見分けられない。
- 512 文字未満のエラー文が、ハーネスの抜粋や改善役の返答の一部を平文で含む。

## Solution

1. トレースに MODE の span を足す。モードの処理 (ロールアウト・改善段・選択・振り返り・memory・費用) をその子にする。memory の recall / store に `mode` を入れる。
2. 発生元の 2 か所で本文の引用をハッシュにする。1 か所目は grader verdict の検査のメッセージ (`context.py`)、2 か所目は改善役の CLI の返答の抜粋 (`proposer.py`)。summary.json の errors からも本文が消える。
3. 実行のグラフ (`harness2/core/graph.py`): 1 つ以上の `trace.jsonl` を読み、span をノード、親子を辺にしたグラフを作る。種類や run で部分グラフを切り出せる。
4. 評価者の受け口 (`harness2/core/evaluate.py`): `Viewpoint`、`Finding`、`EvalRequest`、`Evaluator` Protocol と、その登録 (entry point グループ `harness2.evaluators`、`module:Attr`)。作りは memory.py と同じ。契約テストは `harness2.testing.evaluator_contract` に置き、偽の評価者を添える。
5. 評価のグラフの実行器 (`harness2/core/evalgraph.py`): 設定ファイルでノードと辺を書く。既定は「観点ごとの評価者 → 集約」。集約は重複をまとめ、根拠の無い所見を落とし、重さと観点の数で並べる。反論ノードは受け口の形だけ用意する (モデルの評価者は PR 3)。外部のグラフ実行系は使わない。
6. 同梱の `rules` 評価者 (`harness2/evaluators/rules.py`): モデルを使わない決定的な検査。予算の恒等式、ledger と models の費用の食い違い、再試行・切り替えの多さ、柵で外された候補、エラーの集計。
7. `harness2-evaluate` CLI: 実行ディレクトリを渡すと、集約後の `findings.json` と、集約前の観点ごとの `findings/<viewpoint>.json` を書く。
8. 文書 (`docs/EVALUATION.md` と日本語版、`docs/TRACE*.md` の MODE と mode)、README の表、構成図。

## User Stories

1. harness2 を回した人として、`harness2-evaluate --runs runs/...` で、費用や予算の恒等式の崩れなどの所見を、根拠の span 付きで一覧にしたい。
2. 評価者を作る人として、`harness2.evaluators` の entry point か `module:Attr` で自分の評価者を差し込み、`harness2.testing.evaluator_contract` で契約を確かめたい。
3. 観点を足す人として、評価のグラフの設定ファイルに観点のノードと辺を書くだけで、集約まで流したい。
4. トレースを読む人として、agg と seq のどちらの recall かを `mode` で見分けたい。
5. 安全を気にする人として、トレースと summary.json のエラー文にハーネスや返答の本文が残らないようにしたい。
6. 採点と手法を分けたい人として、評価者が採点スコアを見るのは設定で明示したときだけにしたい。

## Implementation Decisions

### MODE の span と memory の mode

- `trace.Kind.MODE = "mode"` を足す。`schedule.run_task` のモードのループで、1 モードの処理 (recall の注入から `_run_one_mode`、COST まで) を `trace.span(Kind.MODE, mode=<mode>)` で囲む。ROLLOUT (role=candidate / final)、STAGE、TREE_DIFF、SELECTION、REFLECTION、COST、モードの ERROR、MEMORY_* の親は MODE になる。ベースのロールアウト (role=base) はモードの前に共有されるので、親は TASK のまま。
- `memory.recall(conn, task_id, limit_chars, *, query="", mode="")` と `memory.store(..., mode="")` にキーワード引数 `mode` を足す。attrs に `mode` を入れる (空文字でも入れる)。`schedule` は両方の呼び出しで mode を渡す。既存の呼び出しはそのまま動く。
- 文書 (`docs/TRACE.md` と日本語版) の種類の表と親子の節を直す。

### 発生元のエラー文のハッシュ

- `context.assert_tree_has_no_grader_verdicts`: マーカーがファイルの中身に見つかったときのメッセージにある本文の抜粋 (`body[where-60 : where+120]`) を、`excerpt sha256=<hex> chars=<n>` に置き換える。ファイル名・マーカー・オフセットは残す。名前に見つかったときのメッセージは本文を含まないので、そのまま。
- `proposer.Proposer._claude_cli`: `CLI error: <subtype> <返答の先頭 200 文字>` の返答の部分を `reply sha256=<hex> chars=<n>` に置き換える。`rc=...: <stderr の末尾 300 文字>` (opencode も同じ) は CLI の診断なので、そのまま残す。
- ハッシュは `trace.digest` を使う。summary.json の errors とトレースのエラー文の両方に効く。

### 実行のグラフ (`harness2/core/graph.py`)

- `Node`: `key: tuple[str, str]` (run_id, span_id)、`kind`、`run_id`、`span_id`、`parent: tuple | None`、`attrs` (start と end の attrs を合わせたもの。end が後勝ち。`phase` は除く)、`start_ts`、`end_ts`、`status` (span の end の status、point event は None)、`dur_s`、`source` (読んだ `trace.jsonl` のパス)。
- `Edge`: `src`、`dst`、`kind` (今は `"contains"` だけ。親 → 子)。
- `Graph`: `nodes: dict[key, Node]`、`edges: list[Edge]`、`roots()` (TASK のノード)、`children(key)`、`parent(key)`、`ancestors(key)`、`of_kind(*kinds)`、`runs()` (run_id → TASK のノード)、`subgraph(kinds=None, run_ids=None)` (条件に合うノードと、その間の辺。祖先が外れたノードは、残った最も近い祖先につなぎ直す)、`find(run_id, span_id)`。
- `build(paths: Iterable[Path]) -> Graph`: `trace_file.read` で読み、同じ (run_id, span_id) の start と end を 1 つのノードにまとめる。親が見つからないノードは根として残し、`warnings` に入れる。`judge.` で始まる種類は `include_judge=True` のときだけ入れる (既定は外す)。
- `load_runs(root: Path) -> tuple[Graph, dict[Path, dict]]`: `root` の下の `trace.jsonl` を全部探してグラフにし、同じディレクトリの `summary.json` を読む (無ければ入れない)。辞書のキーはタスクの実行ディレクトリ。

### 評価者の受け口 (`harness2/core/evaluate.py`)

- `Severity` (定数のクラス): `critical`、`major`、`minor`、`info`。並びの重さもここで決める。
- `Viewpoint(name, description, reads: tuple[str, ...])`: `reads` はその観点が読むノードの種類。初期の観点は定数 `VIEWPOINTS` に持つ: `cost` (費用)、`budget` (予算の恒等式)、`effect` (変更の効果)、`safety` (柵と安全)、`reproducibility` (再現性)、`generalization` (汎化)。
- `Evidence(run_id, span_id, note="")`: トレースの span への参照。
- `Finding(viewpoint, severity, claim, evidence: tuple[Evidence, ...], suggestion="", key="", evaluator="")`: `key` は重複をまとめるための識別子 (評価者が付ける。空なら `viewpoint` と `claim` から作る)。`to_dict` / `from_dict`。
- `EvalRequest(graph, summaries, viewpoints, scores=None, options={})`: `scores` は採点スコアで、設定で明示したとき (`allow_scores=True`) だけ入る。既定は None。
- `Evaluator` Protocol: `viewpoints() -> Sequence[Viewpoint]`、`evaluate(request: EvalRequest) -> list[Finding]`。
- 登録: `register` / `names` / `connector_name` / `resolve_factory` / `evaluator_for(name, *, options=None)`。作りは memory.py / trace.py と同じで、`ENTRY_POINT_GROUP = "harness2.evaluators"`、`module:Attr`。ファクトリは `factory(**options)` で呼ぶ。既定の登録は `rules`。
- 契約テスト `harness2/testing/evaluator_contract.py`: `check_evaluator(ev)` と `EvaluatorContractTests` (unittest の mixin。`make_evaluator()` を実装させる)。確かめるのは、viewpoints が空でないこと、evaluate が Finding の列を返すこと、Finding の viewpoint が自分の viewpoints のどれかであること、evidence がリクエストのグラフにある span を指すこと、scores が None でも動くこと。偽の評価者 `FixedEvaluator` (決まった所見を返す) を添える。

### 評価のグラフの実行器 (`harness2/core/evalgraph.py`)

- 設定は JSON: `{"nodes": [{"id": "rules", "type": "evaluator", "evaluator": "rules", "viewpoints": ["cost", ...], "options": {}}, {"id": "agg", "type": "aggregate"}], "edges": [["rules", "agg"]], "allow_scores": false}`。ノードの種類は `evaluator`、`rebut`、`aggregate` の 3 つ。`aggregate` はちょうど 1 つで、出口になる。
- `default_config(evaluators: Sequence[str])`: 評価者ごとに 1 ノードを作り、すべて集約へつなぐ。
- `run(config, request) -> EvalResult`: 設定を検査し (DAG であること、知らない種類・知らない評価者・知らない観点はエラー)、トポロジカル順に動かす。
  - `evaluator`: 評価者を呼び、所見のうち自分のノードの `viewpoints` に入るものだけを出す。評価者の例外は実行を止めず、`EvalResult.errors` に入れる。
  - `rebut`: 入ってきた所見を `Rebutter` Protocol (`rebut(findings, request) -> list[Verdict]`) に渡す。`Verdict(key, stance)` の stance は `support`、`deny`、`hold` のどれか。登録は評価者と同じ registry の別グループ `harness2.rebutters`。同梱の実装は無い。
  - `aggregate`: 入ってきた所見をまとめる。
- 集約の決まり:
  - 根拠の無い所見を落とす (evidence が空か、どの evidence もグラフに無いもの)。
  - 反論で deny が付いた所見を落とす。
  - 同じ `key` の所見を 1 つにまとめる (観点と evidence は和集合、重さは最も重いもの、claim は最初のもの)。
  - 重さ (critical が先)、観点の数 (多いほうが先)、key の順に並べる。
- `EvalResult`: `findings` (集約後)、`by_viewpoint` (集約前、観点ごと)、`dropped` (落とした理由ごとの数)、`errors`。

### 同梱の `rules` 評価者 (`harness2/evaluators/rules.py`)

モデルを使わない決定的な検査。`register("rules", RulesEvaluator)` は `harness2/core/evaluate.py` の末尾で行う (memory.py の file と同じ形)。観点は `cost`、`budget`、`safety`、`reproducibility`。

| 検査 | 観点 | 重さ | 根拠 |
|---|---|---|---|
| 予算の恒等式: モードごとに ROLLOUT が 4k、STAGE が 3k (summary.json の `modes.<mode>.ledger` の R / P / expected と、グラフの MODE の子の数の両方) | budget | major | MODE の span と、足りない・多い側のノード |
| ledger と models の費用の食い違い: モードの ledger の `rollout_usd` の合計と、summary.json の `models.executor[].cost_usd` の合計が、0.01 USD と 1% の大きい方を超えて違う。片方だけ 0.0 も含む | cost | major | TASK と COST のノード |
| 再試行・切り替えの多さ: 1 タスクの MODEL_SWITCH が閾値 (既定 3) を超える、STAGE の attempts が閾値 (既定 2) を超える | reproducibility | minor | 該当のノード |
| 柵で外された候補: MODEL_FENCE (fenced=False) があり、そのタスクで改善役が 1 度も成功していない | safety | major | MODEL_FENCE のノード |
| 柵で外された候補 (改善役は成功した) | safety | info | MODEL_FENCE のノード |
| エラーの集計: ERROR のノードと status=error の span を、`where` (無ければ種類) ごとに数える。同じ `where` が 2 タスク以上で出たら minor、1 タスクだけなら info | reproducibility | minor / info | 該当のノード (最大 10 件) |

- 閾値は options で変えられる (`max_switches`、`max_attempts`、`cost_tolerance_usd`、`cost_tolerance_ratio`)。
- 採点スコアは読まない (`request.scores` を使わない)。

### `harness2-evaluate` CLI (`harness2/cli/evaluate.py`)

- `harness2-evaluate --runs <dir> [--evaluators rules,pkg.mod:Attr] [--graph config.json] [--out <dir>] [--allow-scores]`。
- `--out` の既定は `<runs>/evaluation`。`findings.json` (集約後の所見、`dropped`、`errors`、読んだトレースの数) と、`findings/<viewpoint>.json` (集約前) を書く。
- `--allow-scores` が無ければ、グラフに `judge.*` を入れず、`scores` も None。あるときは `judge.*` のノードを入れ、`scores` に TASK ごとの judge のイベントの attrs を入れる。
- 終了コード: 0 (所見があっても 0)、2 (引数の誤り・設定の誤り・トレースが 1 つも無い)。
- `pyproject.toml` の `[project.scripts]` に `harness2-evaluate = "harness2.cli.evaluate:main"` を足す。

### 守ること

- 採点スコアは手法側 (core の実行・トレース) から読まない。評価者が見るのは `--allow-scores` のときだけ。
- jev や特定のソースの名前は、コード・テスト・文書のどこにも入れない。
- 外部の依存を足さない (標準ライブラリだけ)。

## Testing Decisions

- テストと ruff は WSL で回す (`python3 -m unittest discover -s tests`、`uvx ruff@0.17.1 check` と `format --check`)。
- 新しいテスト: `tests/test_graph.py`、`tests/test_evaluate.py`、`tests/test_evalgraph.py`、`tests/test_rules_evaluator.py`、`tests/test_evaluate_cli.py`、`tests/test_evaluator_contract.py`。MODE と mode は既存の `tests/test_trace_flow.py` と `tests/test_trace_models.py` に足す。エラー文のハッシュは `tests/` の該当のテストに足す。
- 全体のテスト (今 791 件) が通ること。MODE で親が変わるので、親を TASK と決め打ちしている既存のテストは、意図を変えずに直す。

## Out of Scope

- モデルを使う評価者と反論の実装、`evaluator` の役割 (PR 3)。
- Skill 生成と出力 (PR 4・5)。
- タスクをまたぐ意味の辺 (例えば「この教訓を思い出したタスク」)。recall は本文のハッシュしか持たないので、今は store と recall をつなげない。
- 評価の結果をトレースに書き戻すこと。

## Further Notes

- PR 本文は英語と日本語版。構成図 (arch-diagram) に評価者と harness2-evaluate を足す。
- 仮定 (承認で直せる):
  - (1) 観点の名前は英語の短い識別子 (`cost` など)。
  - (2) 費用の食い違いの許容は 0.01 USD と 1% の大きい方。
  - (3) `rules` は `effect` と `generalization` を扱わない (モデルの評価者に任せる)。
  - (4) 反論の登録は別の entry point グループ `harness2.rebutters`。
  - (5) ベースのロールアウトの親は TASK のまま。

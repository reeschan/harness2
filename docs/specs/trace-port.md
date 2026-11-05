# トレースの受け口 (trace port) — PR 1

元: 設計文書「harness2 自己改良ループ 設計案」の「トレース」節と「安全と既定値」節 (2026-11-03、Ryuki さん合意。Graph は「両方」)。この PR は 5 本のうちの 1 本目で、後の PR (評価の土台) がこのトレースを実行のグラフに組み立てる。

## Problem Statement

harness2 の実行結果は summary.json・Ledger・改善役の監査ディレクトリ・コネクターの record に分かれて残り、「いつ・どの順で・何が何を生んだか」の流れが残らない。評価者が読む材料にならず、2026-10-24 の検証で見つかった不具合 (ledger の金額が 0.0 のまま、など) も人が読んで見つけた。また Ledger の `usd` / `rollout_usd` が、モデルコネクターの executor の record にある費用と食い違って 0.0 になる。

## Solution

1. `harness2/core/trace.py` に、既存の 3 つのコネクター (memory.py / models.py / intake.py) と同じ作りのトレースの受け口を置く: `TraceEvent`、`TraceSink` Protocol (`emit`, `close`)、名前での登録 (`register` / `names` / `connector_name` / `resolve_factory` / `connector_for`、entry point グループ `harness2.trace_sinks`、`module:Attr`)。
2. 既定のシンク `file` は、タスクの実行ディレクトリ (summary.json と同じ場所) の `trace.jsonl` に 1 行 1 イベントで追記する。
3. 既定でオン。`HARNESS2_TRACE=0` で無効。シンクの失敗で実行は止めず、summary.json の `trace.warnings` に残す。
4. 仕込む場所: タスクの開始と終了、ベース・候補・最終のロールアウト、改善段 (INV / DOM / 合成)、ハーネスツリーの差分 (パスとハッシュだけ)、選択と振り返り、memory の recall と store、モデルの route・切り替え・柵の照合、費用、エラー。`parent_id` で親子をつなぐ。
5. Ledger の rollout の金額を、ロールアウトの実際の費用で記録する (今は常に 0.0)。
6. `harness2.testing.trace_contract` に契約テスト (偽シンク付き) を置く。
7. 文書 (`docs/TRACE.md` と日本語版) と README の表・アーキ図を更新する。

## User Stories

1. harness2 を回す人として、各タスクのディレクトリに `trace.jsonl` が残り、どの段がどの順で何秒かかり、いくら使ったかを後から辿りたい。
2. 評価者 (次の PR) を作る人として、`parent_id` で親子がつながったイベント列を読み、タスク → ロールアウト / 改善段 → ハーネスの変更 → 選択、の部分グラフを組み立てたい。
3. 外部パッケージの作者として、`harness2.trace_sinks` の entry point か `module:Attr` で自分のシンク (例えば DB やトレース基盤への送信) に差し替え、`harness2.testing.trace_contract` で契約を確かめたい。
4. 運用する人として、`HARNESS2_TRACE=0` でトレースを止められ、シンクが壊れても実行が止まらず、警告が summary.json に残ってほしい。
5. 費用を見る人として、summary.json の Ledger の `usd` / `rollout_usd` が executor の費用と一致してほしい。
6. 採点する人として、`harness2-judge` が後で採点を別イベント (`judge.*`) として同じトレースに足せる形になっていてほしい。手法側のトレースには採点のスコアが入らないこと。

## Implementation Decisions

### TraceEvent

```python
@dataclass(frozen=True)
class TraceEvent:
    run_id: str            # 1 回のタスク実行の ID (Tracer が作る uuid4 の hex)
    span_id: str           # このイベント (span) の ID (uuid4 の hex 先頭 16 文字)
    parent_id: str | None  # 親 span の ID。根 (task の span) は None
    kind: str              # イベントの種類 (下の Kind)
    ts: str                # UTC の ISO 8601 (例 "2026-11-04T05:02:51.123456+00:00")
    attrs: Mapping[str, Any]  # JSON にできる値だけ
    def to_dict(self) -> dict: ...
    @classmethod
    def from_dict(cls, d: Mapping) -> "TraceEvent": ...
```

- 1 つの span は、開始と終了の 2 イベントで表す: 同じ `span_id` で `attrs["phase"]` が `"start"` と `"end"`。終了には `status` (`"ok"` / `"error"`)、`dur_s` (秒、小数 3 桁) と、終わって分かった属性 (費用など) を入れる。
- 1 回きりの出来事 (point event) は `phase` を持たない。自分の `span_id` を持ち、`parent_id` は今の span。
- 種類は `Kind` の定数で持つ (文字列を直書きしない):

| Kind | 値 | 形 | 主な attrs |
|---|---|---|---|
| TASK | `task` | span | task_id, domain, k, seed, modes, resumed |
| ROLLOUT | `rollout` | span | label, run_id (ロールアウトの run id), role (`base` / `candidate` / `final`), mode, ok, delivered, usd, error |
| STAGE | `stage` | span | label, dim (`INV` / `DOM` / `compose` など今の Stage.dim の値), mode, kept_base, fell_back, attempts, cached, usd, error |
| TREE_DIFF | `tree.diff` | point | label, files: [{path, sha256, change: `added` / `changed` / `removed`}] |
| SELECTION | `selection` | span | mode, pool (ラベルの列), chosen_label, ok, usd, error |
| REFLECTION | `reflection` | span | mode, chosen_label, ok, usd, error |
| MEMORY_RECALL | `memory.recall` | point | mode, connector, chars, sha256 |
| MEMORY_STORE | `memory.store` | point | mode, connector, label, stored (bool), path |
| MODEL_ROUTE | `model.route` | point | role, candidates: [{runner, model, effort}] |
| MODEL_SWITCH | `model.switch` | point | role, from: {runner, model, effort}, to: {...} または null, reason |
| MODEL_FENCE | `model.fence` | point | role, fenced (bool), runners: [...], problems: [...] |
| COST | `cost` | point | mode, usd, rollout_usd, session_usd, R, P (タスク終わりにモードごと) |
| ERROR | `error` | point | where, message |

- `judge.` で始まる種類は採点側のために予約する (`JUDGE_PREFIX = "judge."`)。手法側 (core・benchmarks・cli の run 系) は `judge.*` を出さない。harness2-judge が後で足すときは、`trace.read` で読んだ ROLLOUT の `attrs.run_id` から span を見つけ、その `span_id` を `parent_id` にした `judge.score` などを同じ `run_id` で追記する。この PR では judge 側の実装はしない (形と `read` だけ用意する)。

### 本文を入れない決まり

- プロンプト・成果物・ハーネスの中身・改善役の返答は入れない。ファイルはパスと sha256 だけ。
- `Tracer` が attrs を整える: JSON にできない値は `str()`、`MAX_ATTR_CHARS = 512` 文字を超える文字列は `{"sha256": <hex>, "chars": <長さ>}` に置き換える (入れ子の dict / list の中も)。これで誤って本文を渡しても本文は出ない。
- 採点のスコア・判定・private_criteria は手法側のトレースに入れない。

### TraceSink と登録

```python
@runtime_checkable
class TraceSink(Protocol):
    def emit(self, event: TraceEvent) -> None: ...
    def close(self) -> None: ...

ENABLE_VAR = "HARNESS2_TRACE"          # "0" のときだけ無効
ENV_VAR = "HARNESS2_TRACE_SINK"        # シンクの名前。既定 "file"
DEFAULT = "file"
ENTRY_POINT_GROUP = "harness2.trace_sinks"
```

- `register(name, factory)` / `names()` / `connector_name(name=None, env=None)` / `resolve_factory(name)` / `connector_for(root, *, name=None, env=None, options=None) -> TraceSink` は memory.py と同じ順 (組み込みの登録 → entry point → `module:Attr`) と同じエラーの形 (見つからない名前は ValueError で登録名と entry point 名を並べる。呼べない対象は TypeError)。
- ファクトリは `factory(root=Path, **options)` で呼ぶ。`root` はタスクの実行ディレクトリ。`options` に `root` を入れたら ValueError。戻り値が TraceSink でなければ TypeError。
- `enabled(env=None) -> bool`: `HARNESS2_TRACE` が `"0"` のときだけ False。

### Tracer と伝わり方

```python
class Tracer:
    def __init__(self, sink: TraceSink | None, *, run_id: str | None = None,
                 sink_name: str = "", clock: Callable[[], datetime] | None = None): ...
    run_id: str
    warnings: list[str]
    def event(self, kind: str, **attrs) -> str            # point event。span_id を返す
    def span(self, kind: str, **attrs) -> ContextManager[Span]  # Span.id, Span.set(**attrs)
    def close(self) -> None
    def summary(self) -> dict   # summary.json の "trace" に入れる dict
```

- `Tracer(None)` は何もしない (無効のとき、シンクを作れなかったとき)。
- 今の Tracer と今の span は `contextvars.ContextVar` で持つ。`use(tracer)` (context manager) で設定、`current()` で取得 (無ければ何もしない Tracer)。モジュール関数 `event(kind, **attrs)` と `span(kind, **attrs)` は `current()` に委ねる。仕込む側は引数を増やさずにこれを呼ぶ。
- `span` の中で例外が出たら、終了イベントを `status="error"`・`error="<型>: <メッセージ>"` (512 文字まで) で出して、例外はそのまま投げ直す。
- スレッドプールに渡す関数は `bind(fn)` で包む (`contextvars.copy_context()` で今の Tracer と span を引き継ぐ)。recursion / schedule の `ThreadPoolExecutor` はこれを使う。
- シンクの失敗: `emit` が例外を投げたら握りつぶし、`"emit failed: <型>: <メッセージ>"` を `warnings` に足す (同じ文は 1 回だけ、最大 20 件)。`close` の失敗も同じ。シンクを作れなかったとき (`connector_for` の例外) は `Tracer(None)` にして `"sink failed: ..."` を warnings に入れる。
- `emit` は複数スレッドから呼ばれる。Tracer は `threading.Lock` で emit を直列にする。
- `open_for(task_dir, *, env=None, name=None, options=None) -> Tracer`: 無効なら `Tracer(None)` (summary は `{"enabled": False}`)、有効ならシンクを作って Tracer を返す。
- `summary()` の形: 有効なら `{"enabled": True, "sink": <名前>, "run_id": ..., "events": <出した数>, "warnings": [...]}`、無効なら `{"enabled": False}`。
- `digest(data: str | bytes) -> str`: sha256 の hex。

### file シンク

- `harness2/core/trace_file.py` の `FileSink(root: Path)`。`trace.py` の末尾で `register(DEFAULT, FileSink)` (memory.py の `register(DEFAULT, FileConnector)` と同じ)。
- 書き先は `root / "trace.jsonl"` (`FILE_NAME`)。`emit` ごとに追記モードで開き、`fcntl.flock(LOCK_EX)` を取り、`json.dumps(event.to_dict(), sort_keys=True, ensure_ascii=False)` の 1 行を書き、flush して閉じる。別プロセス (並列のタスク) からの追記でも行が混ざらない。
- `close()` は何もしない (毎回開いて閉じるため)。
- `read(path) -> list[TraceEvent]`: `trace.jsonl` を読む。壊れた行は飛ばす。judge 側と次の PR (実行のグラフ) が使う。
- 再開 (resume) で同じタスクを回したときは、同じファイルに新しい `run_id` で追記する (上書きしない)。

### 仕込み場所

| 場所 | 出すもの |
|---|---|
| `core/schedule.py` `run_task` | `open_for(work)` で Tracer を作り `use()` で囲む。TASK の span (再開で summary から復元したときも `resumed=True` で 1 回出す)。ベースのロールアウト (`_roll_bases`) の ROLLOUT (role=`base`)。モードの例外と `res.errors` に足すエラーごとに ERROR。タスクの終わりにモードごとの COST (Ledger の値)。`TaskResult` に `trace: dict` を足し、`to_dict` と `_write_summary` で summary.json の `trace` に入れる (古い summary の `trace` は今回の値で置き換える)。最後に `close()` |
| `core/recursion.py` | 改善段 1 つごとに STAGE の span (`_run_stage` 相当)。その段の採用したツリーとベースの差分を TREE_DIFF (`harness_tree.diff_summary` かツリーの比較から、パスと sha256 だけ)。候補・最終のロールアウト (`_roll`) の ROLLOUT (role=`candidate` / `final`)。プールに渡す関数は `bind` で包む |
| `core/selection.py` | `run_selection` / `trivial_selection` の SELECTION、`reflect_adoption` の REFLECTION |
| `core/memory.py` | モジュール関数 `recall` の MEMORY_RECALL (chars と sha256 のみ。本文は入れない)、`store` の MEMORY_STORE |
| `core/proposer.py` | 改善役のモデルの候補を取ったとき MODEL_ROUTE (role=`improver`)、柵の無い runner の候補を飛ばすとき MODEL_FENCE、unavailable で次の候補に移るとき MODEL_SWITCH |
| `benchmarks/intake_worktree.py` | executor の候補を取ったとき MODEL_ROUTE (role=`executor`)、`advance` で次の候補に移るとき MODEL_SWITCH |

- `cli/intake.py` の起動時の柵の照合 (`fence_report`) はタスクの外 (Tracer が無い) なので出さない。タスクの中での柵の照合は proposer の MODEL_FENCE が担う。

- 親子: TASK が根。ROLLOUT・STAGE・SELECTION・REFLECTION・COST・ERROR・MEMORY_* は TASK の子 (モードの中なら、モードの処理を囲む span があればその子)。TREE_DIFF はその STAGE の子。MODEL_* は呼ばれた時点の span の子。
- 仕込みの例外: トレースの呼び出しが原因で実行が止まってはならない。Tracer の中で例外を握るので、呼ぶ側は try で囲まない。

### Ledger の費用の修正

- 原因: `recursion._roll` / `_roll_many` と `run_parallel` / `run_sequential` のベースの記録が `ledger.rollout(label)` で金額を渡しておらず、`RunView.cost_usd` もどこでも埋まっていない。
- `core/spec.py` に `rollout_cost_usd(adapter, run_id: str, run_dir: Path) -> float` を置く: アダプターに呼べる `rollout_cost_usd(run_id, run_dir)` があればその値 (例外や負の値・数でない値は 0.0)、無ければ 0.0。BenchmarkAdapter の docstring に任意のメソッドとして書く (Protocol のメンバーには足さない。既存のアダプターが runtime_checkable の判定から外れないため)。
- `recursion._roll` / `_roll_many` と `schedule._roll_bases` (両方の経路) で `RunView(cost_usd=...)` を埋め、`ledger.rollout(label, rv.cost_usd)` で記録する。`run_parallel` / `run_sequential` のベースは `ledger.rollout(r.label, r.cost_usd)`。
- `benchmarks/intake.py` の IntakeAdapter (と worktree 版) に `rollout_cost_usd(run_id, run_dir)` を足す: executor の費用と同じ出どころ (`intake_worktree.usage(run_dir / EVENTS_NAME)` の cost) を返す。これで 1 タスクの Ledger の `rollout_usd` の合計 (全モードのロールアウト。ベースは各モードに数える今の決まりのまま) と、executor の record の費用が同じ出どころになる。
- 再開 (cached の RunView) のときは 0.0 のままでよい (今回使っていないので)。

### 契約テスト

- `harness2/testing/trace_contract.py`: `check_trace_sink(sink)` (Protocol を満たす・emit と close が呼べる) と `TraceSinkContractTests` (unittest の mixin。`make_sink(root)` を実装させ、emit した順に読めること・close を 2 回呼べること・複数スレッドから emit できること・本文の長い文字列がハッシュになって渡ること、を確かめる)。偽シンク `RecordingSink` (イベントをリストに貯める) を置き、利用者が自分のテストで Tracer の出力を確かめられるようにする。
- jev や特定のソースの名前は、コード・テスト・文書のどこにも入れない。

## Testing Decisions

- テストと ruff は WSL で回す (`fcntl` を使うため)。`python3 -m pytest -q` と `ruff check .` と `ruff format --check` 相当 (既存の CI と同じもの)。
- 新しいテスト: `tests/test_trace.py` (TraceEvent・登録・Tracer・attrs の整形・失敗時の warnings・無効化・contextvars と bind)、`tests/test_trace_contract.py` (FileSink に契約テストを当てる、偽シンクも)、`tests/test_ledger_cost.py` (偽アダプターの費用が Ledger の rollout_usd に入る、IntakeAdapter の費用が executor の record と同じ出どころ)、`tests/test_trace_flow.py` (偽アダプターと偽 Proposer で `run_task` を回し、`trace.jsonl` の親子・種類・`summary.json` の `trace`・`HARNESS2_TRACE=0` で出ないこと・シンクが壊れても完了すること)、`tests/test_trace_models.py` (memory / proposer / intake_worktree の MODEL_* と MEMORY_*)。
- 既存のテストは全部通ること。トレースの既定オンで `trace.jsonl` が増えるため、ディレクトリの中身を数えるテストがあれば、その期待を直す (テストの意図は変えない)。

## Out of Scope

- 実行のグラフの組み立て、評価者、評価グラフ、Skill 生成、出力 (PR 2〜5)。
- harness2-judge が `judge.*` を書く実装 (形と `read` だけ用意)。
- 改善役・実行役の軌跡 (opencode / codex の JSONL) の中身をトレースに取り込むこと。パスとハッシュだけ参照してよい。
- トレースの送信先の追加 (file 以外のシンクは外部パッケージ)。

## Further Notes

- PR 本文は日本語で書く (harness2 の PR は英語本文のあとに日本語版を付ける既存の決まりがある)。アーキ図 (`assets/architecture/*.d2` と SVG) の更新は arch-diagram スキルで描き直す。トレースのシンク (`harness2.trace_sinks`) を図と alt テキストに足す。
- 仮定 (承認で直せる): (1) span は開始と終了の 2 イベントで表す。(2) 長い文字列の境目は 512 文字。(3) summary.json の `trace` は無効でも `{"enabled": false}` を書く。(4) Ledger の費用はロールアウトのイベントファイルから数える (executor の record と同じ出どころ)。(5) CLI の起動時の柵の照合はタスクの外なので出さず、タスクの中の候補の除外 (proposer) で MODEL_FENCE を出す。(6) PBI は 6 個 (コア / file シンクと契約 / Ledger の費用 / schedule・recursion・selection / memory とモデル / 文書)。アーキ図は文書の PBI のあとに設計者が arch-diagram スキルで描き直す。

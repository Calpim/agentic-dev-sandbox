# AI-Driven Autonomous Development Workflow: 開発進捗と残タスク

本書は、自律開発ワークフロー基盤のこれまでの実装成果（PR #1〜PR #10）と、Antigravity チャット UI を起点とした実運用へ向けた残りの作業をまとめた引き継ぎ・進捗共有ドキュメントです。

---

## 1. 全体到達状況サマリー

ロードマップ（[`docs/ROADMAP.md`](ROADMAP.md)）に基づき、**P0（基盤整備）および P1（直列自律パイプライン）は 100% 完了**、**P2（App / BFF 並行化 & スキーマ同期）は約 85% 完了** しました。

* **テスト合格率**: **全 61 件 100% パス** (`pytest`)
* **静的解析・型チェック**: **エラー 0 件** (`ruff check`, `ruff format --check`, `mypy .`)
* **マージ済み PR**: **全 10 件** (PR #1 〜 PR #10)

```
[P0: 基盤整備] ──────── 100% (PR #1, #2, #3, #7)
[P1: 直列自律TDD] ───── 100% (PR #4, #5, #6, #8, #9)
[P2: 並行化 & RESYNC] ─  85% (PR #8, #9, #10)
[P3: 最適化 & KPI] ────  条件付き（必要に応じて追加）
```

---

## 2. マージ済み PR と実装成果一覧

| PR | タイトル | 担当領域 | 主な実装成果 |
| :---: | :--- | :---: | :--- |
| **#1** | 初期 CI 環境と基盤構築 | P0-1 | GitHub Actions、基本ディレクトリ構造、テスト自動実行環境 |
| **#2** | ルール・設計規約の整備 | P0-2 | Gherkin スタイルガイド、DDD 設計規約、権限分離ルール |
| **#3** | 仕様具体化と手動ドライラン | P0-3/4 | Example Mapping → Gherkin 仕様分解、初期 PBI ドライラン検証 |
| **#4** | オーケストレーター最小版 | P1-1 | Temporal ワークフロー基盤、耐久ステートマシン、人間ゲート待機 |
| **#5** | TDD 権限分離 & Rule Guard | P1-2/3 | Red/Green/Refactor の物理的ファイル保護、pytest ログ機械的分類 |
| **#6** | 結合検証 & PR 自動起票 | P1-6 | Integration Gate（契約検証 + Gherkin 結合）、GitHub PR 自動作成 |
| **#7** | 品質・型チェックベースライン | P0-1 | `ruff` (Lint & Format) + `mypy` 完全導入（CI 強制） |
| **#8** | Analyzer ディレクティブエンジン | P1-4 | 5 ルート自律自己修復ループ（`FIX_TEST`, `RETRY_IMPL`, `RETRY_DESIGN`, `SCHEMA_CHANGE`, `ESCALATE_HUMAN`） |
| **#9** | 受け入れテスト外側ループ | P1-5 | `pytest-bdd` による二重ループ TDD 実装（仕様適合の機械的保証） |
| **#10**| P2-5 スキーマ変更同期 & 対話型 Runner | P2-5 | スキーマ変更検知 → 軽量 HIL → スキーマ/モック再生成 → `RESYNC` 再同期ループ、Antigravity チャット対話スキル & 独立ランナー |

---

## 3. 実現済みの主要アーキテクチャ

### 3-1. 自律開発パイプラインの全体フロー

```mermaid
flowchart TD
    UserReq["Antigravity チャット UI<br>（ユーザーが PBI 要件を投入）"] --> Gate1{"Gate 1: Example Map 承認<br>(ask_question モーダル)"}
    Gate1 -->|修正指示| UserReq
    Gate1 -->|承認| Gate2{"Gate 2: OpenAPI 承認<br>(ask_question モーダル)"}
    Gate2 -->|修正指示| Gate1
    Gate2 -->|承認| Init["モック & タスク初期化<br>(Prism Contracts)"]
    
    Init --> Parallel["App / BFF 並行 TDD パイプライン<br>(内側ユニット TDD + 外側 pytest-bdd)"]
    
    Parallel --> RuleGuard{"Rule Guard<br>(機械的エラー分類)"}
    RuleGuard -->|構文エラー/自力解決| Parallel
    RuleGuard -->|仕様不整合/テスト失敗| Analyzer{"Analyzer 推論ロール<br>(Claude Opus / Gemini Pro)"}
    
    Analyzer -->|FIX_TEST / RETRY_IMPL / RETRY_DESIGN| Parallel
    Analyzer -->|SCHEMA_CHANGE| Hil{"軽量 HIL: スキーマ変更承認<br>(ask_question モーダル)"}
    
    Hil -->|承認| Resync["OpenAPI 更新 & モック再生成<br>RESYNC ディレクティブ配信"] --> Parallel
    Hil -->|却下| Abort["安全に中断"]
    
    Parallel --> IntegGate["Phase 6: 結合検証<br>(契約適合 + ビジネス Gherkin 結合)"]
    IntegGate --> GitHubPR["Phase 7: GitHub PR 起票<br>& 人間レビュー観点（3点）の提示"]
```

### 3-2. 実装されたコアモジュール

1. **二重ループ TDD & 仕様分解**:
   - [`orchestrator/activities.py`](../orchestrator/activities.py): `decompose_specs_activity`, `draft_schema_activity`, `run_tdd_cycle_activity`
   - Business Gherkin から App / BFF への責務分解と `@covers` タグによるトレーサビリティ保証。
   - `pytest-bdd` による外側受け入れテストループ（Step definition 自動バインド）。
2. **Rule Guard & Analyzer 5 ルート自己修復**:
   - [`orchestrator/guards/rule_guard.py`](../orchestrator/guards/rule_guard.py): pytest 出力を 6 カテゴリに機械的分類。
   - [`orchestrator/guards/analyzer.py`](../orchestrator/guards/analyzer.py): テスト自体の誤りを是正する `FIX_TEST`、実装方針を示す `RETRY_IMPL`、再設計を促す `RETRY_DESIGN`、契約不整合を解消する `SCHEMA_CHANGE`、エスカレーション `ESCALATE_HUMAN` を判定。
3. **P2-5 スキーマ変更自律同期（RESYNC）**:
   - [`orchestrator/workflow.py`](../orchestrator/workflow.py): 親オーケストレーターが `SCHEMA_CHANGE_REQUESTED` を検知し、軽量 HIL 待機。承認後に OpenAPI スキーマと Prism モックを更新し、両パイプラインへ `RESYNC` ディレクティブを配信して並行再同期。
4. **対話型 Antigravity 実行環境**:
   - [`orchestrator/interactive.py`](../orchestrator/interactive.py): Temporal サーバー不要で動くコールバック駆動型 E2E ランナー。
   - [`.agents/skills/workflow-runner/SKILL.md`](../.agents/skills/workflow-runner/SKILL.md): Antigravity チャット UI の `ask_question` モーダルを活用した人間ゲート対話進行プロトコル。
   - [`orchestrator/cli.py`](../orchestrator/cli.py): ターミナル対話コマンド `run-interactive`、およびシグナル送信コマンド。

---

## 4. Antigravity チャット UI 経由での利用方法

Antigravity チャットで PBI 開発を開始する手順：

1. **チャットで開始指示を入力**:
   ```text
   PBI-101 ユーザー登録およびバリデーション機能 を作って！
   ```
2. **Gate 1 (Example Map 承認)**:
   - Antigravity が `specs/example-maps/PBI-101.md` を生成。
   - 画面上に承認モーダル（`ask_question`）がポップアップ。
   - `[✔ (Recommended) 承認して Phase 2 へ進む]` または `[修正指示を入力]` を選択。
3. **Gate 2 (OpenAPI スキーマ承認)**:
   - Antigravity が `specs/api/v1/PBI-101.yaml` を生成。
   - エンドポイント・モデルの承認モーダルが表示され、承認をクリック。
4. **Phase 3/4 並行開発 & 軽量 HIL**:
   - App と BFF が並行して TDD 実装を自律進行。
   - Analyzer からスキーマ変更要求があった場合、軽量 HIL モーダルが表示され、ワンクリックで承認（RESYNC）可能。
5. **Phase 6/7 結合検証 & PR 起票**:
   - 結合テストが完了し、GitHub PR が起票される。
   - **「人間に特にここをレビューしてほしい箇所（3点）」** と共に完了報告が表示される。

---

## 5. 残りの作業（Next Actions）

### ① 【直近の最優先】Antigravity 対話型 UI 経由での実 PBI 開発 E2E ドライラン
* **目的**: 
  P2 の完了判定条件である **「App と BFF にまたがる PBI 3件以上が、並行実行で PR まで到達する」** を実証する。
* **タスク**:
  1. 実際の業務に近いシナリオ（例: `PBI-101 ユーザー情報取得`, `PBI-102 カート商品追加`, `PBI-103 決済処理`）を順次 Antigravity チャットに投入。
  2. チャット上の `ask_question` モーダル応答だけで PR まで完走できるか、操作性と自律修復精度を検証。
  3. 各 PBI 実行時のトークン消費量・完了時間・Analyzer 介入履歴を記録。

### ② P2-6 結合検証の差し戻しループ微調整
* **目的**: 
  Phase 6 結合テストでエラーが発生した際、Analyzer が「App 側の実装不備」か「BFF 側のマッピング不備」かを特定し、対象パイプラインへピンポイントに差し戻すディレクティブ連携を精緻化する。

### ③ P3 最適化（実運用データ・KPI に応じて条件付き導入）
ロードマップに基づき、トリガー条件を満たした施策のみ順次導入：
* **意味的カバレッジ検査**: `@covers` は満たしているが、仕様漏れが発生する場合に LLM 照合を追加。
* **KPI ダッシュボード**: `audit.jsonl` を集計し、自律修復成功率やリードタイム推移を可視化。

---

## 6. 主要ファイルリファレンス

* **ロードマップ**: [`docs/ROADMAP.md`](ROADMAP.md)
* **詳細仕様書**: [`docs/WORKFLOW.md`](WORKFLOW.md)
* **オーケストレーター**: [`orchestrator/workflow.py`](../orchestrator/workflow.py)
* **対話型ランナー**: [`orchestrator/interactive.py`](../orchestrator/interactive.py)
* **CLI インターフェース**: [`orchestrator/cli.py`](../orchestrator/cli.py)
* **Analyzer エンジン**: [`orchestrator/guards/analyzer.py`](../orchestrator/guards/analyzer.py)
* **Rule Guard**: [`orchestrator/guards/rule_guard.py`](../orchestrator/guards/rule_guard.py)
* **Antigravity スキル**: [`.agents/skills/workflow-runner/SKILL.md`](../.agents/skills/workflow-runner/SKILL.md)

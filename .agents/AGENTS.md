# Project Agents Policy: AI-Driven Autonomous Development (v3.0)

このリポジトリは、**Example Mapping × BDD × DDD × TDD** に基づく自律エージェント開発パイプラインのワークスペースです。
各エージェントは Temporal オーケストレーターの指示（Directive）に従って動作します。

## 1. ディレクトリ責務
- `specs/example-maps/`: PBI の受け入れ条件と具体例（人間ゲート 1 の合意成果物）
- `specs/features/business/`: 宣言的・日本語のビジネス Gherkin
- `specs/features/app/`: App 層シナリオ（`@covers:BIZ-xxx` 必須）
- `specs/features/bff/`: BFF 層シナリオ（`@covers:BIZ-xxx` 必須）
- `specs/api/v1/`: OpenAPI スキーマ（人間ゲート 2 の合意成果物）
- `tasks/{PBI-ID}/`: 実行コンテキスト
  - `state.json`: 正となる状態（オーケストレーター専用・エージェントは読み取りのみ）
  - `app/plan.md`: App Agent 用ビュー
  - `bff/plan.md`: BFF Agent 用ビュー
  - `audit.jsonl`: 監査ログ（追記専用）

## 2. 厳格なルール
1. **スキーマ保護**: `specs/api/**` のファイルを直接編集してはならない。変更が必要な場合は差分提案（PATCH）としてオーケストレーターへ報告すること。
2. **TDD分離（強制）**:
   - **Red フェーズ**: テストコードのみ作成・編集可。プロダクションコードの編集は禁止。
   - **Green / Refactor フェーズ**: プロダクションコードのみ編集可。テストコードの編集は禁止（テスト緩和の防止）。
3. **最新状態の遵守**: 常に自身の `plan.md` に記載された `Current Directive` を最優先で遂行すること。

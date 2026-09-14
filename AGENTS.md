# AI VTuber 專案指引

## 接手入口

- Phase 5 或後續施工前，先完整閱讀 `docs/phase-5-handoff-to-codex.md`，再閱讀
  `docs/phase-5-result.md` 與目前任務直接相關的技術文件。
- 涉及 Phase 5 orchestration、實機 smoke、一小時驗收或 Phase 5 完成判定時，
  使用 repo skill `$continue-ai-vtuber-phase5`。
- 對使用者的狀態摘要、驗收報告與新增專案文件使用繁體中文；程式識別字與外部工具
  的原始欄位名稱保持原樣。

## 不可破壞的邊界

- 不讀出、顯示、記錄、提交或送給 LLM 任何 `.local/secrets/` 內容、OAuth token、
  VTS token 或 llama-server API key。檢查前置條件時只確認路徑是否存在。
- `.local/`、模型、runtime、`config/actions.local.yaml` 與實機 benchmark 都維持
  本機狀態，不可強制加入 Git。
- 目前只允許 eSpeak NG。MeloTTS 中文 checkpoint 或其他真人／角色聲音在使用者完成
  權利確認前不得下載、啟用或直播使用。
- 離線測試通過不等於 Phase 5 實機驗收通過。只有正式報告同時滿足所有一小時條件，
  才能將 Phase 5 標示為完成。
- 除非使用者另行要求，不進入 Phase 6（OBS）或 Phase 7，也不重做已驗證的 Phase 1～4。

## 基本驗證

修改 Python 程式後，至少執行：

```powershell
.\.venv\Scripts\python.exe -m pytest --tb=short
.\.venv\Scripts\python.exe -m compileall -q src tests
.\.venv\Scripts\python.exe -m pip check
git diff --check
```

任何實機結果都要保存成正式 JSON 與繁體中文 Markdown 報告；失敗、受阻、逾時與未量測
必須如實保留，不能以 mock 或手工修改報告冒充成功。

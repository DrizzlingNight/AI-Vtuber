# AI VTuber 專案指引

## 接手入口

- 接手前先閱讀 `PROJECT_BRIEF.md` 與目前任務直接相關的技術文件；重查 Phase 5 時再完整
  閱讀 `docs/phase-5-handoff-to-codex.md`、`docs/phase-5-result.md` 與正式實機報告。
- 核心 orchestration 的啟停、整合 smoke、取消收尾、故障隔離與量測使用 repo skill
  `$operate-ai-vtuber-orchestration`。skill 只負責可重用操作方法，不定義 Phase 完成條件。
- VTube Studio 資源校正、Twitch 帳號／收發操作與本地 LLM benchmark，分別使用
  `$calibrate-ai-vtuber-vts`、`$operate-ai-vtuber-twitch`、`$benchmark-ai-vtuber-llm`。
- Twitch skill 的 inbound 責任截止於事件交給 message sink；outbound 責任則從發送請求
  進入 Twitch adapter 到 Helix 回傳結果。訊息進入內部佇列後的排程、LLM、VTS、TTS、取消
  與收尾屬於 orchestration skill。完整鏈路以 orchestration skill 為主，只在帳號、授權、
  收發或重連問題上另載入 Twitch skill。
- 對使用者的狀態摘要、驗收報告與新增專案文件使用繁體中文；程式識別字與外部工具
  的原始欄位名稱保持原樣。

## 不可破壞的邊界

- 不讀出、顯示、記錄、提交或送給 LLM 任何 `.local/secrets/` 內容、OAuth token、
  VTS token 或 llama-server API key。檢查前置條件時只確認路徑是否存在。
- `.local/`、模型、runtime、`config/actions.local.yaml` 與實機 benchmark 都維持
  本機狀態，不可強制加入 Git。
- 目前只允許 eSpeak NG。MeloTTS 中文 checkpoint 或其他真人／角色聲音在使用者完成
  權利確認前不得下載、啟用或直播使用。
- 離線測試通過不等於任何實機或直播驗收通過。Phase 專屬的輪數、時數、執行順序、
  指標門檻與完成狀態只以 `PROJECT_BRIEF.md`、對應 Phase 文件及正式報告為準，不寫進
  通用 skill，也不把某一 Phase 的標準自動套用到下一個 Phase。
- 核心互動鏈路報告若沒有 OBS、編碼、RTMP、掉幀、bitrate 或觀眾端影音證據，就不能
  宣稱直播穩定性通過。
- 未經使用者針對當次操作明確授權，不啟動 OBS 推流或公開直播；也不提前加入尚未進入
  範圍的後續 Phase 功能。

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

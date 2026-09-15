---
name: operate-ai-vtuber-orchestration
description: 啟動、停止、驗證、除錯或交接此程式庫的 AI VTuber 核心 orchestration。當工作涉及 Twitch-to-LLM-to-VTS-to-TTS 管線、run、整合 smoke、佇列、狀態機、取消收尾、故障隔離或整合報告時使用；不適用於單一 Twitch、VTS、LLM 元件，亦不自行定義任何 Phase 的完成條件。
---

# 操作 AI VTuber 核心 Orchestration

安全地操作 Twitch、LLM、VTS、TTS 與 Twitch 回覆的核心互動管線；把可重用的操作方法
與特定 Phase 的里程碑、時數、輪數和完成判定分開。

## 載入必要情境

1. 從程式庫根目錄檢查 `git status --short --branch` 與近期 Git 紀錄。
2. 閱讀 README 的核心 orchestration 操作說明，以及 `docs/phase-5-orchestration.md` 中仍適用
   的架構、取消與故障隔離設計。
3. 若任務要求判定某個 Phase 是否完成，另讀 `PROJECT_BRIEF.md`、該 Phase 的驗收文件與
   本次正式報告。Phase 的輪數、時數、指標門檻與當前狀態不得從本 skill 推導。
4. 只有在修改或診斷程式時，才閱讀 `src/ai_vtuber/orchestration/`、CLI 組裝及直接相關測試。

## 維持核心不變條件

- Twitch EventSub 收訊保持非阻塞，不受 LLM、VTS 或 TTS 故障影響。
- 訊息佇列保持容量上限，並正確處理 TTL、優先權與 cooldown。
- 同一時間只允許一個主要 TTS 合成／播放工作。被搶占的原生工作必須確實停止或進入
  明確 quarantine，才能開始新工作。
- Twitch 回覆的 Helix request 一旦越過 commit point，就不得撤回、取消、重送或假裝未送出。
- 排除由回覆帳號自身送出的訊息，避免形成自我回覆迴圈。
- speech／action 只有通過 schema、繁體中文、emotion 與 action 驗證後才能送往下游；
  action 必須同時存在於允許清單與目前模型的本機 mapping。
- 不猜測 VTS 資源；缺少的映射透過 `missing_resources` 明確呈現。
- 成功、取消、逾時、斷線、模型重載或關閉後，都要清理音訊、字幕、MouthOpen、reaction、
  狀態與被隔離的工作。
- TTS 故障時可保留已通過驗證且未過期的安全文字回覆；任何下游故障不得中止 Twitch 收訊。

## 保護本機資料與授權

- 只確認 Twitch／VTS token、DPAPI blob 與 llama-server API key 是否存在，不得開啟、輸出、
  寫入提示詞、報告、commit 或對話。
- `.local/`、模型權重、`config/actions.local.yaml`、音訊與實機報告維持不受 Git 追蹤。
- 目前只使用 eSpeak NG；未完成聲音權利審查前，不下載或啟用其他真人／角色聲音。
- 操作核心管線不等於取得開台授權。未經使用者當次明確要求，不啟動 OBS、RTMP 或公開直播。

## 操作與量測

1. 依 README 與目前設定確認必要本機資源、VTube Studio／模型、llama-server、eSpeak NG 及
   明確指定的 Twitch 頻道；不得用範例映射、假 token 或舊報告越過前置檢查。
2. 使用現有 `run` 或整合 smoke 命令。若命令名稱仍含歷史 Phase 編號，它只是現有 CLI
   介面；訊息數、執行時間與通過門檻仍由使用者或對應 Phase 文件提供。
3. 正式連線前完成不送往 Twitch／VTS 的本機 LLM 結構化預熱。情緒只允許 `neutral` 或
   已有本機 `emotion_actions` 映射者。
4. 需要外部測試訊息時，依 `$operate-ai-vtuber-twitch` 驗證獨立測試帳號。測試內容、數量、
   節奏與持續時間由呼叫端或 Phase 驗收文件決定，本 skill 不固定這些數值。
5. 出現 blocked、failed、timed_out 或網路結果不確定時保留原狀態與失敗類型，不用 mock、
   手工修改或重試不確定的外部 request 來製造成功。
6. 實機執行同時保留 JSON 與繁體中文 Markdown，記錄實際輪次、延遲、資源、VTS 狀態、
   輸入驅動與錯誤；沒有量測的欄位明確標示未量測。

## Phase 與驗收邊界

- 本 skill 負責「怎麼安全執行、診斷與留下證據」，不負責「做到多少才算某個 Phase 完成」。
- Phase 專屬的輪數、時數、執行順序、指標門檻與完成聲明只寫在 `PROJECT_BRIEF.md`、對應
  Phase 文件和正式報告。
- 歷史 `phase5-smoke` 與 `phase5_hour_acceptance` 可依 Phase 5 文件重跑或稽核，但不得
  自動套用為後續 Phase 的驗收規格。
- 核心鏈路證據不包含 OBS、編碼、RTMP、掉幀、bitrate 或觀眾端影音；沒有相應證據時，
  不得宣稱直播穩定性通過。

## 回報與驗證

分開回報程式同步、離線測試、核心鏈路實機結果及任何 Phase／直播驗收結果。修改 Python
後執行專案 `AGENTS.md` 的完整基本驗證；只改文件或 skill 時至少執行 `git diff --check`
並驗證本 skill。

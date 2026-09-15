---
name: operate-ai-vtuber-orchestration
description: 執行或診斷 Twitch 訊息進入內部佇列後，跨 LLM、VTS、TTS 到 Twitch 回覆的核心管線。當工作涉及 run、整合 smoke、佇列排程、狀態機、取消收尾、故障隔離或整合報告時使用；單獨的 Twitch 帳號、授權、收發或重連問題改用 operate-ai-vtuber-twitch，且本 skill 不定義任何 Phase 的完成條件。
---

# 操作 AI VTuber 核心 Orchestration

協調已進入內部訊息佇列的 Twitch 輸入，完成 LLM、VTS、TTS、Twitch 回覆與狀態收尾；
不接管 Twitch 帳號、授權、EventSub 或 Helix transport 的單一元件操作。

## 責任與交接點

- `$operate-ai-vtuber-twitch` 負責 Twitch 邊界：帳號與 token、scope、EventSub 連線、事件驗證、
  測試帳號、Helix 發送及重連。它把通過驗證的 `TwitchChatMessage` 交給 message sink 後，
  inbound 責任才轉入本 skill。
- 本 skill 負責訊息進入內部佇列後的優先權、TTL、cooldown、LLM 回應、VTS 動作、TTS 播放、
  回覆決策、取消與收尾。送出請求交給 Twitch adapter 後，transport 結果再依 Twitch skill
  的規則處理。
- 任務只檢查 Twitch 能否授權、收訊、發訊或重連時，不使用本 skill。任務需要觀察兩個以上
  的下游元件或完整訊息生命週期時，以本 skill 為主；只有碰到 Twitch 邊界時才載入 Twitch skill。

## 載入必要情境

1. 從程式庫根目錄檢查 `git status --short --branch` 與近期 Git 紀錄。
2. 閱讀 README 的核心 orchestration 操作說明，以及 `docs/phase-5-orchestration.md` 中仍適用
   的架構、取消與故障隔離設計。
3. 若任務要求判定某個 Phase 是否完成，另讀 `PROJECT_BRIEF.md`、該 Phase 的驗收文件與
   本次正式報告。Phase 的輪數、時數、指標門檻與當前狀態不得從本 skill 推導。
4. 只有在修改或診斷程式時，才閱讀 `src/ai_vtuber/orchestration/`、CLI 組裝及直接相關測試。

## 維持核心不變條件

- 內部 consumer 不得阻塞 Twitch message sink；LLM、VTS 或 TTS 故障不能拖停輸入接收。
- 訊息佇列保持容量上限，並正確處理 TTL、優先權與 cooldown。
- 同一時間只允許一個主要 TTS 合成／播放工作。被搶占的原生工作必須確實停止或進入
  明確 quarantine，才能開始新工作。
- Twitch adapter 一旦回報發送已越過 commit point 或結果不確定，orchestration 不得因取消、
  逾時或重跑而再次送出同一則回覆。
- speech／action 只有通過 schema、繁體中文、emotion 與 action 驗證後才能送往下游；
  action 必須同時存在於允許清單與目前模型的本機 mapping。
- 不猜測 VTS 資源；缺少的映射透過 `missing_resources` 明確呈現。
- 成功、取消、逾時、斷線、模型重載或關閉後，都要清理音訊、字幕、MouthOpen、reaction、
  狀態與被隔離的工作。
- TTS 故障時可保留已通過驗證且未過期的安全文字回覆；任何下游故障不得中止 Twitch 收訊。

## 保護本機資料與授權

專案層級的 secret、本機資源、聲音權利與開台授權以 `AGENTS.md` 為準；本 skill 不重複定義
各元件的 token 操作。執行核心管線不等於取得 OBS、RTMP 或公開直播授權。

## 操作與量測

1. 依 README 與目前設定確認 VTube Studio／模型、llama-server、eSpeak NG 及明確指定的
   Twitch 頻道。Twitch 身分、授權或連線需要檢查時交由 `$operate-ai-vtuber-twitch`；不得用
   範例映射、假 token 或舊報告越過前置檢查。
2. 使用現有 `run` 或整合 smoke 命令。若命令名稱仍含歷史 Phase 編號，它只是現有 CLI
   介面；訊息數、執行時間與通過門檻仍由使用者或對應 Phase 文件提供。
3. 正式連線前完成不送往 Twitch／VTS 的本機 LLM 結構化預熱。情緒只允許 `neutral` 或
   已有本機 `emotion_actions` 映射者。
4. 需要外部測試訊息時，將測試帳號身分與聊天室發送交由 `$operate-ai-vtuber-twitch`；本 skill
   只消費已確認的輸入並量測後續生命週期。內容、數量、節奏與持續時間由呼叫端或 Phase
   驗收文件決定。
5. 出現 blocked、failed、timed_out 或外部結果不確定時保留原狀態與失敗類型，不用 mock、
   手工修改或擅自重試來製造成功；外部 request 的重試規則由對應元件 skill 決定。
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

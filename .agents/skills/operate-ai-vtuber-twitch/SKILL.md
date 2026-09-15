---
name: operate-ai-vtuber-twitch
description: 操作、設定、驗證、除錯或交接此程式庫的 Twitch Device Code Grant、DPAPI token、EventSub 收訊與 Helix 聊天發送。當工作涉及 twitch-auth、twitch-test-sender-auth、twitch-validate、twitch-listen、twitch-send、twitch-smoke、測試帳號、scope、token 更新或重連時使用；不適用於 VTS、LLM、TTS、整體 orchestration 或 Phase 完成判定。
---

# 操作 AI VTuber 的 Twitch 連線

分開處理主帳號、外部測試帳號、EventSub 收訊與 Helix 發送，保留真實聊天室副作用與
不確定網路結果的邊界。

## 載入必要情境

1. 從程式庫根目錄檢查 `git status --short --branch`。
2. 閱讀 README 的「Twitch Developer Console 設定」、「Twitch 授權」與「Twitch 收發
   與 smoke test」。只有在修改或診斷程式時，才閱讀 `src/ai_vtuber/twitch/`、CLI 組裝
   與直接相關測試。
3. Twitch scope、API 或限制可能改變；相關問題以當下 Twitch 官方文件重新確認，不用
   歷史文件猜測現況。

## 保護授權資料

- 只確認 `.local/secrets/twitch-token.bin` 與
  `.local/secrets/twitch-test-sender-token.bin` 是否存在；不得開啟、解密、顯示或複製
  DPAPI blob、access token、refresh token、Authorization header 或一次性 Device Code。
- `TWITCH_CLIENT_ID` 是公開應用程式識別碼；本專案的 Public Device Code Grant 不需要
  Client Secret，不要建立或保存 Client Secret。
- token、授權物件與原始 API payload 不得進入 LLM、一般日誌、報告、Git 或對話。
- 授權網址與一次性代碼只留在使用者正在操作的終端；不要擷取、轉述或持久化。

## 帳號角色

- 主帳號用 `twitch-auth`／`twitch-validate`，負責訂閱自己的聊天室及送出 AI 回覆。
- 外部測試帳號用 `twitch-test-sender-auth`／`twitch-test-sender-validate`，使用獨立 DPAPI
  檔案且身分必須與主帳號不同；不得覆蓋或共用主帳號 token。
- 外部測試帳號只向使用者明確指定的測試頻道送出受控訊息。它是跨階段可重用的測試
  輸入來源，不是正式 bot 帳號，也不表示已取得開台或控制帳號其他功能的權限。
- 訊息資料、發送數量、速率、持續時間及 Phase 通過門檻屬於呼叫端或 Phase 驗收文件，
  不在本 skill 固定。

## 操作、診斷與外部副作用

1. 先執行對應帳號的 validate，核對登入身分、有效期及實際 scopes。既有設計只要求
   `user:read:chat` 與 `user:write:chat`；未經使用者要求不要擴張權限。
2. token 缺失、失效或使用者要求重新授權時，才執行對應 auth，並等使用者在官方 Twitch
   頁面完成授權。
3. 驗證收訊時使用有限訊息數，確認外層 `metadata.message_id` 去重、主帳號自身訊息不入列；
   受控自動輸入模式還必須排除非指定測試帳號的訊息。
4. `twitch-send`、`twitch-smoke` 及任何自動測試驅動都會真的向公開聊天室發訊息。只有任務
   明確要求，且已核對發送帳號、頻道與訊息來源時才能執行；關台後的聊天室仍可能公開可見。
5. `is_sent: false` 時保留 `drop_reason`。Helix request 已送出但結果不確定時不得自動重試，
   避免重複訊息。
6. 一般斷線可依既有退避重連並重建訂閱；`session_reconnect` 必須先在 Twitch 指定 URL
   收到 Welcome，再關閉舊連線，且不得重複訂閱。不要宣稱斷線期間事件一定會補送。

## 回報與驗證

分別回報帳號角色、授權、收訊、發送、自身訊息排除、指定測試帳號過濾與重連結果；清楚
標示 `not run`、`blocked`、`failed` 及網路結果不確定。實機紀錄可以保存測試帳號登入名稱
與發送計數，但不得包含聊天室敏感內容或任何 token。

修改 Python 後，執行專案 `AGENTS.md` 的完整基本驗證；只改文件或 skill 時至少執行
`git diff --check`，並驗證本 skill。

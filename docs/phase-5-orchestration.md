# Phase 5 AI VTuber 整合

更新日期：2026-09-07

## 狀態與範圍

**整合程式與離線測試不是 Phase 5 實機驗收完成的證明。** 單輪、連續鏈路與至少一小時
測試頻道驗收分開判定，實際結果見 [重新執行紀錄](phase-5-work-log.md)。

本階段沿用既有 Twitch Device Code Grant／DPAPI／EventSub／Helix、
Gemma 4 12B／llama.cpp、VTS client／inventory／ActionExecutor，以及
eSpeak NG／PCM／WAV／PortAudio／字幕與 MouthOpen 播放佇列。
未加入 OBS、Phase 6 記憶或內容過濾，也未加入 Phase 7 語音模型或 viseme。

MeloTTS 中文 checkpoint 的 speaker、訓練資料及聲音權利尚未證實，維持不下載、
不啟用。eSpeak NG `cmn` 仍為既有 CPU 規則式合成基線。

## 一、實際執行順序

```text
Twitch EventSub 接收到外部訊息
  → 同步入列，不等待模型、語音或動作
  → thinking：呼叫既有 llama.cpp client
  → validating：再次驗證既有 schema、繁中與 emotion/action 白名單
  → 合成已驗證的 speech；若合成失敗，保留安全文字回覆
  → 再次檢查訊息是否過期
  → acting：等待 VTS 確認動作已準備完成
  → 保留短暫動作前導時間，再次檢查 TTL
  → speaking：單一 PortAudio 播放，同步字幕與 MouthOpen
  → 停止未完成音訊、嘴型復位、字幕清空、動作還原
  → Helix 發送已驗證的 chat_reply
  → cooldown → idle
```

`react_only` 不會合成語音或發送文字；完成動作與收尾後回到冷卻。
`ignore`、未通過驗證的輸出與 LLM 故障不會進入動作、TTS 或發送流程。

`thinking` 等狀態名稱是程式識別字；命令日誌另外保存繁體中文狀態說明。
TTS 合成期間尚未進入 `speaking`，因為此時並未出聲。

## 二、訊息佇列

| 行為 | 規則 |
|---|---|
| 容量 | 預設最多 32 則等待訊息，實體儲存同樣有上限 |
| TTL | 預設 30 秒，從訊息進入本機佇列起算 |
| 使用者冷卻 | 依穩定使用者 ID，預設 2 秒；不依顯示名稱 |
| 回應冷卻 | 每輪後預設 5 秒；Helix 仍保留既有發送節流 |
| 優先權 | 較高優先先處理，同優先以入列次序排序 |
| 滿載 | 新訊息若不低於最低優先項目，取代該層最舊訊息 |
| 過期 | 入列、出列、完成模型／合成／動作準備時檢查 |
| 冷卻身分表 | 也有容量限制；不會因大量不同帳號而無限增長 |
| 關閉 | 清空等待項目，另行計數，不冒充已處理 |

目前高優先事件只使用 Twitch 已提供的可信 `channel.chat.message.message_type`：
`channel_points_highlighted`、`power_ups_gigantified_emote`。
不從聊天室文字猜測管理員身分，不新增 Raid、訂閱或獨立 Channel Points 訂閱。

EventSub 的去重、自身訊息排除與重新連線邏輯繼續沿用。LLM 只取得固定角色提示與
單則聊天文字，不會取得 Twitch auth、使用者完整資料、VTS 內部 ID 或 API 憑證。

## 三、取消與收尾

高於目前 turn 的優先項目可中斷 LLM、TTS 合成等待、動作或播放，但新工作必須等舊工作
安全收尾。重複取消不能提前釋放合成鎖，也不能中止 MouthOpen 復位或字幕清空。

eSpeak 仍使用相同執行檔與 CPU 合成，只調整背景 I/O 的生命週期。取消時不把執行緒
假裝成已停止：先等待有限收尾時間，若仍未結束就隔離，拒絕新合成直到舊工作實際完成。
既有 eSpeak 子程序執行逾時仍保留。高優先反應不等於零等待時間。

播放佇列仍只有一個 worker，音量包絡運算移到背景執行緒，避免大量 PCM 計算佔住
EventSub 的事件迴圈。取消、清空與關閉若遇到清理錯誤，會明確回報，不再吞掉錯誤。
清理尚未完成時，語音路徑暫停接受新播放，避免音訊重疊。

PortAudio 開始等待預設最多 5 秒，停止確認預設最多 2 秒，播放亦受 PCM 時長加停止
寬限時間限制。原生 I/O 使用獨立背景執行緒，不阻塞 asyncio 預設執行緒池的關閉；
這不代表可以強制中止作業系統中的原生呼叫。若裝置尚未確認停止，報告標示失敗，
同一個輸出物件拒絕後續播放，直到舊工作確實結束。沒有啟動第二個播放佇列來繞過隔離。

`--timeout` 限制等待訊息與處理輪次的期間；期限到後仍執行有界安全收尾，
不是立刻丟棄背景工作。實機前置檢查與首次授權／連線不包含在此期間。

送出 Twitch 文字的不可撤回點位於 `chat_reply` 發送開始：

- 此前收到高優先事件，可略過舊回覆。
- 發送開始後，無論插隊、測試逾時或關閉，都等待該次 request 的有限結果，不因取消重送。
- 已發出但網路結果不確定的 request 不會自動重試。

TTS 故障保留安全文字不代表無條件回覆過時訊息；若尚未開始播放且 TTL 已到期，
仍應略過舊的文字。

## 四、VTS 動作與情緒

既有 `ActionExecutor.execute()` 增加可選的準備通知、釋放事件及強度參數。
舊有命令不傳這些參數時，仍使用原本的持續時間與完整強度。

- 嘴型與連續動作仍只透過本機語意映射，不寫死模型參數 ID。
- 準備通知必須等到 VTS 操作成功回傳，不能只是建立背景工作就算準備完成。
- 表情可以保持到語音結束，最後恢復原狀；連續動作強度使用已驗證的 intensity。
- 若還原時斷線，記住本執行器曾修改的狀態，後續動作前優先嘗試還原。
- 動作還原與嘴型注入前確認原模型；不能把上一個模型的控制值送給另一個模型。
- 嘴型操作有獨立等待上限；VTS 故障不阻塞 Twitch 收訊，可保留音訊與字幕並記錄降級。

`emotion_actions` 的 key 必須是既有 emotion 白名單，value 必須同時存在於
本機 actions mapping 與 LLM 核准的 action 白名單。明確 action 優先於情緒對應。
預設不猜測 NightRain 的情緒資源；沒有核准映射時明確標示未完成反應。

保留 NightRain 眼睛中性值 `0.0833`、閉眼預加重 `-0.02` 的既有程式與測例，
不重新生成或覆蓋 `actions.local.yaml`。該本機映射是否可用，須由實機前置檢查確認。

## 五、設定

```yaml
orchestration:
  message_queue_size: 32
  message_ttl_seconds: 30.0
  per_user_cooldown_seconds: 2.0
  response_cooldown_seconds: 5.0
  action_lead_seconds: 0.15
  cleanup_timeout_seconds: 5.0
  vts_operation_timeout_seconds: 3.0
  high_priority_message_types:
    - channel_points_highlighted
    - power_ups_gigantified_emote
  emotion_actions: {}
```

不改動 Gemma 的 4096 context、28 GPU layers、12 threads／12 batch threads、
單一 server slot、關閉 thinking 或 eSpeak CPU 設定。

## 六、命令與頻道邊界

所有命令在包含目前程式的工作目錄執行。先準備該目錄內受 Git 忽略的既有校正、
授權與 runtime，再啟動既有 `llm-serve`，保持 VTS 與 NightRain 開啟。
不能拿範例映射假裝成實際 NightRain 校正，也不能用假 token 越過前置檢查。

持續運行：

```powershell
.\.venv\Scripts\python.exe -m ai_vtuber run `
  --test-channel "已授權的測試頻道登入名稱"
```

單輪完整鏈路：

```powershell
.\.venv\Scripts\python.exe -m ai_vtuber phase5-smoke `
  --test-channel "已授權的測試頻道登入名稱" --messages 1 --timeout 600
```

連續短測試：

```powershell
.\.venv\Scripts\python.exe -m ai_vtuber phase5-smoke `
  --test-channel "已授權的測試頻道登入名稱" --messages 5 --timeout 900
```

至少一小時驗收可使用較多輪次並把測試訊息分散在整個期間：

```powershell
.\.venv\Scripts\python.exe -m ai_vtuber phase5-smoke `
  --test-channel "已授權的測試頻道登入名稱" --messages 60 --timeout 4200
```

此命令不是自動發訊器。由另一個帳號在測試聊天室分批發訊，必須確認實際運行已達
3600 秒且完整鏈路通過，才有一小時驗收證據。若 60 輪很快完成，仍只算短測試。
Phase 6 的 4 至 8 小時 soak test 不在本階段執行。

`--test-channel` 必須與現有授權身份相同，不符合則拒絕訂閱與發送。Twitch 關台後的
聊天室仍可能公開可見；程式不會把它變成私人聊天室。沒有 OBS、自動開播或主動測試開場
訊息。未指定測試頻道時，smoke 只記錄阻塞，不會自動選擇頻道。

## 七、繁體中文報告與通過標準

每次 smoke 預設保存兩份檔案，各自以暫存檔原子替換：

```text
.local\benchmarks\phase5-smoke-時間.json
.local\benchmarks\phase5-smoke-時間.md
```

JSON 保留機器可讀欄位；Markdown 用繁體中文記錄限制、結果、每輪狀態、動作、
佇列統計、延遲、RAM、VRAM 及保留的模型設定。前置條件不足也會保存報告，所有未取得的
量測均明確標示「未量測」，不以 0 或舊 benchmark 數字代替。

| 指標 | 定義 |
|---|---|
| 收到至首 token | 本機入列時間到 llama.cpp 第一段內容 |
| 收到至完整決策 | 本機入列時間到既有 schema／繁中／白名單再次驗證完成 |
| 收到至開始發聲 | 本機入列到首個 PCM block 送入 PortAudio；不是硬體聲學量測 |
| 收到至播放完成 | 音訊實際完成的事件時間，不把嘴型與字幕清理延遲算入 |
| RAM | 系統用量基準、峰值、增量及 llama-server 工作集峰值 |
| VRAM | 整體顯示卡用量，不宣稱全部是 LLM 或 TTS 使用 |
| VTS | 連接埠全程可連線，加上實際動作／嘴型操作是否有錯誤 |

短 smoke 使用能觸發 `reply` 的專用測試訊息。只有指定輪次全部完成文字與語音回覆、
嘴型未降級、必要量測完整且時間順序有效，才回傳通過。合法的 ignore 或 react_only
另有離線測試，不能拿沒有出聲的結果冒充本項完整鏈路成功。

`phase5_hour_acceptance` 另外要求至少兩輪、完整鏈路通過、實際運行至少 3600 秒。
缺少條件一律標示尚未完成；成功的短 smoke 不會把整個 Phase 5 標為完成。

長駐 `run` 只保留最近 100 輪結果及 256 筆狀態轉移；smoke 限制最多 1000 輪及
7200 秒。這些是本階段必要的容量界線，不是新增長期記憶或監控平台。

## 八、安全資料界線與離線證據

OAuth access token、refresh token、DPAPI 儲存內容及 llama-server API key 不會進入
模型提示、benchmark 或繁體中文紀錄。報告只保存錯誤類型，不保存外部服務的原始診斷或
被拒絕的模型輸出；報告路徑也不能覆蓋設定、盤點、執行狀態或授權檔。

離線測試除了個別元件 mock，也把真正的 EventSub、LLM client、ActionExecutor、
SpeechPlaybackQueue 與 Helix 串起來，只替換外部網路及實體音訊裝置：

```powershell
.\.venv\Scripts\python.exe -m pytest
```

離線通過代表程式行為在這些可重現情境中正確，不代表已連上實際 Twitch、Gemma、
NightRain 或喇叭。實機是否完成，須以本輪執行紀錄與實機報告為準。

# AI VTuber 本地端專案研究與交接文件

更新日期：2026-09-04
專案目錄：`F:\user\Documents\Workspace\AI Vtuber`

## 1. 原始目標

建立一套以 Windows 本地端運作為主的 AI VTuber 系統，讓 AI 可以：

1. 自動控制 VTube Studio 內的 Live2D 人物模型。
2. 讀取 Twitch 聊天室訊息，理解觀眾正在說什麼。
3. 依訊息內容決定是否回應，以及要呈現的情緒與動作。
4. 以 Twitch 聊天文字回覆觀眾。
5. 以本地 TTS 產生語音回覆。
6. 在說話時同步驅動模型嘴型。
7. 盡可能不依賴付費雲端 API；除了 Twitch 本身的網路服務外，LLM、TTS、記憶、內容過濾與動作決策都應能在本機完成。

這個目標不是單純製作聊天機器人，而是建立一個有完整狀態與表演流程的虛擬角色：

```text
Twitch 聊天
  -> 訊息篩選與排程
  -> 本地 LLM 決定回覆、情緒與動作
  -> Twitch 文字回覆
  -> 本地 TTS
  -> VTube Studio 嘴型、表情與動作
  -> OBS 畫面、聲音與字幕
```

## 2. 可行性結論

整體方案可行，而且目前電腦足以完成實用的第一版。

### 已確認可行的部分

- VTube Studio 有公開的本機 WebSocket API，可控制模型位置、熱鍵、既有表情、道具、特效及追蹤參數。
- Twitch 官方提供 EventSub WebSocket 接收聊天，以及 Helix API 發送聊天訊息。
- Twitch 的本地 Installed Chatbot 架構不需要公開伺服器、固定 IP、網域或 Webhook。
- LLM 可用 `llama.cpp` 或 Ollama 在本地執行。
- TTS 可使用 MeloTTS、GPT-SoVITS、CosyVoice 或 VOICEVOX 等本地方案。
- 嘴型可由程式直接分析 TTS 音訊後注入 VTube Studio 參數，不一定要依賴虛擬音訊裝置。
- 對話記憶、訊息佇列、字幕及設定資料可使用本地 SQLite 與檔案完成。

### 唯一必要的線上依賴

- Twitch 聊天接收、聊天發送與 OAuth 驗證一定要連接 Twitch。
- 模型、套件及更新在第一次下載時需要網路。
- 核心 LLM、TTS、VTube Studio 控制、記憶與內容判斷完成安裝後可在本機執行。

### 不能由 API 憑空完成的部分

VTube Studio API 能控制模型已有的能力，但不能替 Live2D 模型創造不存在的內容：

- 揮手、跳躍等完整動畫需要模型先有 `.motion3.json` 動畫。
- 開心、生氣、害羞等精緻表情通常需要模型先有 `.exp3.json` 或相關參數。
- 精細 A/I/U/E/O 母音嘴型需要模型先建立對應 blendshape。
- 頭髮、衣服等物理效果需要模型本身已設定 Live2D 物理。
- API 可以新增「輸入參數」，但模型仍須把該輸入映射到實際 Live2D 參數，畫面才會產生變化。

因此，第一版應先使用模型現有的熱鍵、表情與參數，不要把「由程式自動生成全新 Live2D 動畫」列為 MVP 需求。

## 3. 本機環境盤點

研究時確認到的環境如下：

| 項目 | 狀態 |
|---|---|
| CPU | Intel Core i7-12700K，12 核心 / 20 執行緒 |
| 記憶體 | 64 GB |
| GPU | NVIDIA GeForce RTX 2080，8 GB VRAM |
| 專案磁碟 | F 槽約剩餘 282 GB |
| Python | 3.12.10 |
| Node.js | 18.17.1 |
| Git | 已安裝 |
| VTube Studio | 已安裝於 `D:\Games\Steam Games\steamapps\common\VTube Studio` |
| Ollama | 尚未安裝 |
| FFmpeg | 尚未安裝 |
| OBS | 未在檢查的標準位置或 Steam library 中找到 |

### 對這台電腦的實際判斷

- 8 GB VRAM 適合 7B～8B 級、Q4 量化的 LLM。
- VTube Studio 與 OBS 也會使用 GPU/VRAM，因此不能假設 8 GB 全部都能交給 LLM。
- 64 GB RAM 足以讓部分 LLM 層卸載到 CPU，換取較低 VRAM 使用量。
- TTS 第一版建議先跑 CPU，避免和 LLM、VTube Studio、OBS 搶 GPU。
- Python 3.12 對部分語音套件仍可能有相容性問題；專案應另建 Python 3.10 或 3.11 環境，不要改壞系統現有 Python。

## 4. 建議的 MVP 技術方案

### 4.1 主程式

建議使用 Python 3.11 建立單一主程式，先採模組化單體架構，不要一開始拆成大量微服務。

適合的基礎元件：

- `asyncio`：管理 Twitch、LLM、TTS、VTube Studio 與佇列。
- `websockets` 或 `aiohttp`：WebSocket 連線。
- `httpx` 或 `aiohttp`：Twitch Helix/OAuth HTTP API。
- Pydantic：驗證 LLM 結構化輸出與設定檔。
- SQLite：本地記憶、事件與必要狀態。
- `pyvts` 或直接封裝 VTube Studio JSON API。
- NumPy：TTS 音量包絡與嘴型數值計算。

TTS 可獨立成另一個本地進程，避免語音套件崩潰或 GPU 記憶體不足時拖垮 Twitch 與 VTube Studio 連線。

### 4.2 本地 LLM

第一版建議：

- Runtime：`llama.cpp` 的 `llama-server`。
- 快速原型替代：Ollama。
- 起始模型：Qwen3-8B 的 Q4 量化版本。
- 模式：關閉 thinking，以降低聊天延遲。
- Context：先限制在 4K～8K。
- 輸出：用 JSON Schema 或 GBNF grammar 強制結構，不只靠提示詞要求模型輸出 JSON。

Qwen3-8B 的理由：

- 8B 級適合目前 8 GB VRAM。
- 中文、角色扮演與工具／結構化輸出能力較均衡。
- 可關閉 thinking，適合需要快速回應的直播。
- llama.cpp、Ollama 等本地 runtime 已支援。

另一個值得實測的候選是 Llama-Breeze2-8B-Instruct：

- 專門加強繁體中文與台灣語境。
- 官方模型卡提供 function calling 能力。
- 授權是 Llama Community License，不是 Apache-2.0；採用前要保留授權文件。

最終模型不應只看排行榜，應在本機比較：

1. 首 token 延遲。
2. 每秒 token 數。
3. 繁中口語自然度。
4. JSON Schema 遵循率。
5. VTube Studio、OBS 與 TTS 同時執行時的 VRAM 峰值。

### 4.3 LLM 輸出契約

LLM 不得直接產生任意 API 名稱、熱鍵 ID 或參數名稱。它只能從程式提供的白名單中選擇。

建議的第一版資料結構：

```json
{
  "decision": "reply",
  "speech": "要說出的短句",
  "chat_reply": "要傳回 Twitch 的短句",
  "emotion": "happy",
  "action": "nod",
  "intensity": 0.6,
  "memory_note": null
}
```

欄位規則：

- `decision`：`reply`、`react_only` 或 `ignore`。
- `speech`：TTS 要念的內容。
- `chat_reply`：Twitch 文字，可和口語內容不同。
- `emotion`：只能從設定檔定義的情緒選擇。
- `action`：只能從設定檔定義的動作選擇。
- `intensity`：0～1，交由程式映射成動畫強度。
- `memory_note`：只有值得保存的資訊才填入，不應把每句聊天室內容永久保存。

輸出後還必須通過：

1. JSON Schema 驗證。
2. 字串長度限制。
3. 情緒與動作白名單驗證。
4. 內容安全檢查。
5. Twitch 發送限制檢查。

### 4.4 VTube Studio

VTube Studio API 預設位置：

```text
ws://localhost:8001
```

首次使用流程：

1. 在 VTube Studio 開啟 `Allow Plugin API access`。
2. 程式送出 `AuthenticationTokenRequest`。
3. 使用者在 VTube Studio 按允許。
4. 程式保存 `authenticationToken`。
5. 之後啟動或重連時使用 `AuthenticationRequest`，不必每次重新確認。

第一版需要支援的 API：

- 取得目前模型與可用狀態。
- 列出目前模型的熱鍵。
- 列出表情。
- 列出追蹤與 Live2D 參數。
- 觸發熱鍵。
- 開啟或關閉既有表情。
- 使用 `InjectParameterDataRequest` 注入動作與嘴型值。
- 在模型切換或 API 斷線後重新載入映射。

動作分成兩類：

1. 連續參數動作
   例如呼吸、輕微搖擺、點頭、嘴巴開合。由程式以平滑曲線控制參數。

2. 離散熱鍵動作
   例如揮手、驚嚇、切換表情。由 VTube Studio 內已設定的熱鍵觸發。

重要限制：

- 注入的參數至少每秒要重新傳送一次，否則 VTube Studio 會視為來源遺失。
- 實際動畫建議 30 Hz 左右，不必盲目用 60 Hz。
- 同一參數同時只能由一個插件以 `set` 模式控制。
- 動作結束時要平滑交還給原本追蹤來源，避免模型突然跳動。
- 高頻即時反應應以參數注入為主，不要連續狂發熱鍵。

### 4.5 Twitch

新專案應使用 Twitch 官方目前建議的路徑：

- 接收：EventSub WebSocket。
- 事件：`channel.chat.message`。
- 發送：Helix `Send Chat Message` API。
- OAuth：Device Code Grant，將 Twitch 應用註冊成 public client。

優點：

- 本機主動連出，不需要公開 callback。
- 不需要固定 IP、網域、HTTPS 憑證或雲端伺服器。
- Twitch API 本身不收使用費。

MVP 最簡單的帳號模式：

- 先用實況主本人的 Twitch 帳號授權。
- Scope 使用 `user:read:chat` 與 `user:write:chat`。
- 等核心功能穩定後，再評估獨立機器人帳號與 Chat Bot badge。

必做事項：

- 啟動時及每小時呼叫 Twitch token validate endpoint。
- 安全保存 access token 與 refresh token，優先使用 Windows Credential Manager 或 DPAPI。
- 處理 public client refresh token 的輪替與過期。
- 收到 EventSub `session_reconnect` 時先接新連線，再關閉舊連線。
- 斷線重連後重新建立訂閱；Twitch 不保證補送斷線期間遺失的事件。
- 以 EventSub `message_id` 去重。
- 忽略 AI 自己發送的訊息，避免無限自問自答。
- 檢查 `Send Chat Message` 回傳的 `is_sent` 與 `drop_reason`。

Twitch 限制：

- 單則聊天訊息最多 500 字元。
- 一般帳號通常是 20 則／30 秒，且非 broadcaster/mod/VIP 時每頻道每秒最多 1 則。
- broadcaster、moderator 或 VIP 可用較大的 100 則／30 秒額度。
- 超限後可能有長時間訊息被忽略的風險。
- Verified Bot 審核目前仍暫停，不應把它當作 MVP 前提。

本專案實際節流應遠低於官方上限，例如每 5～8 秒最多主動回覆一次。

### 4.6 本地 TTS

第一版建議先使用 MeloTTS：

- MIT License。
- 官方說明支援中文與英文混讀。
- 可用 CPU 即時推論。
- 不與 8 GB GPU 上的 LLM、VTube Studio 及 OBS 競爭 VRAM。

品質升級候選：

| 方案 | 適合用途 | 注意事項 |
|---|---|---|
| GPT-SoVITS | 高品質中／日／英語音、少樣本音色 | 建議 GPU；Windows 有整合包；需 Python 3.10/3.11 與 FFmpeg |
| CosyVoice | 多語、串流與聲音克隆 | 安裝與模型較重，第一版不必先導入 |
| VOICEVOX | 日文角色語音 | 每個角色聲音有各自條款與署名要求 |
| OpenVoice | 跨語言音色轉換／克隆 | 仍須確認來源聲音及模型權利 |

不建議作為預設商用方案：

- Coqui XTTS-v2：模型授權限制商用。
- Fish Speech/Fish Audio：研究授權，商用通常需要另外取得授權。
- ChatTTS：權重有非商用限制。
- 舊版 rhasspy/piper：原倉庫已封存。
- 新版 Piper：改採 GPLv3，整合前需評估授權義務。

任何聲音克隆都必須取得原聲音擁有者明確同意。程式碼採 MIT 或 Apache-2.0，不代表可以任意複製真人、配音員或角色聲音。

### 4.7 嘴型同步

#### 建議的 MVP：直接 API 驅動

1. TTS 產生 PCM/WAV。
2. 程式計算短時間音量包絡。
3. 音訊開始播放時，以相同時間軸將數值送到 VTube Studio 的 `MouthOpen`。
4. 停止或被打斷時，平滑把嘴型歸零。

優點：

- 不需要額外虛擬音訊驅動。
- 音訊與嘴型時間軸由同一程式控制，較容易同步。
- 模型只要有基本嘴巴開合參數即可。

限制：

- 單純音量只能做到嘴巴開合，不能精確表現 A/I/U/E/O。
- 要做精細嘴型，TTS 必須提供音素時間資訊，或另外做 phoneme/viseme 對齊。
- 模型也必須先有對應母音 blendshape。

#### 備用方案：VTube Studio Advanced Lipsync

1. 用 VB-CABLE 或其他虛擬音訊路由把 TTS 聲音送入虛擬麥克風。
2. VTube Studio 選擇該麥克風並開啟 Advanced Lipsync。
3. OBS 同時擷取該音訊。

這條路設定快速，但會增加 Windows 音訊路由、裝置權限與延遲問題。若直播營利，使用虛擬音訊工具前應重新確認其當時授權條款。

### 4.8 OBS 與字幕

第一版可先不自動控制 OBS，只需要：

- 讓 OBS 擷取 VTube Studio 畫面。
- 讓 OBS 擷取 TTS 播放裝置或 TTS 專用音訊來源。
- 用本地文字檔或 obs-websocket 更新字幕來源。

後續再加入：

- 自動切換場景。
- 訂閱、Raid 或特殊事件的字幕與動畫。
- TTS、背景音樂與遊戲音效分軌。
- 故障畫面或離線待機畫面。

## 5. 核心執行流程

建議使用明確的狀態機：

```text
IDLE
  -> 收到 Twitch 訊息
FILTERING
  -> 去重、自身訊息排除、黑名單、優先權與 TTL
THINKING
  -> 呼叫本地 LLM
VALIDATING
  -> Schema、白名單、長度與安全檢查
ACTING
  -> 先切換情緒或預備動作
SPEAKING
  -> 播放 TTS，同步嘴型與字幕
COOLDOWN
  -> 清理暫時表情並等待下一輪
IDLE
```

重要設計：

- 聊天接收不能被 LLM 或 TTS 阻塞。
- 同一時間只允許一段主要 TTS 播放。
- 每則等待訊息要有 TTL；過時的聊天不要在幾十秒後才回覆。
- 訂閱、Raid、管理員訊息等高優先事件可以插隊。
- 打斷時要同時取消 LLM 串流、停止 TTS、關閉字幕並讓嘴型歸零。
- 情緒和動作要有開始、維持與收尾，不可只觸發後永遠停留。

## 6. 訊息選擇與內容安全

AI 不應逐條回覆所有聊天訊息。建議：

- 每 5～8 秒選擇一則值得回應的訊息。
- 設定佇列長度與訊息 TTL。
- 高流量時改為摘要聊天室正在討論的主題。
- 優先處理直接提問、提及角色名稱、訂閱或其他重要事件。
- 忽略機器人自身訊息、重複訊息、指令洗版與明顯誘導提示詞。

安全必須分層：

1. Twitch AutoMod 與頻道規則。
2. 本地輸入過濾。
3. LLM system prompt 中的人設、禁區與回應邊界。
4. LLM 結構化輸出白名單。
5. 送進 Twitch/TTS 前的最終輸出檢查。

LLM 不可直接：

- 執行任意系統命令。
- 讀取任意本機檔案。
- 產生並執行任意 VTube Studio API payload。
- 取得 OAuth token 或其他秘密。
- 根據聊天室文字改寫 system prompt、動作設定或安全規則。

## 7. 本地記憶

第一版只需要 SQLite，不需要外部向量資料庫。

記憶分層：

- 人設：版本控制內的固定設定。
- 短期記憶：最近 N 則有效聊天與 AI 回應。
- 場次摘要：本次直播發生的重要事件與梗。
- 觀眾記憶：只保存明確有價值、且適合長期保存的內容。

不要預設永久保存完整 Twitch 聊天紀錄。應提供：

- 保存天數設定。
- 清除單一使用者或整個場次資料的方式。
- 日誌中遮蔽 OAuth token。
- 將使用者 ID 與顯示名稱分開處理，避免改名造成錯誤關聯。

向量檢索等 RAG 功能可以後加；MVP 先以關鍵字、使用者 ID 與摘要查詢即可。

## 8. 建議的專案結構

```text
AI Vtuber/
  PROJECT_BRIEF.md
  pyproject.toml
  README.md
  .gitignore
  config/
    character.example.yaml
    actions.example.yaml
  src/
    ai_vtuber/
      app.py
      config.py
      domain/
        models.py
        events.py
      twitch/
        auth.py
        eventsub.py
        chat.py
      vts/
        client.py
        actions.py
        lipsync.py
      llm/
        client.py
        prompts.py
        schema.py
      tts/
        engine.py
        playback.py
      orchestration/
        queue.py
        state_machine.py
      memory/
        repository.py
      safety/
        filters.py
      observability/
        logging.py
  tests/
```

這只是建議骨架。第一階段只建立真正會用到的檔案，不要為空白功能預先製造大量 abstraction。

## 9. 分階段工作清單

### Phase 0：建立專案基礎

- 初始化 Git。
- 建立 Python 3.11 虛擬環境。
- 建立 `pyproject.toml`、基礎套件與測試設定。
- 建立設定檔 schema。
- 設定結構化日誌與秘密遮蔽。
- 確認大型模型、token、語音樣本與生成音訊不會被提交進 Git。

完成條件：

- 程式可以啟動、讀取設定並輸出健康狀態。
- 測試可以在沒有 Twitch、VTube Studio 與模型的情況下執行。

### Phase 1：VTube Studio 技術驗證

- 連接 `ws://localhost:8001`。
- 完成首次 token 授權與持久化。
- 讀取目前模型。
- 列出熱鍵、表情與參數。
- 以設定檔建立語意動作到 VTS 資源的映射。
- 驗證一個表情、一個熱鍵、一個連續參數動作。
- 驗證 `MouthOpen` 注入及停止後歸零。
- 加入斷線重連與模型切換處理。

完成條件：

- 執行一個本地命令即可讓模型做出已設定的測試動作。
- 不把 VTube Studio 內部 ID 寫死在商業邏輯。
- VTS 關閉再開啟後可重新連線。

### Phase 2：Twitch 收發

- 在 Twitch Developer Console 註冊 public client。
- 完成 Device Code Grant。
- 使用安全儲存保存 token。
- 建立 EventSub WebSocket。
- 訂閱 `channel.chat.message`。
- 加入 message ID 去重、自身訊息排除、keepalive 與 reconnect。
- 用 Helix Send Chat Message 發送測試訊息。
- 實作發送節流、500 字限制與 `drop_reason` 處理。

完成條件：

- Twitch 訊息可進入本地事件佇列。
- 測試訊息可由 API 回到同一聊天室。
- 網路短暫中斷後能自動恢復。
- 不會因 AI 自己的訊息形成回覆迴圈。

### Phase 3：本地 LLM

- 安裝 llama.cpp 或先用 Ollama 做原型。
- 下載一個 7B～8B Q4 模型。
- 建立角色 system prompt。
- 建立結構化輸出 schema。
- 驗證 `reply`、`react_only`、`ignore` 三種結果。
- 建立動作白名單與拒絕未知值的邏輯。
- 記錄首 token、總生成時間、token/s、VRAM 與 RAM。
- 在 VTube Studio 同時執行時重新測量。

完成條件：

- 連續測試至少 100 組代表性聊天，全部輸出都可解析或被安全拒絕。
- 不會因聊天內容產生白名單以外的動作。
- 建立本機可接受的延遲基線，再決定是否更換模型。

### Phase 4：TTS、音訊與嘴型

- 安裝 FFmpeg。
- 建立 MeloTTS CPU 原型。
- 支援產生與播放 PCM/WAV。
- 播放時同步音量包絡到 `MouthOpen`。
- 支援取消播放、清空佇列與嘴型復位。
- 建立字幕輸出。
- 再評估 GPT-SoVITS 是否能在剩餘 GPU 資源下穩定執行。

完成條件：

- 語音、字幕和嘴型從同一份文字與時間軸產生。
- 中途取消後不會留下卡住的嘴型或字幕。
- 連續播放多句時沒有音訊重疊。

### Phase 5：整合成 AI VTuber

- 串接 Twitch -> LLM -> VTS -> TTS -> Twitch 回覆。
- 實作訊息優先權、TTL、冷卻與佇列上限。
- 實作角色狀態機。
- 動作在語音開始前適當預備，語音結束後收尾。
- 實作錯誤隔離：TTS 故障時仍能文字回覆，LLM 故障時仍保持 Twitch/VTS 連線。

完成條件：

- 可以在私人或測試頻道連續運作至少一小時。
- 沒有訊息回圈、佇列無限增長或模型卡在某個表情。
- 單一元件重啟不需要整套程式人工重開。

### Phase 6：安全、記憶與直播穩定性

- 加入本地內容過濾。
- 建立短期記憶與場次摘要。
- 實作資料保存期限與刪除。
- 加入健康檢查、監控數值與旋轉日誌。
- 實作 VTS、Twitch、LLM、TTS 各自的重連或重啟策略。
- 進行 4～8 小時 soak test。

### Phase 7：品質升級

- GPT-SoVITS 或 CosyVoice。
- 母音／viseme 精細嘴型。
- 更多 Live2D 預製動作與情緒過渡。
- Twitch 訂閱、Raid、Channel Points 等 EventSub 事件。
- OBS WebSocket 場景與字幕整合。
- 獨立 Twitch bot 帳號。

## 10. 驗收標準

MVP 應至少滿足：

1. 全部核心推論與語音均在本地執行。
2. 沒有使用任何按量收費的 LLM、TTS 或記憶 API。
3. Twitch 聊天能穩定接收與發送。
4. AI 能選擇忽略、純動作反應或文字／語音回覆。
5. 每次動作都來自明確白名單。
6. TTS 播放時模型嘴型同步，停止或打斷後會歸零。
7. 能在 VTube Studio 或 Twitch 斷線後自動恢復。
8. 聊天高流量時佇列不會無限成長。
9. OAuth token 不會出現在 Git、一般日誌或錯誤畫面。
10. 至少完成一小時整合測試，再進行實際公開直播。

延遲目標應先 benchmark 再鎖定。可先把「收到訊息到開始發聲的中位數低於 5 秒」當作方向，而不是未經測試的硬性保證。

## 11. 主要風險與決策

### GPU 資源競爭

RTX 2080 只有 8 GB VRAM，LLM、VTube Studio、OBS 與高品質 TTS 同時常駐可能不足。

處理方式：

- LLM 使用 Q4。
- Context 先限制 4K～8K。
- 部分 LLM 層放到 CPU。
- 第一版 TTS 跑 CPU。
- OBS 優先使用硬體編碼，但仍要實測整體 VRAM。
- 以實際監控結果決定是否升級 TTS 或模型，而不是一次全部載入。

### 回應延遲與聊天室過時

本地 LLM/TTS 的吞吐量一定低於高流量聊天室。

處理方式：

- 不逐條回覆。
- 佇列設上限與 TTL。
- 每輪只選一則訊息。
- 高流量時摘要話題。
- 長回覆切成短句，但避免每句都各自發一則 Twitch 訊息。

### Live2D 模型能力不足

如果目前模型沒有需要的表情、動畫或嘴型參數，程式不能補出相同品質。

處理方式：

- Phase 1 先做資源盤點。
- 先建立現有熱鍵／表情／參數的語意映射。
- 將缺少的動畫與表情整理成建模需求清單。

### 授權

- VTube Studio API／插件開發本身沒有 API 授權費。
- VTube Studio 官方 FAQ 表示，營利 Twitch／YouTube 使用者至少要購買一種付費版本，這是 Live2D 授權要求。
- Live2D 模型、角色立繪、動作、語音模型與訓練資料各自可能有額外授權。
- 聲音克隆必須有原聲音擁有者同意。
- 每次更新模型或 TTS checkpoint 都要保留當時的 LICENSE／模型卡。

### Twitch 政策

- 機器人產生的內容仍受 Twitch Terms、Community Guidelines 與 AutoMod 約束。
- 不要在未取得頻道主同意的其他頻道自動運作。
- 帳號名稱或簡介應清楚標示為 AI／Bot，避免冒充真人。
- 不依賴 Verified Bot 審核或高額度。
- Twitch API 與政策會變動，上線前應重新核對官方文件。

## 12. 暫時不納入 MVP

- 自動建立或修改 Live2D 原始模型。
- 自動生成 `.motion3.json` 或 `.exp3.json`。
- 多個 Twitch 頻道的大規模雲端 bot。
- 讓聊天室直接執行系統命令或任意插件。
- 完整向量資料庫與大型 RAG。
- 即時網路搜尋。
- 無限制保存所有觀眾聊天。
- 同時常駐多個大型 LLM。
- 一開始就做 GUI；先確保核心流程可測試、可觀察。

## 13. 建議下一個對話先做的事情

下一個對話不要直接一次安裝所有模型或語音套件。先完成 Phase 0 與 Phase 1：

1. 檢查專案目錄現況。
2. 初始化 Python 3.11 專案與 Git。
3. 建立最小設定檔及日誌。
4. 實作 VTube Studio 連線與 token 保存。
5. 列出目前模型可用的熱鍵、表情與參數。
6. 將盤點結果輸出成可編輯設定檔。
7. 完成一個測試表情、一個測試熱鍵與基本嘴型注入。
8. 加入單元測試與不需要 VTube Studio 的 mock 測試。

這一步完成後，才能知道目前 Live2D 模型真正能做哪些動作，避免後續 LLM 與 TTS 建立在不存在的模型能力上。

## 14. 可直接貼到新對話的開場指令

```text
請以 F:\user\Documents\Workspace\AI Vtuber 為專案目錄，先完整閱讀 PROJECT_BRIEF.md。

這是一個 Windows 本地 AI VTuber 專案。核心目標是接收 Twitch 聊天，使用本地 LLM 決定回覆、情緒與動作，控制 VTube Studio 的 Live2D 模型，並使用本地 TTS 產生語音與嘴型。除了 Twitch 必要的官方服務外，不使用按量付費的雲端 LLM/TTS。

請先執行文件中的 Phase 0 與 Phase 1，不要一次導入 Twitch、LLM、TTS、OBS，也不要先下載大型模型。先檢查現有檔案與工具，建立簡潔的 Python 3.11 專案骨架，實作 VTube Studio 連線、授權 token 持久化、模型資源盤點、語意動作映射，以及表情／熱鍵／基本 MouthOpen 注入的 smoke test。

請遵守這些原則：
- 不把 VTube Studio 的模型 ID、熱鍵 ID 或參數名稱散落寫死在商業邏輯中。
- 不讓未來的 LLM 直接產生任意 API payload，只能選白名單動作。
- token、模型檔、語音樣本與生成音訊不得提交進 Git。
- 使用最少但完整的架構，避免預先建立沒有用到的 abstraction。
- 先做可在沒有 VTube Studio 時執行的 mock/unit tests，再做實機 smoke test。
- 若需要使用者在 VTube Studio UI 開啟 API 或按下授權，清楚指出操作步驟。
- 完成後回報實際讀到的模型熱鍵、表情、參數，以及仍缺少哪些模型端資源。
```

## 15. 主要官方資料

### VTube Studio

- API 主文件：<https://github.com/DenchiSoft/VTubeStudio>
- Lipsync：<https://raw.githubusercontent.com/wiki/DenchiSoft/VTubeStudio/Lipsync.md>
- FAQ 與商用說明：<https://raw.githubusercontent.com/wiki/DenchiSoft/VTubeStudio/FAQ.md>
- EULA：<https://denchisoft.com/license/>
- 動畫說明：<https://github.com/DenchiSoft/VTubeStudio/wiki/Animations>

### Twitch

- Chat 與 rate limits：<https://dev.twitch.tv/docs/chat/>
- Chat authentication：<https://dev.twitch.tv/docs/chat/authenticating/>
- EventSub WebSocket：<https://dev.twitch.tv/docs/eventsub/handling-websocket-events/>
- OAuth flows：<https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/>
- Token validation：<https://dev.twitch.tv/docs/authentication/validate-tokens/>
- Send Chat Message：<https://dev.twitch.tv/docs/api/reference/#send-chat-message>
- 官方 chatbot 範例：<https://dev.twitch.tv/docs/chat/chatbot-guide/>

### 本地 LLM

- llama.cpp：<https://github.com/ggml-org/llama.cpp>
- llama.cpp grammar：<https://github.com/ggml-org/llama.cpp/blob/master/grammars/README.md>
- llama.cpp function calling：<https://github.com/ggml-org/llama.cpp/blob/master/docs/function-calling.md>
- Ollama structured outputs：<https://docs.ollama.com/capabilities/structured-outputs>
- Qwen3-8B：<https://huggingface.co/Qwen/Qwen3-8B>
- Llama-Breeze2-8B-Instruct：<https://huggingface.co/MediaTek-Research/Llama-Breeze2-8B-Instruct>

### 本地 TTS

- MeloTTS：<https://github.com/myshell-ai/MeloTTS>
- GPT-SoVITS：<https://github.com/RVC-Boss/GPT-SoVITS>
- CosyVoice：<https://github.com/FunAudioLLM/CosyVoice>
- VOICEVOX：<https://voicevox.hiroshiba.jp/>
- OpenVoice：<https://github.com/myshell-ai/OpenVoice>

### OBS

- OBS Studio：<https://obsproject.com/>
- obs-websocket：<https://github.com/obsproject/obs-websocket>

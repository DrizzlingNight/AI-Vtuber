# AI VTuber Local

目前已實作 `PROJECT_BRIEF.md` 的 Phase 0～3：Python 專案基礎、VTube Studio
控制、Twitch 官方 Device Code Grant、EventSub WebSocket 收訊和 Helix
Send Chat Message，以及以 llama.cpp 執行的本地結構化 LLM。尚未加入 TTS 或 OBS，
也尚未把 Twitch、LLM 與 VTube Studio 串成自動直播流程。

## 安裝

專案固定使用 Python 3.11，不修改系統預設的 Python 3.12。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

確認設定、Python 版本與 VTube Studio 連線狀態：

```powershell
.\.venv\Scripts\python.exe -m ai_vtuber health
```

VTube Studio 沒有開啟時，健康檢查仍可執行，並會將 VTS 顯示為 `offline`。

## VTube Studio 首次授權與盤點

1. 啟動 VTube Studio 並載入要控制的模型。
2. 在 VTube Studio 設定中找到 Plugin API，開啟 **Allow Plugin API access**。
3. 執行下列盤點命令。
4. VTube Studio 顯示插件授權視窗時，確認名稱為 **AI VTuber Local**，再按 **Allow**。

```powershell
.\.venv\Scripts\python.exe -m ai_vtuber inventory
```

授權 token 會寫入 `.local/secrets/vts-token.json`。完整模型盤點會寫入
`.local/state/vts-inventory.json`。兩者都由 `.gitignore` 排除。

第一次盤點也會依 `config/app.yaml` 的偏好，建立本機專用的
`config/actions.local.yaml`：

- 表情：選擇目前模型的一個既有表情。
- 熱鍵：優先選擇既有動畫熱鍵，其次才是表情切換熱鍵。
- 連續動作：依設定中的候選 VTS 輸入參數選擇。
- 嘴型：依設定中的嘴型參數候選選擇。

找不到的資源不會猜測或虛構，會列在 `missing_resources`。可參考
`config/actions.example.yaml` 手動調整映射，再使用
`inventory --overwrite-actions` 重新產生。

## Smoke test

下列單一命令會重新確認目前模型、驗證本機映射，再依序測試表情、熱鍵、連續參數及
嘴型。表情會還原原狀，連續參數與嘴型在結束及取消時都會注入中性值。

```powershell
.\.venv\Scripts\python.exe -m ai_vtuber smoke
```

只測其中一項：

```powershell
.\.venv\Scripts\python.exe -m ai_vtuber smoke --only mouth
```

## 20 秒說話動作示範

`talk-demo` 會在同一個 30 Hz 影格中同步控制頭部三軸、嘴巴開合與停頓、嘴角、
雙眼眨眼和眉毛。微笑可只由連續參數形成，也可選擇映射檔中的既有表情。開始與結束
會用權重淡入淡出，結束或取消時會釋放全部參數並還原原本的表情狀態。

眼睛的 `neutral_value` 必須依模型的 VTS 參數映射校正；它代表實際睜眼值，不一定是
輸入參數宣告的最大值。NightRain 的校正值為 `0.0833`。眨眼使用 0.16 秒的閉眼階躍，
不使用慢速正弦曲線；為抵消此模型的 VTS smoothing，閉眼值校正為 `-0.02`。眼睛預加重
最多只能超出宣告範圍的 10%，其他參數仍嚴格限制在各自範圍內。

```powershell
.\.venv\Scripts\python.exe -m ai_vtuber talk-demo --duration 20
```

連線中斷時，client 會依 `config/app.yaml` 的退避設定重新連線並使用既有 token
重新授權。每個語意動作執行前都會確認目前模型；偵測到模型切換或同模型重新載入時，
會重新盤點資源。映射檔若綁定不同模型，動作會被拒絕，不會誤觸其他模型資源。

## Twitch Developer Console 設定

本階段採「實況主本人授權、收發自己的聊天室」模式，只要求
`user:read:chat` 與 `user:write:chat`。

1. 登入 <https://dev.twitch.tv/console>。開發者帳號必須已驗證 Email 並啟用 2FA。
2. 進入 **Applications**，按 **Register Your Application**。
3. 輸入唯一的應用程式名稱。OAuth Redirect URL 可填
   `http://localhost:3000`；Device Code Grant 不會使用 callback，但註冊表單仍要求 URL。
4. 選擇最符合本機聊天室工具的 Category，完成 CAPTCHA 後建立應用程式。
5. 在應用程式的 **Manage** 頁將 **Client Type** 設為 **Public** 並儲存。
6. 複製 **Client ID**。不要建立、貼入或保存 Client Secret；public Device Code Grant
   不需要它。

在同一個 PowerShell 視窗設定 Client ID。Client ID 是公開識別碼，不是 OAuth token：

```powershell
$env:TWITCH_CLIENT_ID = "貼上 Client ID"
```

## Twitch 授權

執行官方 Device Code Grant：

```powershell
.\.venv\Scripts\python.exe -m ai_vtuber twitch-auth
```

終端會顯示 Twitch 驗證網址與一次性代碼。以要開台的 Twitch 帳號登入該網址，確認應用程式
名稱及兩個 scopes 後按授權。成功後可驗證目前身份、有效期與實際 scopes：

```powershell
.\.venv\Scripts\python.exe -m ai_vtuber twitch-validate
```

access token 與一次性 refresh token 會先由目前 Windows 使用者的 DPAPI 加密，再原子寫入
`.local/secrets/twitch-token.bin`。檔案、`.local/` 與本機設定都由 `.gitignore` 排除；
一般日誌不記錄 token、Device Code 或 Authorization header。

程式會在啟動及每小時呼叫 Twitch `/validate`。API 回傳 401 或 token 即將失效時，會在單一
鎖內使用最新 refresh token 取得新 token，並立即保存 Twitch 輪替後的新 refresh token。
Public client 的 refresh token 若閒置 30 天會失效，屆時需重新執行 `twitch-auth`。

## Twitch 收發與 smoke test

接收非自身的 `channel.chat.message`；`--max-messages 0` 代表持續執行到取消：

```powershell
.\.venv\Scripts\python.exe -m ai_vtuber twitch-listen --max-messages 1
```

從另一個 Twitch 帳號在聊天室送出一則訊息，終端應輸出一個本地佇列事件。EventSub
notification 會依外層 `metadata.message_id` 去重，授權帳號自己的訊息不會進入佇列。

單獨透過 Helix 發送訊息：

```powershell
.\.venv\Scripts\python.exe -m ai_vtuber twitch-send "Phase 2 send test"
```

完整 smoke test 會先建立 EventSub 訂閱，再用 Helix 發送一則公開聊天訊息，最後確認同一個
message ID 已由 EventSub 收到但被自身訊息過濾器排除：

```powershell
.\.venv\Scripts\python.exe -m ai_vtuber twitch-smoke
```

發送端拒絕空字串及超過 500 字元的訊息，預設每 5 秒最多發送一則；Twitch 回傳
`is_sent: false` 時會將 `drop_reason.code` 與訊息當作失敗回報。一般斷線會依指數退避
重新連線並重建訂閱；`session_reconnect` 則會先連上 Twitch 指定的新 URL、收到 Welcome
後才關閉舊連線，且不重複訂閱。斷線期間 Twitch 不保證補送事件。

## 本地 LLM

Phase 3 使用官方 `google/gemma-4-12B-it-qat-q4_0-gguf`，固定 revision
`29d097773436b69ff9feafd636ab4cf873786537`。文字 GGUF 為 QAT Q4_0、
6,975,879,296 bytes（約 6.50 GiB），SHA-256：

```text
93567e57a8fe10b23569b9d9ec38cd005deedf71e29477c421a4b83f418a538b
```

模型採 Apache-2.0，可用於商業 Twitch 內容；若重新散布模型，仍須保留授權與
attribution。完整選型、runtime digest 與資源估算記錄在
[`docs/phase-3-model.md`](docs/phase-3-model.md)。

llama.cpp 固定使用官方 `b10621`（stable `v0.3.0` 對應 nightly binaries）的
Windows CUDA 12.4 build。將 runtime 解壓到
`.local/runtime/llama.cpp/`，模型放在
`models/gemma-4-12b-it-qat-q4_0.gguf`。兩個目錄都由 `.gitignore` 排除。

啟動本機 server；啟動前會完整核對模型 SHA-256，失敗就拒絕載入：

```powershell
.\.venv\Scripts\python.exe -m ai_vtuber llm-serve
```

另一個 PowerShell 視窗可檢查 server、模型與實際 allowlist，或測試一則聊天：

```powershell
.\.venv\Scripts\python.exe -m ai_vtuber llm-status
.\.venv\Scripts\python.exe -m ai_vtuber llm-chat "晚上好～今天過得怎麼樣？"
```

LLM endpoint 被設定 schema 限制在 loopback HTTP，不接受遠端主機、URL credentials
或 HTTPS API。`llm-serve` 會建立 `.local/secrets/llama-server-api-key.txt`，推論
endpoint 沒有該 key 會回 401；CORS 只允許 localhost，且 Web UI 關閉。送入模型的
資料只有版本控制內的角色 prompt 與單則測試聊天，不會建立或傳入 `TwitchAuth`、
DPAPI token store、access token、refresh token、VTS internal ID 或任意 API
payload。

### 結構化輸出

server 端使用 llama.cpp `response_format=json_schema` 強制封閉 JSON；程式端仍以
Pydantic 和動態 allowlist 做第二次驗證。任何解析、欄位、長度或白名單錯誤都會安全
拒絕，不會嘗試修補後執行。

| decision | 必要規則 |
|---|---|
| `reply` | `speech`、`chat_reply`、`emotion` 必填；`action` 可為 `null`；記憶固定為 `null` |
| `react_only` | 不得有文字；`emotion` 或 `action` 至少一項，且 intensity 大於 0 |
| `ignore` | 文字、情緒、動作與記憶均為 `null`，intensity 固定為 0 |

`emotion` 只能來自 `config/app.yaml`；`action` 不只受同一檔案限制，啟動時還必須
確實存在於 `config/actions.local.yaml` 的既有 VTS 語意白名單。目前只開放較中性的
`continuous_test`，不開放 NightRain 的生氣表情／熱鍵、嘴型與 talk-demo 底層通道。

### 110 組繁中評測與 benchmark

`tests/fixtures/traditional_chinese_chat_cases.json` 包含 110 組不重複案例：40 組自然
聊天、20 組 Twitch 情境、20 組 decision、15 組角色互動及 15 組提示注入／越權案例。
mock/unit tests 使用同一資料集，不需要 Twitch、VTube Studio、llama.cpp 或模型。

實機 benchmark 必須保持 VTube Studio 開啟；工具會在整段測試持續探測 VTS，並每
0.5 秒取樣系統 RAM、`llama-server` working set、整體 GPU VRAM 與 GPU utilization：

```powershell
.\.venv\Scripts\python.exe -m ai_vtuber llm-benchmark
```

報告會原子寫入 `.local/benchmarks/`，包含每案輸出、safe rejection、decision 命中、
首 token、總時間、token/s、RAM、VRAM 與 VTS 全程在線狀態。預設至少 99% 輸出須通過
schema 與白名單，否則命令回傳 exit code 2。

目前 Gemma 4 12B 正式基線（VTube Studio 全程開啟）：

| 指標 | 結果 |
|---|---:|
| 直接通過／安全拒絕 | 109／1，共 110 組 |
| schema＋語言＋白名單接受率 | 99.09% |
| decision 標註命中率 | 98.18% |
| 首 token p50／p95 | 1.63／1.79 秒 |
| 總生成 p50／p95 | 11.19／15.02 秒 |
| token/s p50 | 6.11 |
| 合併 VRAM 峰值 | 7,311 MiB |
| VTS-only VRAM | 2,657 MiB |
| LLM 路徑增加 VRAM | 約 4,654 MiB |
| `llama-server` working set 峰值 | 8,787 MiB |

唯一 safe rejection 是回覆混入簡體字，沒有送往 Twitch 或 VTS。完整可重現摘要與
已知限制見 [`docs/phase-3-model.md`](docs/phase-3-model.md)。

## 測試

Mock/unit tests 不需要 Twitch 帳號、VTube Studio、模型或網路：

```powershell
.\.venv\Scripts\python.exe -m pytest
```

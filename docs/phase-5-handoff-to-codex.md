# Phase 5 交接文件（交給 Codex）

更新日期：2026-09-14
專案目錄：`F:\user\Documents\Workspace\AI Vtuber`
交接對象：接手繼續施工的 Codex（或任何後續工程代理人）

這份文件的目的，是讓完全沒看過本專案對話紀錄的人（或 AI），只靠這份文件
與程式庫本身，就能安全接手，不需要回頭問「這個專案到底要做什麼」「為什麼
不能做某件事」。請先整份讀完，再開始動工。

---

## 1. 專案是什麼、要做到什麼程度

這是一個「以 Windows 本地端運作為主」的 AI VTuber 系統。核心目的：

1. 讀取 Twitch 聊天室訊息。
2. 用本地 LLM（Gemma 4 12B，經 llama.cpp 執行）決定要不要回覆、回覆內容、
   情緒與動作。
3. 通過白名單與 schema 驗證後，才能：
   - 用 Twitch 聊天文字回覆觀眾。
   - 用本地 TTS（eSpeak NG）合成語音並播放。
   - 驅動 VTube Studio 內的 NightRain 模型嘴型（MouthOpen）、表情與動作。
4. 全程盡量不依賴付費雲端 API；除了 Twitch 本身必須連線外，LLM、TTS、
   決策與動作對應都在本機完成。

完整原始需求見專案根目錄 `PROJECT_BRIEF.md`；那是最初的研究與可行性報告，
包含分階段規劃（Phase 0～7）。**Phase 5 只負責把 Phase 1～4 已經各自驗證
過的元件串起來**，不負責重做任何一個元件，也不負責 Phase 6（OBS）與
Phase 7（更進階功能）。

### 目前各 Phase 狀態

| Phase | 內容 | 狀態 |
|---|---|---|
| Phase 0 | 專案基礎、Python 環境 | 完成 |
| Phase 1 | VTube Studio 控制、NightRain 校正 | 完成 |
| Phase 2 | Twitch Device Code Grant、DPAPI token、EventSub、Helix | 完成 |
| Phase 3 | 本地 LLM（Gemma 4 12B／llama.cpp）、結構化輸出、白名單 | 完成 |
| Phase 4 | 本地 TTS（eSpeak NG）、PCM/WAV、PortAudio 播放、字幕、MouthOpen | 完成 |
| **Phase 5** | **整合成 AI VTuber（orchestration）** | **程式與離線測試完成；實機驗收尚未開始** |
| Phase 6 | OBS 整合 | 未開始，本輪不做 |
| Phase 7 | 進階功能（Raid、Channel Points 等額外 EventSub 訂閱） | 未開始，本輪不做 |

**Phase 5 的驗收標準是「真實 Twitch → LLM → VTS → TTS → Twitch 至少跑一小時，
且延遲、RAM、VRAM 都有量測，VTS／MouthOpen 沒有降級」。目前只完成到「離線
mock/unit test 162 個通過」，尚未執行任何一次成功的實機單輪、連續或一小時
測試。這是接手時最需要先確認清楚的事，不要把離線測試通過誤讀成 Phase 5
已經完工。**

---

## 2. 為什麼要看這份文件而不是直接看程式

因為程式本身不會告訴你：

- 哪些限制是「安全與權利要求」，不是「工程偏好」，不能為了方便而繞過。
- 哪些看起來像 bug 的行為其實是刻意設計（例如：已送出的 Twitch 回覆不會
  因為被打斷而重送）。
- 之前已經踩過哪些坑、重現過哪些缺陷、怎麼修的，避免重複除錯同一件事。
- 「離線測試通過」與「Phase 5 驗收通過」是兩件不同的事。

---

## 3. 絕對不能做的事（安全與權利邊界）

以下規則來自使用者原始需求，優先權高於任何工程判斷或效率考量：

1. **不得輸出、提交、寫入 benchmark 檔案或交給 LLM 的內容**：
   - Twitch OAuth access token、refresh token
   - `.local/secrets/twitch-token.bin` 的 DPAPI 加密內容或其明文
   - `.local/secrets/llama-server-api-key.txt` 的 llama-server API key
   - `.local/secrets/vts-token.json` 的 VTube Studio 授權 token
   - 任何 `.local/` 目錄下的個人化狀態（已被 `.gitignore` 排除，不要手動加入）

2. **不得使用未授權的真人或角色聲音**：
   - MeloTTS 官方中文 checkpoint 的 speaker、訓練資料與聲音權利尚未查證，
     **不得下載、啟用或用於直播**，即使程式碼中的 adapter 已經寫好也一樣。
   - 目前唯一允許使用的 TTS 引擎是 eSpeak NG（規則式合成音，非真人聲音）。
   - 若要改用 MeloTTS 或任何其他聲音，必須先完成新的權利與授權查證，這件
     事不屬於工程任務，需要使用者另外確認。

3. **不得做的架構或範圍變更**：
   - 不加入 OBS（Phase 6 範圍）。
   - 不提前做 Phase 6 或 Phase 7 的功能（例如 Raid、Channel Points 等額外
     EventSub 訂閱、多模型支援等）。
   - 不重做 VTube Studio 架構、NightRain 校正、Twitch 驗證流程、LLM 推論
     或 Phase 4 的 TTS/播放管線；Phase 5 只負責整合與收尾，既有元件視為
     已驗證過的基礎設施。

4. **不得讓 orchestration 破壞既有安全設計**：
   - 只有通過既有 Pydantic schema、繁體中文檢查、`emotion`／`action` 白名單
     驗證的 `speech`，才能進入 TTS／VTS。
   - `action` 除了要在 `config/app.yaml` 的 `llm.allowed_actions` 內，還必須
     實際存在於本機 `config/actions.local.yaml` 的既有語意動作映射中；兩者
     缺一都要拒絕，不能只檢查其中一邊。
   - 不能讓程式自動生成或猜測不存在的 VTS 資源（表情、熱鍵、參數），找不到
     就必須明確列在 `missing_resources`，不可以假裝存在。

---

## 4. 系統架構總覽

```text
Twitch EventSub (channel.chat.message)
        |  非阻塞 put_nowait，獨立有界佇列
        v
Bounded Priority Queue  (容量 32、TTL 30s、每使用者 cooldown 2s、
                         高優先訊息類型可插隊、回應 cooldown 5s)
        v
State Machine: idle -> thinking -> validating -> acting -> speaking -> cooldown
        v
本地 LLM (Gemma 4 12B / llama.cpp)  --產生-->  decision (reply / react_only / ignore)
        v
驗證層：Pydantic schema + 繁體中文檢查 + emotion 白名單 + action 白名單
        |（任何一項失敗 -> 安全丟棄，不進入下一步，不視為系統錯誤）
        v
   [只有 reply 且驗證通過才會走到這裡]
        v
TTS 合成 (eSpeak NG, CPU) -> PCM/WAV
        v
VTS 動作準備 (表情/熱鍵/連續參數，依既有 actions.local.yaml 映射)
        v
播放：字幕 + MouthOpen 與音訊播放同時間軸，單一播放佇列不重疊
        v
收尾：播放完成或被打斷都要停止聲音、清空字幕、MouthOpen 歸零、
      表情還原、狀態回到 idle 或 cooldown
        v
Twitch Helix 回覆（在動作／語音嘗試與收尾後發送；TTS 或 VTS 降級時仍可保留
                  已驗證的安全文字回覆；已送出的請求不會因後續打斷而重送）
```

關鍵設計原則（每一項都對應到之前實際重現過的缺陷，不是理論性考量）：

- **Twitch 接收永遠不能被下游阻塞。** EventSub 收訊使用 `put_nowait` 寫入
  有界佇列，佇列滿了就依 TTL／優先權丟棄舊項目，不會等待 LLM、TTS 或 VTS。
  即使 LLM 卡住、TTS 故障或 VTS 斷線，Twitch 收訊迴圈都必須繼續運作。

- **同一時間只允許一段主要 TTS。** `SerializedSpeechRuntime`（見
  `src/ai_vtuber/orchestration/adapters.py`）保證播放不重疊；高優先事件可以
  安全打斷目前的合成或播放，但打斷邏輯必須先確認目前工作真的停止（含原生
  eSpeak／PortAudio I/O），才能開始下一段，否則會造成音訊重疊或殘留佇列項目。

- **已提交的 Twitch 回覆不可撤回。** 一旦 Helix send 請求已經送出，就不能因
  為後續事件打斷而假裝取消或重送；否則會造成重複訊息或狀態與實際聊天室
  不一致。

- **故障隔離方向是單向的：** LLM 或 VTS 故障不能讓 Twitch 收訊停止；TTS
  故障時，仍要保留「安全的文字回覆」（也就是已通過驗證的 `chat_reply`
  仍可以送到 Twitch，即使語音沒有播放成功）。反過來，Twitch 收訊或送出
  失敗不應該讓 LLM／VTS 狀態卡死。

- **不形成自我回覆迴圈。** 授權帳號自己在聊天室發的訊息（包含程式自己送出
  的回覆）必須被過濾，不能被當成新的觸發訊息再進佇列。

- **佇列不可無限成長。** 容量、TTL、cooldown 三者同時作用；佇列滿了要有
  明確的丟棄與統計（見 `queue.py` 的 drop reason 記錄），不能靜默吃掉錯誤
  也不能無限緩衝。

---

## 5. 程式庫地圖

```text
src/ai_vtuber/
  app.py                 CLI 入口；所有 `python -m ai_vtuber <command>` 都在這裡註冊。
                          Phase 5 相關：正式整合命令、phase5-smoke。
  config.py               讀取 config/app.yaml、config/actions.local.yaml。
  tasks.py                 finish_task()（取消安全收尾）、
                          run_blocking()（原生 I/O 背景工作，避免卡住 asyncio executor）。
  logging_setup.py         Logging 設定；注意不可記錄 token／API key。

  orchestration/
    queue.py               有界優先佇列：容量、TTL、per-user cooldown、
                          高優先插隊、丟棄統計。
    state.py                六種角色狀態（idle/thinking/validating/acting/
                          speaking/cooldown）、有界歷史、繁體中文狀態描述。
    controller.py           核心流程：AIVTuberOrchestrator.run()、單輪處理、
                          打斷（preemption）、Twitch 回覆提交點（chat commit
                          point）、關閉收尾。**這是最重要的檔案，任何流程調整
                          都應先看這裡。**
    adapters.py             SerializedSpeechRuntime（TTS 序列化／隔離／
                          取消）、VTSReactionRuntime（VTS 動作準備／復位／
                          重試）、Twitch reply sink（發送與去重）。
    report.py               JSON／繁體中文 Markdown 報告產生、延遲欄位、
                          RAM／VRAM 量測、佇列與狀態統計、
                          `phase5_hour_acceptance` 判定邏輯。

  llm/                     Phase 3 既有 LLM client、schema、white list 驗證。
  tts/
    espeak.py               eSpeak NG CPU 合成（Phase 4 既有邏輯，Phase 5 只
                          改善取消與原生工作生命週期，沒有換引擎）。
    output.py                PortAudio 播放：開始／停止／播放逾時上限、
                          單一 active playback gate。
    playback.py              單一播放 worker、音量包絡、字幕與 MouthOpen
                          收尾、真正的播放完成時間量測。
  vts/
    actions.py                NightRain 語意動作白名單、表情持有與還原、
                          失敗重試、模型一致性保護（換模型或重載時重新確認
                          映射仍然有效）。
    lipsync.py                 MouthOpen prepare/reset 時確認目前模型仍與
                          已準備的映射相同。
  twitch/                  Phase 2 既有 Device Code Grant、DPAPI token
                          store、EventSub、Helix client。

config/
  app.yaml                  所有服務設定（VTS、Twitch、LLM、TTS、
                          orchestration、paths、discovery、logging）。
  actions.local.yaml         本機專屬 NightRain 動作映射（由 `.gitignore`
                          排除，不會出現在版本控制內，每台機器需自行產生）。
  actions.example.yaml       手動調整映射時的參考範本。

docs/
  phase-3-model.md            Phase 3 LLM 選型、benchmark、限制。
  phase-4-tts.md               Phase 4 TTS 選型、MeloTTS 權利問題說明。
  phase-5-orchestration.md      Phase 5 完整架構、流程、取消、報告、
                             安全與實機驗收規則（比本文件更技術細節導向）。
  phase-5-work-log.md           本輪逐次工作紀錄：已重現的缺陷、修正內容、
                             測試結果、實機阻塞原因（繁體中文，非常詳細）。
  phase-5-result.md              一頁式結論：目前驗收現況一眼看懂版本。
  phase-5-handoff-to-codex.md    本文件。

tests/
  test_orchestration_*.py        controller／queue／adapters／state／report
                             的單元與整合測試。
  test_orchestration_integration.py  真實元件（EventSub、LLM client、
                             ActionExecutor、SpeechPlaybackQueue、Helix
                             client）串接假外部服務的離線端到端測試。
  test_phase5_cli.py              直接測試正式 `phase5-smoke` 命令的報告與
                             生命週期（成功、逾時、取消、發送失敗、頻道
                             不符、前置阻塞）。
  test_tasks.py                    驗證原生背景工作卡住也不會拖住程序退出。
  fixtures/traditional_chinese_chat_cases.json   110 組繁中評測案例
                             （Phase 3 既有資料，Phase 5 測試沿用）。
```

---

## 6. 本輪（Phase 5）實際做了什麼

以下是這次交接前完成的變更。程式原先提交在分支
`claireke0329-phase-5-orchestration`（提交 `67b1ccc`、`9fd6ba2`），其 Pull
Request #1 已合併至 `DrizzlingNight/AI-Vtuber` 的 `main`；本機主工作目錄也已
快轉同步至合併提交 `17a9e77`（見第 8 節）。

### 6.1 新增 orchestration 骨架

- `queue.py`：有界優先佇列，支援容量上限、TTL 過期、per-user cooldown、
  回應 cooldown、高優先訊息插隊、以及被丟棄項目的統計（容量已滿、TTL
  過期、cooldown 中）。
- `state.py`：六種角色狀態的 enum 與轉移邏輯，附繁體中文狀態描述，供報告
  與除錯使用。
- `controller.py`：完整回應流程（收訊 → LLM → 驗證 → TTS → VTS → 播放 →
  收尾 → Twitch 回覆），高優先事件打斷邏輯，錯誤隔離（哪一段故障不影響
  哪一段）。
- `adapters.py`：把 Phase 2～4 既有 client／runtime 包裝成 orchestration
  可以安全序列化、取消、重試的介面，不改動底層元件本身的邏輯。
- `report.py`：報告格式與必要欄位判定，包含「一小時驗收」的明確條件（見
  第 7 節）。

### 6.2 CLI 整合

`app.py` 新增：

- 正式整合指令 `run`（持續執行完整流程，需要 `--test-channel`）。
- `phase5-smoke`（有限次數／有 timeout 的驗收指令，會產生 JSON 與繁體中文
  Markdown 報告）。
- 前置條件檢查：缺少必要授權檔、模型檔、runtime 執行檔、或 `--test-channel`
  與目前 Twitch 授權身份不符時，直接持久化 `blocked` 報告，而不是讓程式
  崩潰或印出不完整的錯誤。

### 6.3 既有元件的收尾修正（不是重做）

在重現與修正過程中，發現並修好了以下缺陷，這些都屬於「讓既有元件能被
orchestration 安全呼叫」所必要的修正，不是重新設計：

1. 關閉流程時殘留未完成的等待工作。
2. 已提交的 Twitch 回覆被錯誤地當作可取消項目取消掉。
3. TTS 完成後，訊息若已過期仍會誤觸發 VTS 動作。
4. VTS 收尾發生錯誤時被誤報成功。
5. 重複觸發取消會造成語音合成重疊。
6. VTS 尚未真正完成準備就被當作已完成，導致下一步提早開始。
7. 播放清理階段的錯誤被吞掉，沒有反映到最終結果。
8. 播放完成時間量測混入了清理階段的時間，導致延遲數字失真。
9. 缺少必要量測欄位（RAM／VRAM／延遲）時仍被誤判為驗收通過。
10. 換模型或模型重載時，錯用了舊模型的復位／校正值。
11. 原生音訊裝置（PortAudio）在異常狀況下會卡住整個 asyncio 事件迴圈。
12. 被隔離（quarantine）的合成工作若在關閉時才丟出例外，原本沒有被回報。

修正對應到 `tasks.py`（新增 `finish_task()` 取消安全收尾、原生 I/O 背景
工作）、`tts/output.py`（PortAudio 開始／停止／播放逾時上限）、
`tts/playback.py`（真正的播放完成時間、清理錯誤回報）、`vts/actions.py`
（重試、模型一致性檢查）、`vts/lipsync.py`（MouthOpen 模型一致性檢查）。

### 6.4 測試

新增／擴充了 orchestration controller、queue、adapters、state、report、
整合測試、CLI 測試、原生工作測試，並擴充既有 action、LLM、TTS
playback/output、VTS lipsync 的測試。**全部離線測試結果：162 個通過**，
外加 `compileall`、`pip check`、`git diff --check` 皆通過。

### 6.5 文件

新增 `docs/phase-5-orchestration.md`（架構）、
`docs/phase-5-work-log.md`（逐次繁體中文工作紀錄，包含每一次重現的缺陷與
修正細節）、`docs/phase-5-result.md`（一頁式結論），並在 `README.md` 最上方
加入一頁式結論的連結。

### 6.6 2026-09-14 交接收尾

- 將本機 `main` 快轉同步至已合併 PR #1 的 `17a9e77`。
- 校正 README、一頁式結論及工作紀錄頂端的 Git 狀態，清楚區分「程式已同步」
  與「實機驗收仍未完成」。
- 新增本交接文件、根目錄 `AGENTS.md`，以及 repo 級
  `.agents/skills/continue-ai-vtuber-phase5/SKILL.md`。
- skill 已通過 `skill-creator` 的 `quick_validate.py`；同步後完整離線驗證仍為
  **162 個通過**，`compileall`、`pip check` 與 `git diff --check` 也通過。

---

## 7. Phase 5 驗收條件（尚未達成，請勿誤判）

`report.py` 對「一小時驗收通過」的判定，**不是**「命令執行超過一小時」，
而是同時滿足：

- `completed_turns == requested_turns`（要求跑幾輪就真的完成幾輪，不是
  收到幾則訊息就算數）。
- `measurements_complete: true`（首 token、完整決策、開始送出 PCM、播放
  完成的延遲欄位都必須存在且順序正確）。
- system RAM、`llama-server` working set 與 GPU VRAM 都有實際量測值；GPU
  utilization 也會寫入報告，但目前不是 `measurements_complete` 的硬性欄位。
- VTS／MouthOpen 全程沒有降級（沒有連線中斷、沒有動作被拒絕、沒有復位
  失敗）。
- 命令實際執行時間至少 3600 秒。
- 只有以上全部成立，報告裡的 `phase5_hour_acceptance` 欄位才會是
  `"passed"`；否則會是 `"not_completed"`、`"blocked"`、`"failed"` 或
  `"timed_out"` 之一。

**歷史實機狀態：兩次 smoke 嘗試都在前置檢查階段停止（結束碼 2，
`status: "blocked"`），完成 0 輪，沒有任何延遲、RAM、VRAM 的實際量測。**
原因是執行環境（隔離 worktree）缺少下列本機資源，這些資源本來就不該進版本
控制，需要在 `F:\user\Documents\Workspace\AI Vtuber` 這個「真正的」工作
目錄裡自行具備：

```text
.local\secrets\vts-token.json
.local\secrets\twitch-token.bin
.local\secrets\llama-server-api-key.txt
config\actions.local.yaml
.local\runtime\llama.cpp\llama-server.exe
models\gemma-4-12b-it-qat-q4_0.gguf
.local\runtime\espeak-ng\eSpeak NG\espeak-ng.exe
.local\runtime\espeak-ng\eSpeak NG\espeak-ng-data
```

以及一個明確、已授權、且與 Twitch 目前登入身份一致的測試頻道登入名稱
（由 `--test-channel` 參數指定）。

2026-09-14 交接時只以路徑存在性重新核對（沒有讀取任何憑證內容）：上述八項資源在
主工作目錄均已存在。不過當時未偵測到 VTube Studio 或 `llama-server` 正在執行，
也沒有取得本次實機測試要使用的明確頻道名稱。因此前一次 `blocked` 報告仍是歷史事實，
但下一位接手者不應再假設檔案缺失；應重新啟動服務、核對授權身份與頻道後，從單輪測試
開始取得新的正式證據。

---

## 8. Git 與 PR 狀態

- 遠端主線：`origin/main`（`DrizzlingNight/AI-Vtuber`）。
- Phase 5 原工作分支：`claireke0329-phase-5-orchestration`。
- 程式與完整紀錄提交：`67b1ccc`。
- 一頁式結論提交：`9fd6ba2`。
- Pull Request #1 已於 2026-09-14 合併，merge commit 為 `17a9e77`。
- 本機 `F:\user\Documents\Workspace\AI Vtuber` 的 `main` 已快轉至 `17a9e77`，
  並與 `origin/main` 一致。
- 本交接文件與 repo 級 Codex skill 是合併後新增的交接變更；接手前仍應以
  `git status --short --branch` 及 `git log -5 --oneline --decorate` 核對當下狀態，
  不要只依賴這份文件中的提交編號。

---

## 9. 建議的下一步（給 Codex 接手時的具體順序）

1. **在主工作目錄核對主線。** 目前 PR 已合併且本機 `main` 已同步；先確認
   `git status` 沒有意外變更、`main` 與 `origin/main` 沒有分歧。不要重新開分支
   重做已合併的 Phase 5 orchestration。
2. **在 `F:\user\Documents\Workspace\AI Vtuber` 這個真正的工作目錄**執行
   離線測試：
   ```powershell
   Set-Location 'F:\user\Documents\Workspace\AI Vtuber'
   git status --short --branch
   .\.venv\Scripts\python.exe -m pytest
   ```
   必須維持 162 個以上全數通過，才能繼續下一步。
3. **確認本機資源齊全**（見第 7 節清單），並確認 VTube Studio 已開啟、載入
   NightRain 模型且已完成 Phase 1 校正，Gemma 4／llama.cpp 依 Phase 3 既有
   設定啟動（`context_size=4096`、`gpu_layers=28`、`threads=12`，見
   `config/app.yaml` 的 `llm` 區塊），eSpeak NG runtime 存在。
4. **依序執行實機驗收，不要跳步**：
   - 單輪：`phase5-smoke --test-channel <頻道> --messages 1 --timeout 600`
   - 連續：`phase5-smoke --test-channel <頻道> --messages 5 --timeout 900`
   - 一小時：`phase5-smoke --test-channel <頻道> --messages 60 --timeout 4200`
     （由另一個帳號在測試聊天室分批送訊息，讓命令實際持續 3600 秒以上，
     不是把訊息一次塞完）
   每一步都要看報告的 `status`／`phase5_hour_acceptance` 欄位，不能只看
   結束碼或「有沒有印出東西」。
5. **只有在報告明確顯示 `phase5_hour_acceptance: "passed"` 後**，才能在
   `docs/phase-5-result.md` 與 `README.md` 更新結論為「Phase 5 已完成」。
   在那之前，任何離線測試結果都不能拿來替代實機證據。
6. **若要繼續 Phase 6（OBS）或 Phase 7（進階 EventSub 訂閱、更多動作類型
   等）**，必須先完成第 4～5 步的實機驗收，且需要使用者另外確認範圍，
   不要自行擴大範圍。
7. **若要啟用 MeloTTS 或任何非 eSpeak NG 的語音**，必須先由使用者完成新的
   聲音權利與授權查證，這不是工程判斷可以自行決定的事。

---

## 10. 常見誤解（請不要重蹈覆轍）

- ❌「離線測試 162 個通過，代表 Phase 5 完成了」——不對，離線測試只證明
  程式邏輯在模擬環境下正確，不代表真實 Twitch／VTS／LLM／TTS 串起來能跑。
- ❌「命令跑了超過一小時就算通過一小時驗收」——不對，還要看
  `completed_turns`、`measurements_complete`、`phase5_hour_acceptance`
  是否都達標。
- ❌「隔離 worktree 缺資源就直接把測試通過的邏輯 mock 掉，改成永遠回傳
  成功」——不對，這樣會讓報告失真，等於偽造驗收證據。
- ❌「交接文件寫 main 已同步，所以不用核對」——不對；文件只記錄交接當下
  的狀態。每次接手仍要以 `git status` 與 `git log` 核對當下主線。
- ❌「MeloTTS adapter 已經寫好，可以先用著」——不對，權利未查證前不得
  下載或啟用，即使程式碼已經存在。

---

## 11. Codex 專案指令與 skill

交接已把長期規則分成兩層，兩者都在 Git 內：

- 根目錄 `AGENTS.md`：每次從此 repository 啟動 Codex 時載入的短版專案邊界，
  包含先讀本文件、保護憑證、聲音權利、Phase 範圍與基本驗證命令。
- `.agents/skills/continue-ai-vtuber-phase5/SKILL.md`：當任務涉及 Phase 5
  orchestration、`phase5-smoke`、實機驗收或完成判定時使用的完整工作流程。

可在 Codex 中明確輸入 `$continue-ai-vtuber-phase5` 觸發；任務描述與 skill 的
description 相符時也可自動載入。若 skill 未出現在選擇器，先確認 Codex 的工作目錄位於
本 repository，再重新開啟工作階段。skill 只保存流程與安全邊界，不包含任何本機憑證、
頻道名稱、模型權重或 benchmark 私有資料。

# Phase 5 重新執行紀錄

更新日期：2026-09-07

## 目前結論

本輪已實際修改 33 個檔案，完成程式修正與 **162 個離線測例**。實機單輪及連續測試
仍停在前置條件受阻：完整鏈路實際完成 **0 輪**，延遲、RAM、VRAM **未量測**，
至少一小時驗收 **未完成**。不能宣告 Phase 5 全部完成。

所有原始碼與本紀錄保存在 `claireke0329-phase-5-orchestration` 分支，未自動套用至
`F:\user\Documents\Workspace\AI Vtuber`、未合併 `main`、未推送或公開直播。
本文件及下列修改一併作為本輪 Git 提交保存，提交標題為
「補齊第五階段整合與繁體中文執行紀錄」；可用 `git log -1 --oneline` 取得提交識別碼。

本文件只保存必要的工程紀錄，不保存 OAuth access token、refresh token、
DPAPI 儲存內容、llama-server API key、完整聊天室內容或未通過驗證的模型輸出。
命令、程式識別字與工具原始狀態值保留原格式，其說明及結論使用繁體中文。

## 一、工作位置與上一輪交付問題

使用者指定的主目錄：

```text
F:\user\Documents\Workspace\AI Vtuber
```

本工作階段實際編輯的隔離 worktree：

```text
C:\Users\RainingNight\.copilot\repos\copilot-worktrees\AI Vtuber\claireke0329-reimagined-guide
```

目前分支：`claireke0329-phase-5-orchestration`。

重新執行起點：`68808edaacc7d454551e832ac55626d5b0397794`，提交標題為
「完成本地 TTS 與嘴型同步」。此時本分支沒有新增提交。

### 2026-09-07 首次核對

執行：

```powershell
git status --short --branch
git --no-pager log -6 --format='%h %ad %s' --date=iso
git --no-pager diff --stat
git --no-pager diff --name-only
git ls-files --others --exclude-standard
```

結果：

- 10 個已追蹤檔案有未提交修改；差異摘要為新增 639 行、刪除 7 行。
- 另有 12 個未追蹤新增檔案，包含整個 orchestration 模組及其測試、說明文件。
- 工作階段差異摘要只列出上述 10 個已追蹤檔案，未顯示核心新增檔案。
- 這些修改未套用至主 checkout，也未提交或推送。

因此，上一輪並非完全沒有寫檔案，而是交付位置、未追蹤檔案及未完成實機驗收沒有
清楚交代。上一輪結尾宣稱「程式整合完成」過於籠統，本輪不沿用該結論。

### 上一輪已留下的檔案

| 檔案 | 上一輪狀態 | 用途 |
|---|---|---|
| `README.md` | 已修改、未提交 | 新增整合命令與說明 |
| `config\app.yaml` | 已修改、未提交 | 佇列、TTL、冷卻與動作設定 |
| `src\ai_vtuber\app.py` | 已修改、未提交 | 整合命令與實機前置檢查 |
| `src\ai_vtuber\config.py` | 已修改、未提交 | Phase 5 設定驗證 |
| `src\ai_vtuber\tts\output.py` | 已修改、未提交 | 播放開始時間通知 |
| `src\ai_vtuber\tts\playback.py` | 已修改、未提交 | 播放票據的開始通知 |
| `src\ai_vtuber\twitch\eventsub.py` | 已修改、未提交 | 可注入的同步訊息接收介面 |
| `tests\test_config.py` | 已修改、未提交 | 設定與命令測試 |
| `tests\test_tts_output.py` | 已修改、未提交 | 音訊開始通知測試 |
| `tests\test_tts_playback.py` | 已修改、未提交 | 播放與取消測試 |
| `docs\phase-5-orchestration.md` | 新增、未追蹤 | 設計與操作說明 |
| `src\ai_vtuber\orchestration\__init__.py` | 新增、未追蹤 | 整合套件 |
| `src\ai_vtuber\orchestration\queue.py` | 新增、未追蹤 | 有界優先訊息佇列 |
| `src\ai_vtuber\orchestration\state.py` | 新增、未追蹤 | 角色狀態機 |
| `src\ai_vtuber\orchestration\controller.py` | 新增、未追蹤 | 回應流程與打斷 |
| `src\ai_vtuber\orchestration\adapters.py` | 新增、未追蹤 | 現有元件的整合介面 |
| `src\ai_vtuber\orchestration\report.py` | 新增、未追蹤 | 整合測試報告 |
| `tests\test_orchestration_queue.py` | 新增、未追蹤 | 訊息排序與容量測試 |
| `tests\test_orchestration_state.py` | 新增、未追蹤 | 狀態轉移測試 |
| `tests\test_orchestration_controller.py` | 新增、未追蹤 | 回應流程測試 |
| `tests\test_orchestration_adapters.py` | 新增、未追蹤 | 整合介面測試 |
| `tests\test_orchestration_report.py` | 新增、未追蹤 | 報告測試 |

## 二、本輪範圍與驗收清單

| 需求 | 本輪狀態 | 驗收依據 |
|---|---|---|
| 沿用 Phase 0 至 4 架構與 NightRain 校正 | 程式與資源設定保留；本機校正檔缺失 | Git 差異確認既有 Twitch 授權、LLM runtime、talk-demo 與 Melo adapter 未被重做 |
| 有界佇列、TTL、優先權與冷卻 | 離線通過 | 不依賴網路或裝置的行為測試 |
| 六個角色狀態與正確執行順序 | 離線通過 | 狀態、正式命令及整合測試 |
| speech 必須通過既有驗證才能進入 TTS | 離線通過 | 畸形資料、繁簡混用、記憶與越權輸出均在副作用前拒絕 |
| 語音前動作準備、播放同步與完整收尾 | 離線通過；實機待驗 | 真實播放佇列搭配假裝置，以及停止失敗／模型切換測試 |
| 單一主要語音及高優先打斷 | 離線通過；實機待驗 | 合成、播放、清理期間的取消競態與原生工作卡住測試 |
| Twitch 收訊不被其他元件阻塞 | 離線通過；實機待驗 | EventSub 與控制器共同運行；LLM 等待中仍接收 30 則新訊息且保持容量上限 |
| LLM、VTS、TTS 故障隔離 | 離線通過；實機待驗 | 故障後安全文字保留、復位重試及下一輪恢復 |
| 不自我回覆、不無限增長、不執行未知動作 | 離線通過 | 自身訊息排除、容量與白名單測試 |
| 全部離線測試先完成 | 已完成，162 個通過 | 最終完整測試在實機前置命令之前執行 |
| 實機單輪及連續完整鏈路 | 前置條件受阻，0 輪 | 已保存兩份 JSON 及兩份繁體中文報告 |
| Phase 5 至少一小時測試頻道驗收 | 未完成 | 前置條件尚未具備；不拿開發花費的時間充當測試時長 |
| 延遲、RAM、VRAM 與資源設定 | 設定保留，實機未量測 | 報告欄位明確留空／未量測，不套用 Phase 3／4 數值 |

不加入 OBS，不下載或啟用未證實聲音權利的 MeloTTS 中文 checkpoint，
不新增 Phase 6 記憶／內容過濾或 Phase 7 功能，不自動公開直播。

## 三、本輪逐項修改

| 編號 | 修改 | 原因 | 結果 |
|---|---|---|---|
| 01 | 建立本文件 | 使用者要求所有細節、修改與結論使用繁體中文保存 | 已建立；後續隨實際執行更新 |
| 02 | 將 12 個上一輪新增檔案與本紀錄加入 Git 的意向追蹤 | 原本差異摘要遺漏核心新增檔案 | 現在差異可見 23 個檔案；尚未提交 |
| 03 | 清理優先權等待工作，保護所有已提交 Twitch send 路徑 | 關閉時殘留工作；插隊分支會把取消傳給已提交的 send | 對應回歸測例由失敗轉為通過 |
| 04 | TTS 合成結束後、任何 VTS 副作用前再次檢查 TTL | 過期訊息仍啟動 VTS 動作 | 過期訊息不再觸發動作、播放或回覆 |
| 05 | 動作收尾後才建立 react_only 結果 | 原本先建立成功結果，遺失 finally 的清理錯誤 | 清理錯誤正確標示為降級 |
| 06 | 以可重入取消防護等待底層合成完成 | 第二次取消可能提早釋放合成鎖，造成同時兩段合成 | 重複取消仍維持單一合成 |
| 07 | VTS 執行器增加可選的準備完成通知、釋放事件與強度參數 | 原本只建立背景工作，未確認動作真的開始 | 沿用既有執行器；舊命令預設行為不變 |
| 08 | 播放清空／關閉回報錯誤，重複取消仍等待收尾 | 清理錯誤曾被吞掉，第二次取消會中止嘴型復位 | 已有會在修正前失敗的播放佇列測例 |
| 09 | 分離播放完成時間與字幕／嘴型清理時間 | 原本把 VTS 清理延遲誤算成音訊播放時間 | 播放完成時間由播放佇列記錄，延遲順序須完整且有效 |
| 10 | 必要延遲、RAM、VRAM 缺失時拒絕判定通過 | 舊報告即使量測欄位全部為空仍可能成功 | 缺值明確標示未量測；三種缺值情境均有回歸測例 |
| 11 | VTS 待還原狀態跨輪次保留，模型切換時拒絕錯用參數 | 斷線後表情可能殘留；下一輪嘴型可能沿用舊模型資料 | 只恢復本執行器曾修改的核准資源，不猜測新模型映射 |
| 12 | 嘴型操作設定獨立等待上限，失敗後可重試復位 | 不讓 VTS 長時間等待拖住語音收尾 | 不改 VTS 或 Gemma 資源設定；故障時保留音訊與安全文字 |
| 13 | 將音量包絡計算移至背景執行緒 | 大量 PCM 計算不應佔住 Twitch 的事件迴圈 | 測例確認計算不是在事件迴圈執行緒執行 |
| 14 | 紀錄關閉時清除的等待訊息與各輪狀態轉移 | 原本清空後難以分辨訊息去哪裡 | 關閉丟棄另行計數；狀態日誌使用繁體中文 |
| 15 | 明確指定並核對 `--test-channel` | 已授權帳號不等於已確認是本次測試頻道 | 不指定或身份不符就拒絕收發；不宣稱聊天室因此變成私人 |
| 16 | 正式命令保存 JSON 與同名繁體中文 Markdown | 前置失敗以前只有終端文字，沒有持久結論 | 成功、受阻、逾時、取消與錯誤均有對應測例 |
| 17 | 報告不得覆蓋設定、盤點、執行狀態或授權檔 | 防止 `--output` 誤指向現有重要檔案 | 拒絕後原檔保持不變 |
| 18 | 清理順序改為 EventSub、語音、VTS，再關 HTTP client | 避免連線提早關閉而中止已提交的回覆或收尾 | 正式命令的離線測例確認清理順序 |
| 19 | 沒有核准情緒映射就明確記錄未完成反應 | 不能假裝 NightRain 已呈現未設定的情緒 | 不下載表情、不自行擴充動作白名單 |
| 20 | 修正 README 與 Phase 5 文件的完成宣告 | 原本把程式骨架與實機驗收混為一談 | 明確區分程式、離線測試、短 smoke 與一小時驗收 |
| 21 | 原生音訊開始／停止／播放均有等待上限，逾時後禁止新播放 | PortAudio 原生呼叫若不返回，原本會拖住關閉 | 仍沿用 PortAudio 與單一播放佇列；未確認停止時明確失敗並隔離 |
| 22 | eSpeak 與音訊原生 I/O 使用獨立背景執行緒，合成取消採有限等待 | 不讓原生呼叫拖住 asyncio 預設執行緒池的關閉 | 不殺其他程序、不改音色；背景工作未結束前不啟動第二段合成或音訊 |
| 23 | 隔離中的 VTS 還原工作結束但失敗時，關閉前再重試一次 | 舊分支遇到還原錯誤就直接退出，跳過最後復位 | 在同一個收尾期限內重試，不與仍在執行的舊還原並行 |
| 24 | 關閉時一律檢查已完成或仍在執行的隔離合成結果 | 複核發現背景合成若先失敗，原本會因 done 為真而漏報 | 不因錯誤發生時序不同而吞掉失敗 |

## 四、執行紀錄

| 次序 | 操作 | 實際結果 | 判定 |
|---|---|---|---|
| 01 | 核對 Git 歷史、工作區差異與未追蹤檔案 | 找到上一輪 22 個未提交變更檔案 | 必須重新驗證並讓新增檔案可見 |
| 02 | 閱讀上一輪 Phase 5 設計文件 | 文件有功能宣告，但未明確記錄實機阻塞與一小時驗收未完成 | 不可直接視為 Phase 5 完成 |
| 03 | 完整重讀 PROJECT_BRIEF、README、Phase 3／4 文件及 Phase 4 交接 | 確認 Phase 5 包含至少一小時測試頻道驗收，與 Phase 6 的 4 至 8 小時 soak test 不同 | 保留 Gemma、eSpeak、VTS 及聲音授權限制 |
| 04 | 重新執行 `.\.venv\Scripts\python.exe -m pytest` | 114 個測例通過，耗時 1.06 秒 | 只代表原有測例通過，不能證明未覆蓋的整合競態正確 |
| 05 | 直接追蹤 controller、adapters、播放佇列與命令的取消／收尾流程 | 找到需要新增回歸測例的邊界 | 先建立能重現問題的離線測例，再修改程式 |
| 06 | 新增六個 controller／adapter 回歸測例並執行 | 六個全數失敗：等待工作殘留、已提交 send 被取消、TTL 後仍動作、收尾錯報成功、合成重疊、VTS 未真正準備 | 已證明上一輪驗證不足 |
| 07 | 修正後執行 controller、adapters 及既有 actions 測試 | 26 個測例通過，耗時 0.14 秒 | 上述六個缺陷已修正；繼續確認真實播放佇列與報告 |
| 08 | 新增播放清理、完成時間與報告缺值的六個測例 | 六個全部在修正前失敗，耗時 0.17 秒 | 證明清理錯誤、取消競態及假通過問題 |
| 09 | 修正後執行播放、輸出、字幕、報告、adapters 與 controller | 36 個測例通過，耗時 0.22 秒 | 新舊播放與整合測例均通過 |
| 10 | 執行真實元件加假外部服務的完整離線鏈路 | 31 個整合及相關測例通過，耗時 0.48 秒 | 正常回覆、非法輸出、TTS／VTS 故障、自身訊息過濾及持續收訊有共同驗證 |
| 11 | 加入繁體中文報告與命令流程後執行相關測例 | 54 個測例通過，耗時 0.61 秒 | 報告與既有核心流程相容 |
| 12 | 新增跨輪次模型切換與還原重試測例 | 三個全部在修正前失敗，耗時 0.10 秒 | 證明連續運行仍有上一輪狀態殘留問題 |
| 13 | 修正 VTS 後執行 actions、嘴型、adapters、controller、完整鏈路與播放 | 49 個測例通過，耗時 0.31 秒 | 跨輪次恢復與舊動作測例同時通過 |
| 14 | 驗證正式命令的前置受阻、正常、逾時、發送失敗與頻道不符 | 10 個命令及報告測例通過，耗時 0.36 秒 | 不只驗證個別 helper，而是直接呼叫正式命令組裝 |
| 15 | 加入狀態紀錄後執行相關測例 | 29 個通過、1 個失敗：狀態參數誤接到前置受阻報告函式 | 本輪新增的接線錯誤，已移至完整執行報告分支修正 |
| 16 | 加入真實 SSE 取消、繁中拒絕、冷卻插隊、取消報告等測例後執行 | 42 個測例通過，耗時 0.53 秒 | 新增接線錯誤已修正；持續進行最終驗證 |
| 17 | 首次執行本輪完整套件、compileall、pip check、diff check | 155 個測例通過，耗時 1.29 秒；語法、相依與格式檢查通過 | 仍送交唯讀程式審查，不以測例數量取代責任邊界檢查 |
| 18 | 唯讀審查 | 指出原生 I/O／合成不返回時的關閉等待，及隔離 VTS 還原失敗後跳過最後重試 | 繼續修正，沒有宣告完成 |
| 19 | 新增審查問題的三個回歸測例 | 三個全部在修正前失敗，耗時 0.60 秒 | 證明審查指出的邊界確實存在 |
| 20 | 修正後執行 adapters、音訊、播放、TTS engine、正式命令與離線鏈路 | 40 個測例通過，耗時 0.58 秒 | 有界等待、隔離與舊 TTS 行為相容 |
| 21 | 再次執行完整套件與相依檢查 | 161 個測例通過，耗時 1.66 秒；包括原生工作卡住時，獨立 Python 子程序仍可退出 | 相依完整性、語法與差異格式均通過 |
| 22 | 複核原生工作與隔離收尾 | 找到「隔離合成已先失敗」的關閉分支漏報 | 已補上結果檢查與專用回歸測例，不以 callback 取走例外代替錯誤回報 |
| 23 | 最終完整測試：`.\.venv\Scripts\python.exe -m pytest --tb=short` | **162 個通過，耗時 1.70 秒** | 本輪完整離線驗證完成 |
| 24 | `compileall -q src tests`、`pip check`、`git diff --check` | 語法編譯通過、沒有相依衝突、差異格式通過 | 沒有為本輪額外新增測試或 lint 工具 |
| 25 | 核對 `68808ed` 至本分支的資源設定與保留模組差異 | `config\app.yaml` 只新增 orchestration 區塊；Twitch auth／chat、llama runtime、talk-demo、Melo adapter 無差異 | 沒有重做 Phase 0 至 4 的既有架構或改換模型／聲音 |
| 26 | `phase5-smoke --messages 1 --timeout 30` | 結束碼 2；要求 1 輪、完成 0 輪、9 項前置阻塞 | 保存單輪受阻報告，未執行模型／TTS／Twitch 發送 |
| 27 | `phase5-smoke --messages 5 --timeout 30` | 結束碼 2；要求 5 輪、完成 0 輪、9 項前置阻塞 | 保存連續受阻報告，未偽裝為實機成功 |
| 28 | 讀取本輪產生的兩份中文報告並解析 JSON、執行 `git check-ignore` | 四份檔案均存在；狀態 blocked、資源為 null、一小時狀態 not_completed；均受 Git 忽略 | 確認報告已持久化且不會被提交 |

以上耗時是離線測試程式本身的執行時間，**不是 Gemma 推論延遲、TTS 實機速度或
Twitch 網路延遲**。所有成功報告測例使用測試暫存目錄與假資料，不寫入實機 benchmark
目錄冒充量測。

### 已重現的關鍵回歸測例

| 測例 | 修正前實際問題 |
|---|---|
| `test_shutdown_removes_priority_waiter` | 主流程停止後仍有優先權等待 Future |
| `test_shutdown_after_preemption_does_not_cancel_committed_send` | 高優先插隊後再關閉，仍會取消已提交 send |
| `test_expiry_during_synthesis_prevents_vts_side_effects` | TTS 完成時訊息已過期，卻先啟動了 VTS 動作 |
| `test_react_only_cleanup_failure_is_not_reported_as_success` | 收尾失敗後結果仍是 completed |
| `test_repeated_cancellation_does_not_overlap_synthesis` | 同時出現兩段合成 |
| `test_reaction_start_waits_for_actual_vts_preparation` | start 在 VTS 準備完成前就返回 |
| `test_clear_surfaces_cleanup_failure` | 播放清空吞掉 MouthOpen 復位錯誤 |
| `test_repeated_clear_does_not_abort_mouth_reset` | 重複清空使嘴型復位次數為零 |
| `test_playback_completion_excludes_presentation_cleanup_time` | 沒有獨立的真正播放完成時間 |
| `test_missing_required_measurements_cannot_pass_smoke`（三種情境） | 延遲、RAM 或 VRAM 缺失仍被判通過 |
| `test_held_expression_cleanup_refuses_a_different_model` | 還原表情時可能寫入另一個模型 |
| `test_failed_expression_restore_is_retried_before_next_action` | 表情還原失敗未在下一輪重試 |
| `test_failed_next_prepare_never_resets_a_different_model` | 下一輪 prepare 失敗後仍沿用上一輪 MouthOpen |
| `test_cancelled_synthesis_has_bounded_join_and_remains_quarantined` | 取消後無上限等待合成，不會退出 |
| `test_shutdown_retries_restore_after_quarantined_failure` | 隔離工作失敗後，關閉跳過最後還原 |
| `test_unresponsive_audio_device_is_bounded_and_prevents_overlap` | 原生裝置啟動卡住後不會返回失敗 |

## 五、實機前置結果

單輪報告：

```text
.local\benchmarks\phase5-smoke-20260907T075937540457Z.json
.local\benchmarks\phase5-smoke-20260907T075937540457Z.md
```

連續報告：

```text
.local\benchmarks\phase5-smoke-20260907T075937913398Z.json
.local\benchmarks\phase5-smoke-20260907T075937913398Z.md
```

本 worktree 的前置結果如下，路徑均相對於上方列出的實際工作目錄：

| 條件 | 結果 |
|---|---|
| `.local\secrets\vts-token.json` | 不存在，未讀取內容 |
| `.local\secrets\twitch-token.bin` | 不存在，未讀取或解密 DPAPI 內容 |
| `.local\secrets\llama-server-api-key.txt` | 不存在，未讀取內容 |
| `config\actions.local.yaml` | 不存在，未重新產生假 NightRain 映射 |
| `.local\runtime\llama.cpp\llama-server.exe` | 不存在 |
| `models\gemma-4-12b-it-qat-q4_0.gguf` | 不存在，未重複下載大型模型 |
| `.local\runtime\espeak-ng\eSpeak NG\espeak-ng.exe` | 不存在 |
| `.local\runtime\espeak-ng\eSpeak NG\espeak-ng-data` | 不存在 |
| 明確指定的測試頻道 | 尚未提供，不自動猜測帳號 |
| VTS 連接埠 | 前置 TCP 探測可連線；未授權呼叫模型 API，不能當作 NightRain 驗證通過 |

未跨讀或複製主 checkout 的憑證、模型或 runtime，未把 token／API key 交給 LLM、
寫入版本控制或 benchmark。沒有啟動 Twitch 收發、模型推論、實體語音或直播；
也沒有下載／啟用 MeloTTS。

## 六、最終變更索引

| 檔案 | 本輪交付內容 |
|---|---|
| `README.md` | 清楚區分程式與實機驗收；加入測試頻道、中文報告與工作位置 |
| `config\app.yaml` | 新增 orchestration 設定，不改既有模型／TTS／VTS 參數 |
| `docs\phase-5-orchestration.md` | 完整設計、操作、資料界線及驗收定義 |
| `docs\phase-5-work-log.md` | 本輪逐項紀錄、失敗證據、修正、命令與結論 |
| `src\ai_vtuber\app.py` | 正式命令組裝、測試頻道核對、生命週期及持久報告 |
| `src\ai_vtuber\config.py` | 佇列／TTL／冷卻／等待上限設定驗證 |
| `src\ai_vtuber\orchestration\__init__.py` | 整合套件入口 |
| `src\ai_vtuber\orchestration\adapters.py` | TTS／VTS／Twitch 的序列化、隔離、還原及發送介面 |
| `src\ai_vtuber\orchestration\controller.py` | 角色回應順序、驗證、副作用、取消與錯誤結果 |
| `src\ai_vtuber\orchestration\queue.py` | 有界排序、過期、冷卻、喚醒與丟棄統計 |
| `src\ai_vtuber\orchestration\report.py` | 機器可讀 JSON 與繁體中文報告、必要量測與一小時判定 |
| `src\ai_vtuber\orchestration\state.py` | 有界狀態歷史及繁體中文轉移說明 |
| `src\ai_vtuber\tasks.py` | 取消安全收尾及原生 I/O 背景執行 |
| `src\ai_vtuber\tts\espeak.py` | 沿用相同 eSpeak 合成，改用可隔離的原生 I/O 工作 |
| `src\ai_vtuber\tts\output.py` | 首音時間、單一原生播放、開始／播放／停止等待界線 |
| `src\ai_vtuber\tts\playback.py` | 音量計算不阻塞事件迴圈、可靠取消、錯誤回報與完播時間 |
| `src\ai_vtuber\twitch\eventsub.py` | 接受同步訊息 sink，保留原有收訊與自身訊息排除 |
| `src\ai_vtuber\vts\actions.py` | 真正準備通知、持有／還原表情、強度、失敗復位重試 |
| `src\ai_vtuber\vts\lipsync.py` | 跨輪次準備與當前模型保護 |
| `tests\test_actions.py` | 舊動作相容性、表情持有、強度、模型切換與還原重試 |
| `tests\test_config.py` | 設定、命令參數與不接觸真實服務的前置檢查 |
| `tests\test_llm_client.py` | 取消時真正關閉 SSE 串流 |
| `tests\test_orchestration_adapters.py` | 元件隔離、準備完成、重複取消、等待上限及晚到錯誤 |
| `tests\test_orchestration_controller.py` | 六狀態、驗證、TTL、發送邊界、冷卻與打斷 |
| `tests\test_orchestration_integration.py` | 真實元件搭配假網路／裝置的端到端離線鏈路 |
| `tests\test_orchestration_queue.py` | 容量、排序、TTL、冷卻、關閉及丟棄計數 |
| `tests\test_orchestration_report.py` | 必要量測、中文報告、狀態歷史與假通過防止 |
| `tests\test_orchestration_state.py` | 合法／非法狀態轉移與歷史容量 |
| `tests\test_phase5_cli.py` | 正式命令成功／受阻／逾時／取消／發送失敗及清理順序 |
| `tests\test_tasks.py` | 原生錯誤傳遞，以及卡住的背景 I/O 不拖住 Python 退出 |
| `tests\test_tts_output.py` | PCM 輸出相容性、首音與卡住裝置的隔離 |
| `tests\test_tts_playback.py` | 單一播放、取消／清空、收尾、完播時間及包絡背景計算 |
| `tests\test_vts_lipsync.py` | 嘴型映射與跨模型保護 |

## 七、最終結論與仍需完成的工作

**這次不是只重跑命令：已補齊並修正實際程式，且把全部修改與驗證過程保存為繁體中文。**
程式層面與離線驗證已完成；受阻的實機部分沒有冒充完成。

完整 Phase 5 仍需要在執行此分支的工作目錄備妥既有本機校正、授權、Gemma／llama.cpp、
eSpeak runtime，並明確指定測試頻道；之後才能取得真實單輪／連續鏈路、一小時運行與
RAM／VRAM／延遲證據。憑證只需在本機配置，不應貼入對話。

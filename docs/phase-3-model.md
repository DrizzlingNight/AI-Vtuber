# Phase 3 本地模型選型記錄

更新日期：2026-09-05

## 最終首測模型

| 項目 | 固定值 |
|---|---|
| 模型 | `google/gemma-4-12B-it-qat-q4_0-gguf` |
| Revision | `29d097773436b69ff9feafd636ab4cf873786537` |
| 檔案 | `gemma-4-12b-it-qat-q4_0.gguf` |
| 參數量 | 11.95B dense |
| 量化 | Google QAT Q4_0 |
| 檔案大小 | 6,975,879,296 bytes（約 6.50 GiB） |
| SHA-256 | `93567e57a8fe10b23569b9d9ec38cd005deedf71e29477c421a4b83f418a538b` |
| 授權 | Apache-2.0 |
| Context | 專案限制為 4,096 tokens |
| Thinking | 關閉 |
| GPU offload | 起始實測 28 / 48 layers |

模型卡說明 Gemma 4 12B 支援原生 system role、function calling、140+ 訓練語言與
256K context。本專案只使用文字與 4K context，因此不下載 175,115,616-byte 的
`mmproj` 多模態檔。

來源：

- [Gemma 4 12B 官方模型卡](https://huggingface.co/google/gemma-4-12B-it)
- [官方 QAT Q4_0 GGUF 與固定 revision](https://huggingface.co/google/gemma-4-12B-it-qat-q4_0-gguf/tree/29d097773436b69ff9feafd636ab4cf873786537)
- [Gemma 4 Apache-2.0 授權頁](https://ai.google.dev/gemma/docs/gemma_4_license)

Apache-2.0 允許商業使用與修改；若重新散布模型或衍生物，仍須提供授權副本、保留
attribution／NOTICE，並標示修改。模型與商標沒有擔保或額外商標授權。

## 為何不再使用 Qwen3-8B

本機既有實測顯示 Qwen3-8B 的直播聊天文字不像人類自然說話。Phase 3 的核心目標是
繁中短句、角色互動與 Twitch 即時口語，而不是只追求一般推理榜分數，因此不再下載或
採用 Qwen3-8B。

Gemma 4 12B 是較新的 12B 級模型，有官方 GGUF、乾淨的 Apache-2.0 授權和原生結構化
能力。公開 benchmark 不能證明台灣聊天室自然度，所以最終判斷仍以專案內 110 組繁中
測例及人工閱讀為準。

## 備選模型

若 Gemma 4 的口吻仍有翻譯腔，或 RTX 2080 部分 CPU offload 使延遲不可接受，下一個
比較對象是 `MediaTek-Research/Llama-Breeze2-8B-Instruct` 的 text-only Q4_K_M：

- 約 4.92 GB，較容易與 VTube Studio 共存。
- MediaTek 專為繁體中文與台灣文化語境持續訓練。
- 官方權重採 Llama 3.2 Community License；GGUF 為第三方轉檔，不像 Gemma 有官方
  GGUF，因此只列為 fallback。

來源：

- [Llama-Breeze2-8B-Instruct 官方模型卡](https://huggingface.co/MediaTek-Research/Llama-Breeze2-8B-Instruct)
- [Llama-Breeze2 論文](https://arxiv.org/abs/2501.13921)

## llama.cpp runtime

| 項目 | 固定值 |
|---|---|
| Release | `b10621`，commit `c1d0e7a004015f23bc0233470b747b596f29b264` |
| 對應 stable | `v0.3.0` |
| Backend | Windows x64 CUDA 12.4 |
| 主程式 ZIP SHA-256 | `81c2ff62e14b549cd5c766ccdd5c61f09e821a171655c3047bdccfddc2d1a1e2` |
| CUDA runtime ZIP SHA-256 | `8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6` |

來源：

- [llama.cpp b10621 release](https://github.com/ggml-org/llama.cpp/releases/tag/b10621)
- [llama.cpp server 與 schema-constrained JSON](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)
- [llama.cpp JSON Schema／GBNF 限制](https://github.com/ggml-org/llama.cpp/blob/master/grammars/README.md)

## 下載前資源估算

在 RTX 2080 8 GB、VTube Studio 已使用約 3.27 GiB VRAM、4K context、28 layers
offload 的條件下，下載前估算如下：

| 指標 | 估算 |
|---|---|
| `llama-server` RAM | 約 8～10 GiB |
| LLM GPU VRAM | 約 3.2～3.8 GiB |
| 生成速度 | 約 7～12 token/s |
| 首 token | 約 0.8～3 秒，視 prompt cache 與 CPU offload 而定 |

這些數字不是官方實測；以下本機結果已取代它們作為 Phase 3 基線。

## 本機正式 benchmark

正式報告：`.local/benchmarks/llm-benchmark-20260904T180344Z.json`

條件：

- Intel Core i7-12700K、64 GB RAM、NVIDIA RTX 2080 8 GB。
- llama.cpp `b10621` CUDA 12.4，4K context，28 / 48 layers GPU offload。
- VTube Studio 與 NightRain 全程開啟。
- 110 組繁中案例逐一執行，固定 seed 42，單一 server slot。
- llama.cpp JSON Schema grammar 後，再經 Pydantic、繁中腳本與白名單驗證。

結果：

| 指標 | 結果 |
|---|---:|
| 直接接受 | 109 / 110 |
| 安全拒絕 | 1 / 110 |
| schema＋語言＋白名單接受率 | 99.09% |
| decision 標註命中 | 108 / 110（98.18%） |
| TTFT min／p50／p95／max | 1.45／1.63／1.79／2.02 秒 |
| 總生成 min／p50／p95／max | 7.18／11.19／15.02／16.75 秒 |
| token/s min／p50／p95／max | 5.05／6.11／6.24／6.27 |
| VTS 連線探測 | 1,936 / 1,936 全程在線 |
| 合併 GPU VRAM baseline／peak | 7,207／7,311 MiB |
| VTS-only GPU VRAM（server 停止後） | 2,657 MiB |
| LLM 路徑增加 GPU VRAM | 約 4,654 MiB |
| `llama-server` working set peak | 8,787 MiB（約 8.58 GiB） |
| 同場 system RAM peak | 27,990 MiB |
| server 停止後 system RAM | 18,420 MiB |
| LLM 路徑增加 system RAM | 約 9,570 MiB（約 9.35 GiB） |

唯一安全拒絕發生在首次追隨歡迎句：模型混用了「欢迎加入我的直播间」等簡體字。
程式在輸出層攔截，沒有交給 Twitch 或 VTube Studio。另一個 decision 標註落差是把
「純簽到，祝妳直播順利」選成 `reply`，內容只是道謝，不涉及越權。15 組提示注入、
秘密與任意 action 案例全部選成 `ignore`。

自然度抽查顯示，Gemma 4 12B 能穩定產生簡短繁中口語、同理回覆與角色互動，明顯符合
本專案「像人類說話」的方向；仍偶爾偏泛用實況主語氣，因此角色 persona 之後需要由
實際人設內容再校正。Phase 3 先採 Gemma 4 12B 為基線，不下載 Breeze2。若 Phase 4／5
加入其他負載後無法接受 p50 11.19 秒的完整 JSON 延遲，再以同一 110 組資料比較
Llama-Breeze2-8B-Instruct，而不是退回 Qwen3-8B。

## 安全與範圍

- `llama-server` 只綁定 `127.0.0.1`，CORS 僅允許 localhost，Web UI 關閉。
- 推論 API 使用隨機本機 key；key 位於 `.local/secrets/`，不提交也不進入模型 prompt。
- OAuth access／refresh token、DPAPI store 與 Twitch auth 物件不會傳給 LLM client。
- 輸出只允許 `reply`、`react_only`、`ignore`；未知 emotion／action 會安全拒絕。
- Production action 只開放既有 NightRain 白名單中的 `continuous_test`；生氣表情、
  生氣熱鍵、嘴型與 talk-demo 底層通道都不開放。
- Phase 3 不保存記憶，`memory_note` 強制為 `null`。
- 未加入 TTS、OBS，也未串接 Twitch -> LLM -> VTS 自動流程。

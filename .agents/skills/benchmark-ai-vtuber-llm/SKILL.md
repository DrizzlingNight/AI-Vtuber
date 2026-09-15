---
name: benchmark-ai-vtuber-llm
description: 驗證、比較或調整此程式庫的本地 llama.cpp／Gemma LLM、結構化輸出與繁體中文 benchmark。當工作涉及 llm-serve、llm-status、llm-chat、llm-benchmark、模型、prompt、schema、生成參數或 RAM／VRAM／延遲比較時使用；不適用於 Twitch、VTS、TTS、整體 orchestration 或 Phase 完成判定。
---

# 評測 AI VTuber 的本地 LLM

使用同一套繁中案例、結構化輸出契約與資源條件，比較本地模型或設定變更；歷史基線與
當次量測必須分開呈現。

## 載入必要情境

1. 從程式庫根目錄檢查 `git status --short --branch`。
2. 完整閱讀 `docs/phase-3-model.md`，再閱讀 README 的「本地 LLM」、「結構化輸出」與
   「110 組繁中評測與 benchmark」。
3. 只有在修改或診斷程式時，才閱讀 `config/app.yaml` 的 `llm` 區塊、
   `src/ai_vtuber/llm/`、CLI 組裝、評測資料與直接相關測試。
4. 把文件中的 benchmark 當作歷史基線；server、PID、模型檔、runtime 與資源用量必須
   在本機重新確認。

## 保護模型與機密

- `.local/secrets/llama-server-api-key.txt` 只能由既有程式使用；人工檢查只確認存在，
  不得開啟、輸出、寫入 URL、提示詞、報告或 Git。
- llama-server 只能使用 loopback endpoint，不加入 URL credentials、不開 Web UI，也不
  把 Twitch／VTS token、授權物件或任意外部 payload 送入模型。
- 模型、runtime 與 `.local/benchmarks/` 保持不受 Git 追蹤。未經使用者要求及授權／資源
  評估，不下載、替換或重新散布模型。

## 驗證與 benchmark

1. 先用 `llm-status` 核對 server、模型與實際 allowlist；只有任務需要啟動本機服務時才
   執行長駐的 `llm-serve`，並記錄其 PID 供 RAM 量測。
2. 用具代表性的 `llm-chat` 檢查結構化輸出。輸出必須通過 JSON Schema、Pydantic、繁中
   與 emotion/action allowlist；拒絕的輸出不得修補後執行。
3. 需要正式驗收、模型比較，或 prompt／schema／生成參數變更的回歸證據時，保持 VTube
   Studio 開啟並執行 `llm-benchmark --server-pid <pid>`。使用預設 110 組資料與相同條件；
   除非任務明確要求，不更換案例或降低 `minimum-schema-rate`。
4. 比較 schema＋語言＋白名單接受率、decision 命中率、TTFT p50／p95、總生成 p50／p95、
   token/s、system RAM、llama-server working set、GPU VRAM 與 VTS 全程在線狀態。
5. 目前歷史基線為接受率 99.09%、decision 命中率 98.18%、TTFT p50／p95 1.63／1.79 秒、
   總生成 p50／p95 11.19／15.02 秒、token/s p50 6.11；只用來比較，不冒充當次結果。

## 回報與驗證

保留正式本機報告，並清楚列出環境、模型／runtime 身分、案例數、安全拒絕、品質、延遲與
資源差異。沒有量測時標示 `not measured`，不要用 0 或歷史數字代替。

修改 Python、prompt、schema 或生成設定後，執行專案 `AGENTS.md` 的完整基本驗證；正式
品質結論還必須有當次 benchmark。只改文件或 skill 時至少執行 `git diff --check`，並驗證
本 skill。

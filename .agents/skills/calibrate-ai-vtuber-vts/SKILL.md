---
name: calibrate-ai-vtuber-vts
description: 盤點、校正、驗證或修復此程式庫的 VTube Studio 模型資源與本機動作映射。當工作涉及 inventory、actions.local.yaml、模型切換或重載、MouthOpen、smoke 或 talk-demo 時使用；不適用於 Twitch、LLM、整體 orchestration 或 Phase 完成判定。
---

# 校正 AI VTuber 的 VTube Studio

以目前載入的真實模型資源建立或驗證本機語意映射；不猜測 Live2D 參數、表情或熱鍵。

## 載入必要情境

1. 從程式庫根目錄檢查 `git status --short --branch`。
2. 閱讀 README 的「VTube Studio 首次授權與盤點」、「Smoke test」與「20 秒說話動作
   示範」。手動調整映射前再讀 `config/actions.example.yaml`。
3. 只有在修改或診斷程式時，才閱讀 `src/ai_vtuber/vts/` 下與問題直接相關的 client、
   inventory、actions、lipsync、talk_demo 及對應測試。
4. 文件中的模型名稱、ID 與校正值可能過期；每次實機工作都重新向 VTS 盤點。

## 保護本機資源

- `.local/secrets/vts-token.json` 只能確認存在，不得開啟、輸出、複製到報告、提示詞或 Git。
- `.local/state/vts-inventory.json` 與 `config/actions.local.yaml` 維持本機、不受 Git 追蹤；
  不要整份貼到對話或日誌。
- 不修改 Live2D 原始模型，不下載或捏造缺少的資源。找不到的項目保留在
  `missing_resources`。
- `inventory --overwrite-actions` 會取代本機映射；只有使用者明確要求重新產生，且已
  核對目標模型與現有映射後才能執行。一般盤點不得順手覆寫已校正映射。

## 實機校正流程

1. 確認 VTube Studio 已載入目標模型並開啟 Plugin API，先執行 `health`，再執行
   `inventory`。首次授權由使用者在 VTS 視窗確認插件名稱與開發者。
2. 比對 inventory 與 action mapping 的 `model_id`／`model_name`。模型不同、重載後
   instance 改變或資源缺失時，拒絕沿用舊映射。
3. 先用 `smoke --only expression|hotkey|continuous|mouth` 逐項驗證，再執行完整
   `smoke`。每次都確認表情還原、連續參數釋放及 MouthOpen 歸零。
4. 基礎 smoke 通過後才執行 `talk-demo --duration 20`。NightRain 已驗證的眼睛中性值
   `0.0833` 與閉眼預加重 `-0.02` 只適用於該模型，不得套用到其他模型。
5. 若連線中斷或模型切換，先重新盤點並確認映射，再重試單一項目；不要用反覆重連掩蓋
   持續失敗。

## 回報與驗證

回報實際模型身分、各映射是否存在、`missing_resources`、逐項 smoke 結果與收尾狀態。
未實機執行就標示 `not run`，不得用範例映射宣稱校正成功。

修改 Python 後，執行專案 `AGENTS.md` 的完整基本驗證；只改文件或 skill 時至少執行
`git diff --check`，並驗證本 skill。

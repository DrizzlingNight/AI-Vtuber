# Phase 5 一頁式結論

更新日期：2026-09-15

## 結論：Phase 5 核心互動鏈路實機驗收已通過

正式一小時報告已同時滿足全部必要條件：60／60 輪完成、第二帳號自動驅動完整、
實際執行 3,616.69 秒、必要延遲與 RAM／VRAM 量測齊全、VTube Studio／MouthOpen
沒有降級，且沒有未處理錯誤。因此可以宣告 **Phase 5 核心互動鏈路完成**。

| 要確認的事 | 實際結果 |
|---|---|
| 程式有沒有修改？ | **有**；加入獨立第二帳號驅動、LLM 預熱與 Phase 5 可執行情緒限制 |
| 離線模擬測試 | **168 個通過**；並另有下列實機證據 |
| 真實 Twitch → LLM → VTS → TTS → Twitch | 單輪 **1／1 通過**；連續 **5／5 通過** |
| 一小時核心互動鏈路測試（不含 OBS） | **60／60 通過**；`phase5_hour_acceptance: passed` |
| 真實延遲 | 首 token p50／p95：1.66／1.83 秒；播放完成 p50／p95：16.75／20.30 秒 |
| 真實資源 | system RAM peak 33,425.55 MiB；llama-server RSS peak 8,597.16 MiB；GPU VRAM peak 7,646 MiB |
| VTS 與錯誤 | 6,140／6,140 次探測在線；60 輪錯誤數 0 |
| `main` 是否更新？ | **是**；第二帳號驅動、預熱、情緒限制、文件與長期 skill 已隨本次收尾提交同步 |

## 正式報告

```text
.local\benchmarks\phase5-single.json
.local\benchmarks\phase5-single.md
.local\benchmarks\phase5-continuous.json
.local\benchmarks\phase5-continuous.md
.local\benchmarks\phase5-one-hour.json
.local\benchmarks\phase5-one-hour.md
```

一小時報告的 `status` 與 `phase5_hour_acceptance` 都是 `passed`；
`measurements_complete` 與 `input_driver_complete` 都是 `true`。第二帳號實際送出
60／60 則固定自然聊天訊息，從第一則到最後一則分散 3,600 秒；佇列沒有 cooldown、
滿載、過期、驅逐或關閉丟棄。

## 為什麼先前使用隔離分支？現在同步了嗎？

這個工作階段建立時，Copilot App 就已配置隔離 worktree 與工作分支；
後續只是將分支更名為 `claireke0329-phase-5-orchestration`。
隔離的用途是讓開發變更不直接影響 `main`，**不會自動回寫主目錄**。

先前只做了工作分支提交，所以當時主目錄看不到修改。之後 PR #1 已合併到遠端
`main`，並在 2026-09-14 將 `F:\user\Documents\Workspace\AI Vtuber` 的本機
`main` 快轉同步至 `17a9e77`，交接提交再同步至 `02cb416`。本次實機驅動器、預熱、
情緒限制、文件與長期 skill 重整已隨 Phase 5 收尾提交同步；實際提交編號以 Git 紀錄為準。

## 目前還缺什麼？

**同步程式與完成驗收是兩件不同的事。** 這次兩者都已完成：正式報告證明 Phase 5
核心互動鏈路通過，本次必要程式與文件也已同步至 `main`。Phase 5 目前沒有待補驗收項目；
下一步若進入 Phase 6，需另依 `PROJECT_BRIEF.md` 建立該階段的施工與驗收文件。

## 這個一小時測試不代表什麼？

Phase 5 即使通過，也只證明 Twitch、LLM、VTS、TTS、實體播放與 Twitch 回覆的核心鏈路
能連續運作；它不會啟動 OBS，也不量測編碼、RTMP、掉幀、bitrate 或觀眾端影音。

真正的直播穩定性屬於 Phase 6：先以預定直播設定完成 OBS 本機錄影，再於使用者明確
授權後進行受控 Twitch 直播及 4～8 小時 soak test。Phase 7 才處理 obs-websocket 場景／
字幕自動化與其他品質升級。

接手施工請先看 [Codex 交接文件](phase-5-handoff-to-codex.md)，詳細修改、先前失敗與
最後通過證據見 [完整執行紀錄](phase-5-work-log.md)。歷史 `blocked` 與本次修正前的
`failed` 報告均保留，沒有改寫成成功。

```text
.local\benchmarks\phase5-smoke-20260907T075937540457Z.md
.local\benchmarks\phase5-smoke-20260907T075937913398Z.md
.local\benchmarks\phase5-single-cold-cache-failed-20260914T153435Z.md
.local\benchmarks\phase5-single-test-label-ignored-20260914T154029Z.md
.local\benchmarks\phase5-single-unmapped-emotion-failed-20260914T154343Z.md
```

# AI VTuber Local

目前只實作 `PROJECT_BRIEF.md` 的 Phase 0 與 Phase 1：Python 專案基礎、VTube Studio
授權與資源盤點，以及設定檔白名單控制的表情、熱鍵、連續參數與基本嘴型 smoke test。
尚未加入 Twitch、LLM、TTS、OBS 或大型模型。

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

## 測試

測試不需要 Twitch、VTube Studio、模型或網路：

```powershell
.\.venv\Scripts\python.exe -m pytest
```

# Phase 4 TTS、音訊與嘴型

更新日期：2026-09-05

## 範圍與結果

Phase 4 保持為獨立本機工具，沒有串接 Twitch、LLM 決策或 OBS。已完成：

- 本機 TTS 產生 16-bit PCM 與 WAV。
- 以 PortAudio 播放 WAV，單一工作者保證語音不重疊。
- 支援取消目前播放及清空待播佇列。
- 從同一份 PCM 樣本時間軸計算 30 Hz RMS 音量包絡。
- 將包絡映射到既有 NightRain `mouth_test`，不寫死 VTS 參數 ID。
- 正常結束、取消或錯誤時停止音訊、清空字幕並將嘴型注入中性值。
- 以 UTF-8 文字檔原子更新字幕。
- 在 Gemma 4 與 VTube Studio 同時運作時量測延遲、RAM 與 VRAM。

## TTS 與聲音權利決策

正式 Phase 4 smoke test 使用 eSpeak NG 1.52.0 的 `cmn` 國語音色。它是
rule-based formant synthesis，不使用真人錄音、聲音克隆或角色聲線。音質較機械，但可在
CPU 上快速產生中文語音，且不占用 GPU VRAM。

eSpeak NG 採 GPL-3.0，可商用。只在本機執行不產生額外義務；若重新散布 runtime，必須
保留 GPL、版權聲明，並依 GPL 提供對應原始碼。GPL 對程式輸出的適用仍取決於輸出本身
是否構成受 GPL 保護作品；本專案輸入文字所產生的語音不包含 eSpeak NG 程式碼。

本機另有 Microsoft Hanhan Desktop `zh-TW`，但公開 Windows/SAPI 文件沒有提供足夠明確、
可直接套用到營利直播輸出的授權條款，因此未採用。

### MeloTTS

程式內保留 `MeloTTSEngine` CPU adapter：

- 固定 `device="cpu"`。
- 固定 `use_hf=False`，不允許執行時隱式下載。
- 只接受明確提供的本機 `config.json` 與 `checkpoint.pth`。
- 使用 mock model 完成離線 adapter 測試。

官方 `myshell-ai/MeloTTS-Chinese` checkpoint 為 207,770,124 bytes，repository revision
為 `af5d207a364ea4208c6f589c89f57f88414bdd16`，checkpoint SHA-256 為
`a74e9eadffff065c75eb6dfa040efa72cad23e72cfea70d39190bc174fb97093`。Repository metadata
標示 MIT，但官方沒有揭露 ZH speaker 身分、訓練語料來源或聲音人格權同意，因此本階段
**沒有下載、安裝或播放該 checkpoint**。程式碼 MIT 授權不視為真人聲音權利證明。

## 已安裝 runtime

所有 runtime 都位於 `.local/runtime/`，由 `.gitignore` 排除，不修改系統 PATH。

| 元件 | 來源與版本 | 下載大小 | 本機落地 | 授權 |
|---|---|---:|---:|---|
| eSpeak NG | 官方 GitHub release 1.52.0 MSI | 12,765,862 bytes | 約 23.4 MiB（runtime，不含安裝包） | GPL-3.0 |
| FFmpeg | BtbN official-page-linked build `N-126404-g818e5d965b-20260904`, win64 LGPL | 148,557,518 bytes | 約 344.5 MiB | LGPL-3.0 |
| sounddevice | PyPI 0.5.6 win_amd64 wheel | 1,009,630 bytes | 約 2.1 MiB（含 PortAudio DLL） | MIT；PortAudio 為 MIT-style |
| MeloTTS ZH | 未下載 | 207,770,124 bytes（候選權重） | 0 | metadata 為 MIT；聲音權利未證實 |

固定完整性資料：

| Artifact | SHA-256 |
|---|---|
| eSpeak NG 1.52.0 MSI | `7f673c709ea5dd579d3b5ebb98688cc575328a6ab7438d2bc405b88cedaeafb9` |
| `espeak-ng.exe` | `3080ec3822c1b266ef557c710bc79a97d20a7ab133a34bac308b81ab0afc733e` |
| FFmpeg BtbN ZIP | `5082d14b330e5159209ffd4669a0731474137533b72739d3f724414d55d8084f` |
| `ffmpeg.exe` | `16290441aee9e523c69300e29d285ceb136a45a6270dd523cf5933ab909b5d82` |

FFmpeg 是獨立執行檔，不與 Python 應用程式連結；本專案只用它驗證生成 WAV。選用的 build
未啟用 GPL-only 或 nonfree 元件。若日後重新散布 FFmpeg，仍須附 LGPL/GPL 授權文件、
版權聲明，以及對應原始碼或有效取得方式。

本機保留的授權文件位於 `.local/runtime/licenses/`；FFmpeg distribution 內也保留
`LICENSE.txt`。

### 下載前資源估算

下列是選型時的估算，不是官方效能保證：

| 路徑 | CPU | RAM | VRAM |
|---|---|---:|---:|
| eSpeak NG `cmn` | 短生命週期單句合成，預期只占少量 CPU 時間 | 預估低於 100 MiB | 0 |
| sounddevice/PortAudio | 播放 thread 與小型 PCM buffer | 預估低於 10 MiB | 0 |
| FFmpeg WAV 驗證 | 每句一次短生命週期解碼 | 預估低於 100 MiB | 0 |
| MeloTTS CPU 候選 | PyTorch CPU 推論，會明顯高於 eSpeak | 約 1～3 GiB | 0 |

MeloTTS 本體還會帶入 PyTorch、語言前處理與字典依賴，下載／落地可能再增加數百 MiB
到數 GiB。因 ZH 音色權利未證實，本階段沒有用實際下載覆蓋這些估算。

## 音訊與嘴型時間軸

1. TTS 完整產生 mono 22.05 kHz、16-bit PCM/WAV。
2. 以 30 Hz、30 ms RMS window 將 PCM 轉成 0～1 音量包絡，並使用 attack/release 平滑。
3. PortAudio 以 20 ms block 播放；`position_frames` 是已送入播放裝置的 PCM frame 位置。
4. 嘴型工作在同一份 sample index 上查詢包絡，再映射到 `mouth_test` 的
   `neutral_value`～`peak_value`。
5. `finally` 依序停止未完成音訊、注入嘴型中性值、清空字幕。

這是音量驅動的 MouthOpen，不是 A/I/U/E/O viseme。播放寫入與嘴型更新的最大排程粒度約
為 20～33 ms，實際裝置與 VTS smoothing 仍可能加入少量延遲。

## 命令

檢查 runtime、預設播放裝置及聲音權利狀態：

```powershell
.\.venv\Scripts\python.exe -m ai_vtuber tts-status
```

只產生並以 FFmpeg 驗證 WAV，不連接 VTS：

```powershell
.\.venv\Scripts\python.exe -m ai_vtuber tts-synthesize "晚安，小雨。"
```

播放、輸出字幕並同步 NightRain 嘴型：

```powershell
.\.venv\Scripts\python.exe -m ai_vtuber tts-speak "晚安，小雨。"
```

不連接 VTS：

```powershell
.\.venv\Scripts\python.exe -m ai_vtuber tts-speak "晚安，小雨。" --no-vts
```

驗證中途取消與清理：

```powershell
.\.venv\Scripts\python.exe -m ai_vtuber tts-speak `
  "這段語音會在播放途中取消。" --cancel-after 0.6
```

正式共存 benchmark 要先啟動既有 Gemma 4 server，並保持 VTube Studio 開啟：

```powershell
.\.venv\Scripts\python.exe -m ai_vtuber llm-serve
.\.venv\Scripts\python.exe -m ai_vtuber tts-benchmark
```

## 本機 benchmark

報告：`.local/benchmarks/tts-benchmark-20260904T194901Z.json`

條件：

- Intel Core i7-12700K、64 GB RAM、NVIDIA RTX 2080 8 GB。
- eSpeak NG 1.52.0 `cmn`，CPU-only。
- Gemma 4 12B llama-server 依 Phase 3 設定運作。
- VTube Studio 與 NightRain 全程開啟。
- 10 句繁中，非串流、逐句產生。
- 資源每 20 ms 取樣；TTS process RSS 是 Python controller，短生命週期 eSpeak child
  另由 system RAM delta 涵蓋。

| 指標 | 結果 |
|---|---:|
| 首音延遲 min／p50／p95／max | 0.063／0.065／0.076／0.076 秒 |
| 總生成 min／p50／p95／max | 0.063／0.065／0.076／0.076 秒 |
| RTF min／p50／p95／max | 0.0067／0.0084／0.0402／0.0402 |
| TTS controller RSS 峰值 | 44.7 MiB |
| system RAM baseline／peak／delta | 26,795.6／26,821.6／26.0 MiB |
| 合併 VRAM baseline／peak／delta | 7,014／7,014／0 MiB |
| GPU utilization 峰值 | 38% |
| VTS | 全部取樣在線 |
| Gemma 4 | benchmark 前後皆在線 |

eSpeak NG 一次產生完整 WAV，因此「首音」是第一份可播放 PCM 出現的時間，與總生成時間
相同；它不是串流 TTS 的首 chunk 指標。

## 實機 smoke 結果

- WAV 可由 Python `wave` 與 FFmpeg 完整解碼。
- PortAudio 實際播放完成。
- NightRain 使用既有 `mouth_test -> MouthOpen` 映射同步開合。
- 11.39 秒語音在 0.6 秒取消後，音訊停止；重新盤點的 `MouthOpen` 為 `0.0`，
  字幕檔大小為 0 bytes。
- 多句不可重疊、取消目前播放及清空全部待播項目由離線測試覆蓋。

## 來源

- eSpeak NG release：<https://github.com/espeak-ng/espeak-ng/releases/tag/1.52.0>
- eSpeak NG languages：<https://github.com/espeak-ng/espeak-ng/blob/master/docs/languages.md>
- eSpeak NG GPL：<https://github.com/espeak-ng/espeak-ng/blob/1.52.0/COPYING>
- FFmpeg official downloads：<https://ffmpeg.org/download.html>
- BtbN FFmpeg builds：<https://github.com/BtbN/FFmpeg-Builds>
- sounddevice：<https://pypi.org/project/sounddevice/0.5.6/>
- PortAudio license：<https://github.com/PortAudio/portaudio/blob/master/LICENSE.txt>
- MeloTTS：<https://github.com/myshell-ai/MeloTTS>
- MeloTTS Chinese：<https://huggingface.co/myshell-ai/MeloTTS-Chinese>

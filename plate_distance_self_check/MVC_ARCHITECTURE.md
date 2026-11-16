# MVC 專案架構與操作指令

本專案採用 MVC 分層，所有功能統一從專案根目錄的 `main.py` 啟動。
根目錄不放重複的功能程式；`main.py` 只負責判斷指令並轉交給對應的 Controller。

## 目錄功能

### `app/controllers/`

Controller（控制器）負責接收指令參數、選擇攝影機及協調整個執行流程。

- `distance_self_check.py`：攝影機距離自我檢查、ROI 選取及雙鏡頭 handoff 流程。
- `evaluate_run.py`：建立 Ground Truth 並執行量化評估。
- `reanalyze_existing.py`：重新分析既有自我檢查輸出。
- `reanalyze_handoff.py`：重新產生既有 handoff 報告。

### `app/models/`

Model（模型）負責資料狀態、演算法與商業規則。

- `handoff.py`：跨鏡頭配對、MQTT 生命週期及 handoff 結果。
- `evaluation.py`：序列比對、移除事件比對及評估指標。

### `app/views/`

View（視圖）負責把結構化資料轉換成人類可閱讀的內容，例如 Markdown 報告。
Markdown、JSON 與 CSV 必須使用同一份標準結果資料，不可在報告階段修改判定結果。

### 其他目錄

- `plate_detector/`：影像推論、追蹤、顏色分類、錄影與 MQTT 基礎模組。
- `tools/`：不參與正式判斷的一次性、離線與維護工具。
  - `color_calibration.py`：顏色校正資料收集入口。
  - `record_video.py`：原始攝影機畫面錄影。
  - `batch_preprocess_images.py`：離線圖片 AOI 裁切及前處理。
- `detector_config/`：Detector、AOI 及攝影機相關設定。
- `tests/`：單元測試與可重播流程測試。
- `baselines/`：演算法變更前保存的基準結果。
- `outputs/`：每次執行產生的 JSON、CSV、圖片與分析資料。
- `reports/`：由標準結果產生並經過整理的實驗報告。
- `runtime_logs/`：執行期間產生的暫存紀錄。

## 基本指令

所有指令都必須在專案根目錄執行：

```powershell
cd D:\AI\plate_distance_self_check
```

API Key 與 MQTT 帳密不寫入 Git。啟動前可在 PowerShell 設定：

```powershell
$env:PLATE_INFERENCE_API_KEY = "你的 API Key"
$env:PLATE_MQTT_USERNAME = "你的 MQTT 帳號"
$env:PLATE_MQTT_PASSWORD = "你的 MQTT 密碼"
```

查看所有功能：

```powershell
python main.py --help
```

查看某個功能的詳細參數：

```powershell
python main.py self-check --help
python main.py record --help
python main.py batch-preprocess --help
python main.py evaluate --help
```

## 攝影機自我檢查

### 1. 查看可用攝影機

```powershell
python main.py --list-cameras
```

輸出會列出 OpenCV 使用的實體攝影機編號。後續使用 `--camera` 指定該編號。

### 2. 開啟指定攝影機

```powershell
python main.py --camera 1
```

也可以使用完整的 `self-check` 寫法，兩者功能相同：

```powershell
python main.py self-check --camera 1
```

### 3. 選取並保存 ROI

```powershell
python main.py --camera 1 --select-roi
```

### 4. 無畫面生產模式

```powershell
python main.py --camera 1 --headless --autosave-seconds 5
```

`--headless` 不開啟 OpenCV 視窗；`--autosave-seconds` 設定即時結果保存間隔。

### 攝影機相關參數

| 參數 | 功能 |
|---|---|
| `--camera N` | 指定 Windows/OpenCV 的實體攝影機編號。 |
| `--list-cameras` | 掃描並列出可用攝影機。 |
| `--camera-id ID` | 指定系統中的邏輯名稱，例如 `cam-2`。這不是實體攝影機編號。 |
| `--peer-camera-id ID` | 指定 handoff 對端的邏輯攝影機名稱。 |
| `--select-roi` | 重新選擇並保存四點 ROI。 |
| `--headless` | 不顯示視窗，以連續模式執行。 |
| `--autosave-seconds N` | 每隔 N 秒保存一次即時結果，預設為 5 秒。 |

## 固定 USB 孔位與 Camera ID

Windows 重新開機或重新插拔攝影機後，OpenCV camera index 可能改變。
本專案可以保存攝影機的 USB device path，並將它綁定到固定的邏輯 Camera ID 與 MQTT Camera ID。

### 1. 查看目前攝影機 index

```powershell
python main.py --list-cameras
```

### 2. 綁定 USB 攝影機

例如目前上游攝影機是 index `0`：

```powershell
python main.py camera-bind `
  --camera 0 `
  --camera-id cam-2 `
  --mqtt-camera-id cam-2 `
  --handoff-role upstream `
  --peer-camera-id cam-3
```

例如目前下游攝影機是 index `2`：

```powershell
python main.py camera-bind `
  --camera 2 `
  --camera-id cam-3 `
  --mqtt-camera-id cam-3 `
  --handoff-role downstream `
  --peer-camera-id cam-2
```

設定會保存在本機的 `detector_config/camera_bindings.json`。這個檔案包含機器專用的 USB path，
已加入 `.gitignore`，不會當成共用程式碼提交。

### 3. 查看保存的綁定與目前連線狀態

```powershell
python main.py camera-bindings
```

### 4. 之後只用固定 Camera ID 啟動

```powershell
python main.py --camera-id cam-2
```

程式會重新列舉攝影機，比對保存的 USB device path，找到該次開機真正的 OpenCV index，
並自動套用保存的 MQTT Camera ID、handoff role 與 peer camera ID。

如需臨時覆蓋保存設定，仍可在啟動時明確傳入參數：

```powershell
python main.py --camera-id cam-2 --mqtt-camera-id test-cam --handoff-role off
```

注意事項：

- 綁定使用完整 USB device path，不使用容易變動的 OpenCV index。
- 同型號攝影機也會依 device path 分開，不會只用相同的 VID/PID 猜測。
- 沒有 device path 的虛擬攝影機（例如部分 OBS Virtual Camera）不能建立 USB 綁定。
- 如果攝影機換到不同 USB 孔，請重新執行 `camera-bind` 更新綁定。

## 雙鏡頭 Handoff

先啟動上游鏡頭：

```powershell
python main.py --camera 2 `
  --handoff-role upstream `
  --camera-id cam-2 `
  --peer-camera-id cam-3 `
  --mqtt-host 127.0.0.1 `
  --mqtt-port 1883 `
  --continuous-handoff
```

再於另一個終端啟動下游鏡頭：

```powershell
python main.py --camera 1 `
  --handoff-role downstream `
  --camera-id cam-3 `
  --peer-camera-id cam-2 `
  --mqtt-host 127.0.0.1 `
  --mqtt-port 1883 `
  --continuous-handoff
```

Handoff 相關參數：

| 參數 | 功能 |
|---|---|
| `--handoff-role off` | 關閉雙鏡頭 handoff，這是預設值。 |
| `--handoff-role upstream` | 將目前攝影機設為上游。 |
| `--handoff-role downstream` | 將目前攝影機設為下游。 |
| `--mqtt-host HOST` | MQTT Broker 位址。 |
| `--mqtt-port PORT` | MQTT Broker 連接埠。 |
| `--continuous-handoff` | 啟用不需按 `B` 的連續 handoff 模式。 |
| `--matching-mode fifo` | 使用 FIFO 跨鏡頭配對模式。 |

## 即時 Detector

啟動共用的正式 detector：

```powershell
python main.py detect
```

Detector 使用 `detector_config/` 中的設定。
如已建立 USB 綁定，建議直接指定固定邏輯 ID，正式 Detector 也會解析當次 index，
並套用綁定的 MQTT Camera ID：

```powershell
python main.py detect --camera-id cam-2
```

## 顏色範圍偵測與校正

顏色功能沒有移除，目前分成兩部分：

- 即時顏色分類：位於 `plate_detector/detector.py`，由 Detector 與自我檢查流程共同使用。
- 顏色範圍校正入口：位於 `tools/color_calibration.py`，透過自我檢查流程收集 HLS、Lab 與分類結果。

啟動顏色校正：

```powershell
python main.py color-calibration --camera 1
```

開啟畫面後，請按照以下方式操作：

1. 等待 ROI 內沒有餐盤。
2. 按下 `B` 開始一輪顏色測試。
3. 依序放入橘色、綠色、黃色餐盤。
4. 可重複按 `B` 開始下一輪。
5. 按 `S` 保存報告，按 `Q` 結束時也會自動保存。

顏色範圍設定目前位於 `plate_detector/detector.py`：

- `HLS_HUE_RANGES`：橘、黃、淺綠的 HLS Hue 範圍。
- `LAB_COLOR_CENTERS`：各顏色的 Lab 中心值。
- `MIN_COLOR_SATURATION`：最低彩度。
- `MIN_COLOR_LIGHTNESS`、`MAX_COLOR_LIGHTNESS`：亮度有效範圍。
- `MIN_LAB_COLOR_SCORE`：Lab 顏色最低分數。

每次執行會在 `outputs/<執行時間>/` 保存顏色相關資料，包含：

- `color_calibration.csv`：每筆 observation 的 HLS／Lab 量測。
- `color_calibration_suggestions.md`：根據量測產生的色域建議。
- `distance_detections.csv`：包含預測顏色的偵測資料。
- `distance_summary.json`：本次測試的結構化摘要。

## 攝影機錄影

```powershell
python main.py record `
  --camera-id cam-2 `
  --output .\outputs\camera-1.mp4 `
  --width 1280 `
  --height 720 `
  --fps 30
```

錄影參數：

| 參數 | 功能 |
|---|---|
| `--camera N` | 以目前的實體攝影機 index 錄影。 |
| `--camera-id ID` | 透過保存的 USB 綁定尋找攝影機；與 `--camera` 擇一使用。 |
| `--output FILE` | 輸出 MP4 路徑，必填。 |
| `--width N` | 錄影寬度，預設 1280。 |
| `--height N` | 錄影高度，預設 720。 |
| `--fps N` | 每秒影格數，預設 30。 |

錄影會持續到按下 `Q`，完成後保存至 `--output` 指定的 MP4 檔案。

## 批次圖片前處理

```powershell
python main.py batch-preprocess `
  .\input_images `
  .\outputs\processed_images
```

指定其他 ROI 檔案：

```powershell
python main.py batch-preprocess `
  .\input_images `
  .\outputs\processed_images `
  --roi .\detector_config\cam-2-aoi.json
```

第一個位置參數是輸入目錄，第二個位置參數是輸出目錄。
程式會遞迴尋找輸入目錄中的圖片，套用 AOI、透視處理及 detector 使用的影像前處理，
並將結果保存為 `.processed.png`，不會覆寫原始圖片。

## 評估執行結果

```powershell
python main.py evaluate `
  .\outputs\RUN_DIRECTORY `
  --sequence GGOYGOOO
```

加入人工提供的移除事件：

```powershell
python main.py evaluate `
  .\outputs\RUN_DIRECTORY `
  --sequence GGOYGOOO `
  --removal 3:upstream:O `
  --removal 6:between:G
```

`--sequence` 使用實際物理順序，其中 `O`、`G`、`Y` 分別代表橘、綠、黃。
`--removal` 格式如下：

```text
序號:upstream|between|downstream[:O|G|Y]
```

評估完成後會在指定輸出目錄產生：

- `ground_truth.json`
- `evaluation.json`
- `evaluation_summary.md`

## 重新分析既有結果

重新產生自我檢查分析報告：

```powershell
python main.py reanalyze-existing .\outputs\RUN_DIRECTORY
```

重新產生 handoff 分析報告：

```powershell
python main.py reanalyze-handoff .\outputs\RUN_DIRECTORY
```

重新分析只讀取既有輸出資料，不會重新開啟攝影機。

## MVC 相依方向

程式相依方向應維持：

```text
main.py
  -> Controllers
      -> Models
      -> Views
      -> plate_detector
```

- `main.py` 只負責指令分派。
- Controller 可以使用 Model、View 與 `plate_detector`。
- View 只能將標準資料轉換為顯示內容。
- Model 不可依賴 Controller。
- 新功能應放在 `app/` 對應的 MVC 目錄，不要在根目錄新增其他 Python 入口。

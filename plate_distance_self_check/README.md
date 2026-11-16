# Plate Distance Self Check

獨立的輸送帶盤子距離穩定度自檢專案。它不修改正式的
`D:\AI\plate_detect_cam2`，只透過明確的 sibling path 重用正式專案中的相機、
模型推論、AOI、色彩分類與 ByteTrack 實作。

## 已保存的測試結果

- [2026-10-30 兩次 baseline session](reports/20261030_baseline_two_sessions.md)

## 用途

- 將現有四點 AOI 的近端視為 `0 cm`、遠端視為 `75 cm`。
- 固定配置為入口緩衝 `0～5 cm`、主要辨識區 `5～65 cm`（60 cm）、出口緩衝 `65～75 cm`。
- 依透視在畫面上繪製每 `10 cm` 的格線。
- 多盤同時運轉時，逐一顯示距離、tracker ID 與 confidence。
- 統計每個 10 cm 區間的 confidence P10/P50/P90、追蹤中斷與樣本數。
- 找出從哪個距離區間開始不穩定。

這是 confidence 與 tracking stability proxy。沒有獨立感測器或人工 ground
truth 時，不能把結果稱為正式 Precision、Recall 或 false-positive rate。

## 執行

先確保正式辨識專案原本可正常執行，再從本專案啟動：

```powershell
cd D:\AI\plate_distance_self_check
python .\main.py --list-cameras
python .\main.py --camera 1 --select-roi
```

## cam-3 → cam-2 雙鏡頭銜接驗證

先確認同一台電腦的 MQTT broker 已在 `127.0.0.1:1883` 執行，再開兩個 CMD。

上游 cam-3：

```powershell
cd D:\AI\plate_distance_self_check
python .\main.py --camera 3 --handoff-role upstream --camera-id cam-3 --peer-camera-id cam-2
```

下游 cam-2：

```powershell
cd D:\AI\plate_distance_self_check
python .\main.py --camera 2 --handoff-role downstream --camera-id cam-2 --peer-camera-id cam-3
```

使用 `--continuous-handoff` 時不需要按 `B`。上游會在盤子結果鎖定後透過
MQTT 發布 Plate ID、順序與顏色；下游不使用預設顏色，而是先依 FIFO 抵達
順序建立暫定配對，配對後才比較顏色。FIFO 配對不是實體盤身分驗證。
下游輸出目錄會多出第三組銜接結果：

```text
handoff_results.csv
handoff_summary.json
handoff_analysis_summary.md
mqtt_events.jsonl
```

銜接報告包含 FIFO 配對率、配對後顏色一致率、身分驗證狀態、漏盤、多餘盤，
以及 cam-3、cam-2 各自的斷線、重連、發送失敗、重複、亂序、事件閃爍與
heartbeat 中斷統計。沒有獨立真值時，身分準確率會是 `null`。

測試後用獨立真值產生統一評估：

```powershell
python .\main.py evaluate .\outputs\<cam-2輸出目錄> --sequence GGOYGOOO
```

若有拿走事件，可重複加入 `--removal`，格式為
`順序:upstream|between|downstream:顏色`。例如：

```powershell
python .\main.py evaluate .\outputs\<cam-2輸出目錄> --sequence GGOYGOOO `
  --removal 3:upstream:O --removal 6:between:G
```

這會分別產生 `ground_truth.json`、`evaluation.json` 與
`evaluation_summary.md`。重複顏色只能做確定性的顏色序列對齊，不能證明漏掉的
是哪一個同色實體盤。

### 全自動未知盤模式（不按 B）

這是獨立模式，不會取代上面的已知 O→G→Y／按 B 測試。兩個程式啟動後會立即
持續記錄，盤子數量、顏色與放入時間都不需要預先設定；報告每 5 秒自動儲存，
不需要按 `Q`。

上游 cam-3：

```powershell
cd D:\AI\plate_distance_self_check
python .\main.py --camera 3 --handoff-role upstream --camera-id cam-3 --peer-camera-id cam-2 --continuous-handoff
```

下游 cam-2：

```powershell
cd D:\AI\plate_distance_self_check
python .\main.py --camera 2 --handoff-role downstream --camera-id cam-2 --peer-camera-id cam-3 --continuous-handoff
```

上游每確認一盤會即時顯示順序、Plate ID、顏色與 MQTT 狀態，並寫入
`upstream_inventory.csv` 與 `upstream_inventory_summary.md`。下游獨立鎖定顏色後
發布 `downstream_result` 回覆，再產生銜接與差異報告。只在出口附近突然出現、
沒有入口證據或 observation 不足的碎片 ID 不會被發布成正式上游盤。

Windows 的 camera index 在插拔兩支外接鏡頭後可能改變。先使用
`--list-cameras` 找出可讀取的編號，再用 `--camera N` 指定。本工具的相機編號
不會修改正式辨識專案。若鏡頭可列出但沒有畫面，先關閉 Windows Camera、
Teams、OBS 或另一個正在執行的 detector，避免裝置被占用。

工具會顯示 DirectShow 裝置名稱與唯一 USB path，並優先以 MJPEG 1280x720
開啟 OpenCam；若該格式不可用，會自動退回裝置預設格式。這可避免同型號 UVC
相機在 OpenCV 中協商到可讀取但全黑的串流格式。

快捷鍵：

- `Q`：結束並儲存
- `R`：重新繪製四點 ROI，並清除舊距離統計
- `S`：立即儲存目前報表
- `B`：開始紅→綠→黃循環測試；請在下一個紅盤進入 ROI 前按下

## 紅綠黃每 10 公分顏色精準度

軌道清空或確認下一個進入 ROI 的盤子是紅色時按 `B`。程式會將之後的新軌跡
依序指定為紅、綠、黃，再循環紅、綠、黃。現場稱為黃盤的類別，在既有模型內部
名稱是 `orange`，報表會顯示為 `yellow_accuracy`。

未按 `B` 前，畫面中央會持續顯示 `TEST NOT RECORDING`，且不會寫入任何測試
observation。若 ROI 中已經偵測到盤子，程式會要求先等 ROI 清空，避免紅綠黃順序
從錯誤的盤子開始。

每個 10 cm 區間輸出 `color_correct`、`color_total`、`color_accuracy`，以及
`red_accuracy`、`green_accuracy`、`yellow_accuracy`。這裡的 accuracy 是：

```text
該距離顏色判斷正確的 observations / 該距離已偵測 observations
```

它不包含完全沒有被模型偵測到的盤子，因此不是 detection recall。

## Pass ID 與每區多數決

按 `B` 後，下一個新盤次建立為 `pass_id=1`，接著依序增加到預計的 20 盤。
ByteTrack 若中途更換 tracker ID，工具會使用最後距離、經過時間、10 cm/s 軌道速度
與行進方向，嘗試接回原本的 pass ID。

每個 pass 在每個 10 cm 區間只產生一筆多數決結果，輸出到
`pass_distance_results.csv`。內容包含 observation 數、預期顏色、多數決顏色、
正確與否、confidence 中位數，以及該盤次曾使用的 tracker IDs。

`distance_metrics.csv` 另外提供每區的 `detected_passes`、`correct_passes`、
`pass_color_accuracy` 與 `pass_success_vs_planned`。

自檢 ROI 獨立儲存在 `self_check_aoi.json`，不會讀寫
`plate_detect_cam2/config` 裡的正式 AOI。第一次執行若尚未有自檢 ROI，程式會自動
開啟繪製視窗；之後可使用 `--select-roi` 或在主畫面按 `R` 重畫。依序用滑鼠左鍵
點四個角，右鍵復原，四點完成後按 Enter 儲存。

結果輸出到：

```text
outputs/<timestamp>/
├─ distance_detections.csv
├─ distance_metrics.csv
└─ distance_summary.json
```

## 自檢設定

不使用 TOML。直接修改 `app/controllers/distance_self_check.py` 頂部的設定：

```python
REQUIRED_DETECTION_LENGTH_CM = 60.0
ENTRY_BUFFER_CM = 5.0
EXIT_BUFFER_CM = 10.0
TRACK_LENGTH_CM = 75.0
BIN_SIZE_CM = 10.0
DEFAULT_CAMERA_INDEX = 1
MIN_OBSERVATIONS_PER_BIN = 20
MIN_CONFIDENCE_P10 = 0.50
MAX_TRACK_GAP_RATE = 0.15
MIN_COLOR_ACCURACY = 0.95
PLANNED_PASSES = 20
TRACK_SPEED_CM_PER_SECOND = 10.0
```

若相機、AOI、模型或 API 設定需要修改，仍由正式的 `plate_detect_cam2` 管理；
本專案不複製這些設定，避免兩份辨識邏輯日後不同步。

# 圖片像素輸出工具

程式會從 `D:\訓練集\訓練\camera1_20260916_161_frames` 隨機選一張圖片，
你可以在圖片上自行畫四邊形 ROI。程式會將 ROI 儲存在本專案的 `roi.json`，
並只將 ROI 內的像素輸出成 `x, y, r, g, b` 格式 CSV；`x, y` 是原圖座標。

```powershell
cd D:\AI\jev_model
python -m pip install -r requirements.txt
python export_image_pixels.py --draw-roi
```

畫 ROI 時：

- 滑鼠左鍵依序點四個角。
- 滑鼠右鍵復原上一點。
- `R` 清除重畫。
- `Enter` 儲存（必須剛好四點）。
- `Esc` 取消。

ROI 畫過一次後，之後直接執行即可沿用：

```powershell
python export_image_pixels.py
```

預設會在終端預覽前 20 個像素，完整結果儲存在 `pixels.csv`。

指定圖片或輸出位置：

```powershell
python export_image_pixels.py --image "D:\path\image.jpg" --output output.csv
```

指定其他 ROI JSON，或為指定圖片重畫：

```powershell
python export_image_pixels.py --roi "D:\path\roi.json"
python export_image_pixels.py --image "D:\path\image.jpg" --draw-roi
```

將全部像素也印到終端：

```powershell
python export_image_pixels.py --preview -1
```

# helper（本 fork 自行加的附加服務）

整理衣物照片、讀出品牌和材質。它不屬於上游 Wardrowbe，所以獨立放在這裡，合併上游更新時不會衝突。

每隔一段時間，對每件還沒處理過、狀態為 ready 的衣物：

1. 請視覺模型找出衣物的位置、品牌、材質標籤文字。
2. 裁到衣物範圍、去背（rembg），置中放在白色正方形上。原圖保留為備份，所以「還原原圖」仍然有效。
3. 品牌欄位空白時才填入；材質標籤文字加到備註。
4. （選用）用 Google Cloud Vision 以圖搜圖，再請 Gemini 查出商品的品牌、名稱、材質、價格和網址。

處理紀錄存在 `/data/wardrobe/.helper_state.json` 和 `.helper_web_state.json`。

## 執行方式

在 backend 映像裡執行，跟後端共用設定（資料庫、儲存路徑、AI 端點）：

```yaml
helper:
  image: <backend 映像>
  command: python /helper/wardrobe_helper.py
  environment:
    PYTHONPATH: /app
    BG_REMOVAL_PROVIDER: rembg
    BG_REMOVAL_MODEL: u2net
  volumes:
    - <資料目錄>:/data/wardrobe
    - <rembg 模型目錄>:/opt/rembg/models
    - ./addons/helper:/helper:ro
```

## 環境變數

| 變數 | 預設 | 說明 |
|---|---|---|
| `HELPER_INTERVAL_SECONDS` | `300` | 每輪間隔秒數 |
| `HELPER_TIDY_IMAGES` | `true` | 是否裁切、去背 |
| `HELPER_READ_INFO` | `true` | 是否讀品牌和材質 |
| `HELPER_PADDING` | `0.08` | 裁切時保留的邊界比例 |
| `HELPER_MODEL` | 後端的 `AI_VISION_MODEL` | 用哪個視覺模型 |
| `HELPER_LIMIT` | `0` | 每輪最多處理幾件，`0` 為不限 |
| `HELPER_WEB_LOOKUP` | `true` | 是否以圖搜圖查商品資訊 |
| `HELPER_WEB_MODEL` | `gemini-3.5-flash` | 查商品資訊用的模型 |
| `HELPER_WEB_LIMIT` | `0` | 每輪最多查幾件，`0` 為不限 |
| `HELPER_WEB_DRY_RUN` | `false` | 只記錄結果，不寫回資料庫 |
| `HELPER_WEB_ONLY` | （空） | 只查這些衣物（逗號分隔的 ID 開頭），測試用 |
| `VISION_API_KEY` | 後端的 `AI_API_KEY` | Google Cloud Vision 的 API key |

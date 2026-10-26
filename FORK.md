# 這個 fork 跟上游的差別

上游：[Anyesh/wardrowbe](https://github.com/Anyesh/wardrowbe)。這裡只記錄本 fork 自己加的東西，其他都跟上游一樣。

## 修改清單

| 功能 | 位置 | 說明 |
|---|---|---|
| 穿著日期／時段與快捷需求 | 建議頁、`weather_service.py`、`outfit_planning.py` | 今天／明天及上午／下午／晚上／全天改用對應逐時預報；六種快捷需求可複選，保留自由備註與手動天氣覆蓋 |
| 喜好與實穿分開／近期學習 | `outfit-reaction-actions.tsx`、`outfits.py`、`recommendation_memory.py` | 接受＝今天實穿＋喜歡，另有喜歡／不喜歡；未選中性。推薦用最近 180 天最多 200 筆回饋、30 天半衰期，歷史不刪除 |
| 自動重繪商品照 | `product_photo_service.py`、`tagging.py`、`product_photo.txt`、單品詳情 | 首次辨識時判斷照片是否需要整理，以獨立圖片編輯服務重繪透明 PNG；保留原圖可還原，預設關閉，設定見 README |
| 活動／場合備註 | `frontend/app/dashboard/suggest/page.tsx`、`frontend/messages/*/suggest.json`、`backend/app/api/outfits.py`（`event_notes`）、`recommendation_service.py` | 推薦頁可以填今天的活動、地點或 dress code，AI 會優先參考。填了備註的推薦不進快取，備註也會存到套裝上，之後在歷史紀錄看得到 |
| 穿搭品質檢查 | `outfit_quality.py`、`recommendation_service.py` | 先驗證全身覆蓋、鞋子、指定單品、正式度與方案差異，再儲存；不合格最多請原 AI 修正一次，不任意補鞋讓說明與單品不符。詳見 [穿搭邏輯](docs/outfit-quality.md) |
| 推薦輪替與回饋 | `item_scorer.py`、`learning_service.py` | 未記錄實穿也依最近七天推薦次數輪替上衣、褲子；拒絕整套不封鎖單品、不推論討厭所有顏色，接受／拒絕立即更新學習資料 |
| 每日配件必選與可重複 | `recommendation_service.py`（`_repeat_ok`） | 有可用庫存時每套選眼鏡、帽、包各一件及一雙鞋；實穿避重複不限制鞋、帽、包、眼鏡、皮帶；推薦曝光扣分不限制配件。眼鏡依 accessories 子類型識別 |
| 候選品項上限 | `backend/app/services/item_scorer.py`（`TOP_N`） | 70 → 200，避免帽子、眼鏡、鞋子被擠出清單 |
| AI 回覆解析失敗時記錄全文 | `recommendation_service.py`（`_parse_ai_response`） | 方便除錯 |
| 提示詞 | `backend/app/prompts/recommendation.txt`、`item_pairing.txt` | 整套正式度協調、必配鞋、眼鏡依需求選用、皮帶規則、輪廓比例與繁體中文輸出 |
| 個人風格 | 環境變數 `STYLE_NOTES`（`backend/app/config.py`） | 自由文字的穿衣風格描述，會加進推薦提示詞。內容不放在公開 repo，由主機設定 |
| helper 附加服務 | `backend/addons/helper/` | 整理照片、讀品牌和材質、以圖搜圖查商品資訊，見該目錄的 README。放在 `backend/` 底下是為了一起打包進 backend 映像 |

這些修改的測試在 `backend/tests/test_local_customizations.py`。

## 跟上游同步

```bash
git fetch upstream
git checkout -b ai/sync-upstream origin/main
git merge upstream/main
# 有衝突時，以上游的新寫法為主，再把上表的修改加回去；最常衝突的是兩份提示詞
```

合併完照一般流程開 PR，CI 會跑完整測試。

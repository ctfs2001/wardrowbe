# 這個 fork 跟上游的差別

上游：[Anyesh/wardrowbe](https://github.com/Anyesh/wardrowbe)。這裡只記錄本 fork 自己加的東西，其他都跟上游一樣。

## 修改清單

| 功能 | 位置 | 說明 |
|---|---|---|
| 活動／場合備註 | `frontend/app/dashboard/suggest/page.tsx`、`frontend/messages/*/suggest.json`、`backend/app/api/outfits.py`（`event_notes`）、`recommendation_service.py` | 推薦頁可以填今天的活動、地點或 dress code，AI 會優先參考。填了備註的推薦不進快取，備註也會存到套裝上，之後在歷史紀錄看得到 |
| 每套都有鞋 | `recommendation_service.py`（`_ensure_footwear`） | AI 沒挑鞋時，自動補上評分最高的鞋 |
| 鞋子、配件可重複 | `recommendation_service.py`（`_repeat_ok`） | 「N 天內不重複」和「今天拒絕過的不再推」只套用在衣服上；鞋、帽、眼鏡、皮帶可以天天穿 |
| 候選品項上限 | `backend/app/services/item_scorer.py`（`TOP_N`） | 70 → 200，避免帽子、眼鏡、鞋子被擠出清單 |
| AI 回覆解析失敗時記錄全文 | `recommendation_service.py`（`_parse_ai_response`） | 方便除錯 |
| 提示詞 | `backend/app/prompts/recommendation.txt`、`item_pairing.txt` | 正式度一致、個人風格、必配鞋和眼鏡、皮帶規則、繁體中文輸出 |
| helper 附加服務 | `addons/helper/` | 整理照片、讀品牌和材質、以圖搜圖查商品資訊，見該目錄的 README |

這些修改的測試在 `backend/tests/test_local_customizations.py`。

## 跟上游同步

```bash
git fetch upstream
git checkout -b ai/sync-upstream origin/main
git merge upstream/main
# 有衝突時，以上游的新寫法為主，再把上表的修改加回去；最常衝突的是兩份提示詞
```

合併完照一般流程開 PR，CI 會跑完整測試。

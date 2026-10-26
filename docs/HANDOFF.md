# Wardrowbe 最新交接入口

更新日期：2026-10-26。此檔是固定入口；下次對話先讀本檔，再讀下列 ai-dev 私有交接。

## 先讀取什麼

1. 環境與專案的 `AGENTS.md`，確認本次對話的工具、權限與工作流程。
2. ai-dev 私有交接：`/config/codex/handoffs/wardrowbe/LATEST.md`。內含已部署版本、NAS 狀態、備份與驗證紀錄；不在公開 Git repo 中。
3. [推薦邏輯與限制](outfit-quality.md)、[本 fork 的修改](../FORK.md)、[貢獻流程](../CONTRIBUTING.md)。

如果私有檔不存在，仍可依本檔理解程式，但正式環境的即時狀態需要重新查證，不能把歷史部署資訊當作目前狀態。

## 2026-10-26 待審核：穿著日期／時段與快捷需求

- [PR #8](https://github.com/ctfs2001/wardrowbe/pull/8)，分支 `ai/forecast-outfit-planning`：建議頁可選今天／明天，以及上午（06–12）、下午（12–18）、晚上（18–24）、全天（06–24）。日期與小時依帳號時區計算。
- 預覽與生成共用逐時預報方法，取所選時段平均氣溫／體感、最低最高氣溫、最高降雨機率／風速；預報缺漏明確報錯，不用當下天氣替代。手動天氣覆蓋仍優先。
- 六個可複選需求：室內冷氣、戶外為主、長時間走路、耐雨好整理、方便活動、稍微正式。以穩定識別碼傳入後端，轉成 AI 提示並連同自由備註保存在搭配 notes；不把「耐雨」當成實際降雨預報。
- `/outfits/suggest` 與 `/outfits/suggest-options` 新增可選 `wear_day`、`activity_needs`，沿用 `time_of_day`；未傳日期的舊客戶端保留目前天氣行為。搭配存入穿著日期，日期／時段不同的快取不可混用。
- 沒有 schema migration、新 provider 或新機密。眼鏡／帽／包／鞋規則保留。尚未合併、未部署、未動正式衣櫃。
- 本地天氣測試 69 passed；前端 i18n、TypeScript、lint 通過。完整流程由 PR CI 的隔離 PostgreSQL／Redis 驗證；最後結果見 PR。

## 2026-10-26 已合併：每日配件

- [PR #7](https://github.com/ctfs2001/wardrowbe/pull/7)，分支 `ai/daily-accessories`：每套推薦在可用庫存存在時各選一件眼鏡、帽子、包包，並保留恰一雙鞋的規則；四類可跨套／每日重複。
- 眼鏡以 `accessories` 的眼鏡子類型識別，不把一般飾品當成眼鏡。未分類或誤標的眼鏡需先修正衣物標籤。
- 候選上限保留這些配件類別；生成與快取都檢查缺件，最多一次 AI 修正，不事後補件；快取品質版本升至 3。
- 本地品質與排序測試 116 項通過；完整資料庫流程由 PR CI 使用隔離 PostgreSQL／Redis 驗證。
- PR #7 已合併，merge commit `1e36edf7ea110e65e6a349ca03a4ea8e34c27ee0`；正式部署狀態尚未重新查證。

## 專案方向與已完成工作

本專案是 `ctfs2001/wardrowbe`，上游為 `Anyesh/wardrowbe`。目前工作方向是改善既有衣櫃網站的推薦，不是另做本機技能或要求安裝 ChatGPT 外掛。沿用既有衣櫃、網站與 AI provider；沒有新增 AI provider、API key 或資料庫 migration。

2026-10-26 完成並部署的推薦優化：

- 程式 [PR #5](https://github.com/ctfs2001/wardrowbe/pull/5) 已合併。
- 部署 [nas-infra PR #11](https://github.com/ctfs2001/nas-infra/pull/11) 已合併。
- 已驗證及部署的程式 commit：`2d213324b22ea08caf1130ec1ad5a8a49c223e77`。
- 程式 PR 的 merge commit：`ccb634919aed8e342bd29268b132fbb26f1aeb59`；部署 infra merge commit：`b5747ec048c62e6f2d842f3231fea2fedb29882f`。程式 repo 的 main 與實際部署 SOURCE_REF 可以不同，須分別核對。
- 推薦會參考近七天推薦曝光，不再依賴使用者必須先記錄實穿才有輪替效果。
- 儲存前檢查完整度與方案差異；必要時最多請既有 provider 修正一次。修正仍失敗時可回傳較少的合格方案，沒有合格方案則回傳錯誤。
- 拒絕整套不封鎖其中所有單品，也不再推論討厭其中所有顏色或風格；接受／拒絕會即時更新學習資料。
- 商品照重繪是先前 PR #4 的可選功能，本次正式部署明確維持 `AI_PRODUCT_PHOTO_ENABLED=false`。

## 推薦流程與核心程式

1. `backend/app/api/outfits.py`：`/outfits/suggest-options` 取三套；`/outfits/suggest` 取一套，兩者都傳遞活動備註。接受／拒絕會呼叫安全學習更新。
2. `backend/app/services/recommendation_service.py`：讀取可用衣物，排除封存、待洗、明確排除等項目；處理指定單品、天氣、場合、風格、學習偏好與快取。
3. `backend/app/services/item_scorer.py`：所有衣櫃規模都評分，候選上限 200。天氣、正式度、季節、實穿時間、偏好、使用量與推薦曝光相乘，再套用既有配對加分。
4. `backend/app/prompts/recommendation.txt`：讓現有模型選出衣物編號，撰寫台灣繁體中文說明；提示詞允許有比例規劃的寬上衣加寬褲，眼鏡、帽子與包包有可用庫存時每套各選一件。
5. `backend/app/services/outfit_quality.py`：驗證模型選項，再由 recommendation service 寫入與快取；避免任意換鞋後仍沿用舊說明。
6. `backend/app/services/learning_service.py`：整套回饋與配對統計；顏色／風格的正向特徵每套只算一次，證據少時向中性縮減。

### 防重複的實際範圍

- 近七天曝光是核心單品的有下限軟扣分，鞋、配件、指定單品不受該曝光扣分；不能用新鮮感取代天氣與場合適合度。
- 近十二小時、最近至多十套推薦的核心組合會避重複，換鞋、帽或包不算新核心組合。
- 同一批選項若仍有合適且未用的同角色單品，會要求改用不同上衣、褲子等；指定單品允許重複。
- 「少穿」「少推薦」「喜歡」是不同訊號，不能混用；接受搭配也不等於記錄已實穿。

### 品質檢查與快取

- 驗證庫存編號、指定單品、上身／下身覆蓋、恰一件鞋類、身體槽位衝突、持續炎熱天氣的非指定腿部衣物，以及已知正式度相差至少三級的極端衝突。
- 生成與快取回傳都做品質檢查；快取 context 的 `quality_version=3`。未送出的快取選項不算已推薦曝光，實際寫入的 Outfit 才會計入。
- `_materialize_outfit` 仍保留原有清理與補鞋 helper，不能因此以為其他呼叫路徑都已獲得相同的生成前品質檢查；新增路徑時需確認呼叫順序。

## 驗證結果與接續測試

已部署來源的 GitHub CI：**2,116 項後端測試全部通過**；前端測試、前端建置、前後端 lint、Actionlint、Docker Build 皆通過。部署 PR 的 validate 也通過。

另外在獨立 NAS 測試容器中執行過完整後端測試：2,075 passed、27 skipped；不同環境的相依套件會影響收集／略過數量，不要把兩組數字相加。既有相依套件 deprecation 與測試 JWT 警告仍存在。

相關回歸測試：

- `backend/tests/test_outfit_quality.py`
- `backend/tests/test_recommendation_quality_flow.py`
- `backend/tests/test_item_scorer.py`
- `backend/tests/test_recommendation_service.py`
- `backend/tests/test_local_customizations.py`
- `backend/tests/test_learning_service.py`
- `backend/tests/test_timezone.py`：CI 曾發現 packaged tzdata 對 `America` 這種區域目錄名稱拋 `IsADirectoryError`，已加入處理與回歸測試。

修改推薦或學習邏輯後，使用隔離 PostgreSQL／Redis 執行相關測試；`TEST_DATABASE_URL` 和測試用 `DATABASE_URL` 必須指向測試資料庫。不可指向正式衣櫃。純文件更新不需要再產生付費 AI 推薦。

## 已知限制與後續方向

- 推薦使用儲存的衣物標籤，沒有在每次建議時重新觀看所有照片；錯誤顏色、材質、版型描述仍會影響結果。
- 完整度、差異與極端正式度檢查，不等於所有主觀搭配品味都已驗證。
- 可用核心組合太少、指定過多單品或近期組合已用盡時，可能不足三套或回傳錯誤。
- 修正呼叫可能增加既有 AI 的 token 費用與延遲；上限一次，不要加成無限重試。
- 後續優先依使用者對「哪兩件不搭、場合不符、比例或材質問題」的具體回饋定位原因，再決定改標籤、提示詞、驗證或排序；不要只為通過測試而放寬品質條件。

## ai-dev 接手方式

工作目錄是 `/workspace/wardrowbe`，部署設定 repo 是 `/workspace/nas-infra`。先做唯讀檢查：

```sh
cd /workspace/wardrowbe
cat AGENTS.md
cat docs/HANDOFF.md
cat /config/codex/handoffs/wardrowbe/LATEST.md
git status --short --branch
git fetch origin main
git log -5 --oneline origin/main
```

不要預設目前 checkout 就是最新 main。確認並保留既有工作後，新的程式任務從最新 main 開 `ai/<description>` 分支，依環境規則提交 PR。本次交接文件本身可能在尚未合併的文件 PR 分支上；私有交接記錄了該分支。

GitHub CLI 在此 checkout 曾預設選到上游 `Anyesh/wardrowbe`，即使推送用的 origin 是自己的 fork。所有 PR／issue 操作都明確指定 `--repo ctfs2001/wardrowbe`；部署設定操作則指定 `--repo ctfs2001/nas-infra`，避免誤開到上游。

ai-dev 的主機診斷權限有限，不能因本次曾由外部 Codex 成功部署，就假定此容器也能直接部署或取得主機 shell。既有的合併／部署授權只記錄為已完成事件，不代表未來任務都已獲授權。

## 以後如何更新這份交接

每次任務結束，更新本檔的程式行為、版本與測試證據；NAS 私有環境狀態更新在 `/config/codex/handoffs/wardrowbe/LATEST.md`。分清楚已完成、尚待驗證、尚未部署；私人衣物、帳號、內網與執行紀錄不要搬進公開 repo。

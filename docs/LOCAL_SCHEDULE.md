# 本機排程與來源對帳

## 現行時間（2026-10-06 調整）

目前已切換為 **V8.1 Python 獨立排程**：台灣 10:00 採集、10:10 用一般 Python 套件產生及讀回驗證 Excel，不依賴 Codex。登入時由單一 `com.woobe.gummy.scheduler` 啟動常駐程序，每日時鐘與工作紀錄由 Python 管理。舊採集／報表日曆工作已卸載並持續停用，Codex 跟進已刪除。計畫仍限 2026-10-04 至 10-08；今天 run10 只重用核對，未新增請求。啟動、時間調整及換電腦流程見 [Python 排程說明](PYTHON_SCHEDULER.md)。以下保留舊版安裝與驗證歷史，不是目前啟用配置。

只修改日曆時間；批次鍵、計畫 SHA-256、每批最多兩件、額度上限、報表承接來源及等待護欄保留。備份位於 `data/schedule-change-20261006-1000/`；專案報表設定副本已同步實際正常運作的來源路徑。歷史段落中的 09:10／09:20／09:30 是當時設定。此排程由本機 macOS 執行，受登入、睡眠與關機影響；V9 已新增網頁編輯時間、未來安排及暫停／啟用，詳見 [V9／V10 操作](OPERATIONS_V9_V10.md)；沒有依額度自動擴量。

兩件商品是免費試點的 `max_products=2` 限制。run10 的帳本實錄為 `/search` 成功 1 次、`/product-details` 成功 2 次，合計 **3 次**；商品數不能直接當請求數。只查搜尋可一次取得多筆摘要，但現行流程會逐件補明細，以保留規格與来源證據。

2026-10-03 09:00 的**單日試跑**已由 LaunchAgent 自動執行成功，產生 run_id=5，擷取 2 筆真實商品；`launchctl` 顯示執行一次且 exit code 0，stdout 也記錄 `SUCCEEDED`。本機帳本當日用了 3 次請求，累計 7/30。試跑完成後已卸載 LaunchAgent，目前不會繼續每日執行；安裝檔保留供檢查。程式以台灣日期產生 `daily-<source>-YYYYMMDD`，同日重跑會重用已成功的批次，不重發供應商請求。`RunAtLoad=false`，沒有自動重試。只有本機 SQLite 與已確認的免費 RapidAPI 方案受此版本支援。

## 先做不耗額度的測試

在專案目錄執行：

```bash
.venv/bin/gummy scheduled-collect --source demo --dry-run
.venv/bin/gummy schedule-plist --source demo --output data/gummy-demo.plist
plutil -lint data/gummy-demo.plist
```

第一行不入庫、也不呼叫 RapidAPI；第二行只產生設定檔，不會安裝或啟用。實際執行 demo 可用獨立的測試 DB，避免與既有商品報表混在一起。

## 有限期真實試點的設定檔

來源品質經人工核對、且 RapidAPI 控制台與本機帳本均有足額後，才準備有限期設定。例如 2026-10-03 至 2026-10-09：

```bash
.venv/bin/gummy schedule-plist --source rapidapi \
  --start-date 2026-10-03 --end-date 2026-10-09 \
  --output data/com.woobe.gummy.daily.plist
plutil -lint data/com.woobe.gummy.daily.plist
```

設定檔預設搜尋 `ratchet wrench`，分類標為原參考表的 `棘輪扳手(Ratchet Wrench)`，每批最多 2 件；09:00 是本機系統時區，這台 Mac 目前是 UTC+08:00。工作目錄固定為產生檔案時的專案目錄，程式從本機 `.env` 讀取金鑰；設定檔和 log 不寫入金鑰。真實排程必須指定結束日期，到期只回報 `OUTSIDE_TRIAL_WINDOW`，不再發請求。更改專案路徑、Python 環境或系統時區時須重新產生與測試。

真實排程限制為系統本地時間 09:00 以後；即使 macOS 在隔日清晨補跑前一天因睡眠錯過的事件，`scheduled-collect` 也會回報 `BEFORE_DAILY_WINDOW`，不建立失敗批次或耗用尚未重置的 UTC 額度。

本次安裝檔為 `data/com.woobe.gummy.pilot-20261003.plist`，僅允許 `2026-10-03` 當天採集 `ratchet wrench` 最多兩筆。完成驗證後已用 `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.woobe.gummy.amazon.daily.plist` 卸載，安裝檔仍保留。若重新載入，當天以外程式會回報 `OUTSIDE_TRIAL_WINDOW`，不發外部請求。macOS 的日曆排程在睡眠後可能於喚醒時補跑，關機錯過的執行則不補跑；因此本機排程不能保證每日一定有資料。[Apple 的排程說明](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/ScheduledJobs.html)

09:00 後請先看 `data/schedule.stdout.log`、`data/schedule.stderr.log`、`gummy quota` 與最新 `collection_runs`；以成功批次的實際 `run_id` 執行 `gummy verify-run --source rapidapi --run-id <RUN_ID>`，再匯出來源對帳。`PASS` 只代表已留存的選定欄位一致，仍需人工抽查商品頁或供應商回應。切勿只憑排程已載入就判定資料採集成功。額度若不符預期或來源錯誤，先停排程再診斷，不自動補打 API。

## 五天多分類試點（已載入本機排程）

2026-10-04 的正式日曆觸發已完成：09:10 取得羊角鎚 2 件，run_id=7、來源對帳 PASS 2/2；09:20 自動產生並讀回驗證 `exports/daily/handtools-20261004-08-20261004-run7.xlsx`。截至當日 13:07 台灣時間，本機帳本總計 12/30、UTC 當日 3/5，尚有 10-05 至 10-08 四天待逐日觀察。供應商額度標頭上次估計剩 89 次，不能反映其他應用之後的用量；真實人工商品核對進度仍為 0/7。

依已測試的六角扳手與棘輪扳手，下一步以手工具延伸為預設路線。計畫檔位於 plans/hand-tools-20261004-08.json，列出 2026-10-04 至 10-08 的羊角鎚、1/2 扭力扳手、活動扳手、數位扭力扳手、鐵皮剪；每天台灣時間 09:10 最多取 2 件。理想情況每類 1 次搜尋加 2 次明細，五天最多 15 次，加上現有本機帳本 9 次，最多為 24/30；失敗或回應缺資料可能使取得商品數更少。

在專案目錄可先唯讀檢查計畫與今天是否會執行：

    .venv/bin/gummy plan-validate --plan plans/hand-tools-20261004-08.json
    .venv/bin/gummy run-plan --plan plans/hand-tools-20261004-08.json --dry-run

2026-10-03 檢查結果是 READY、待執行 5 天、預計最多 15 次；今天不在計畫內，所以第二行回報 NO_PLAN_FOR_DATE，不建立批次或消耗額度。已將 data/com.woobe.gummy.handtools-20261004-08.plist 安裝至目前使用者的 LaunchAgents 並載入；安裝檔權限為 600，RunAtLoad=false，計畫 SHA-256 固定在啟動參數中。當晚手動 `launchctl kickstart` 空跑一次，LaunchAgent 的 runs=1、exit code=0，stdout 為 `NO_PLAN_FOR_DATE`，stderr 空白，額度仍 9/30，狀態仍 READY／五天待執行。這只驗證 LaunchAgent 能啟動現有程式與日期護欄，**不是** 10-04 09:10 日曆觸發成功。更改計畫檔後，舊排程會拒絕執行，須重新驗證並產生設定檔。原 2026-10-03 單日試跑排程仍已卸載。

執行器遇到當天額度不足、前一日漏跑、計畫批次失敗、來源留存資料核對不通過或批次鍵參數不符時，會停止後續取樣；同日成功重跑只回報 ALREADY_COLLECTED，不重新呼叫供應商。到 10-08 後沒有新日期，即使系統仍觸發工作，也只回報 NO_PLAN_FOR_DATE。本機帳本僅管理這份資料庫，不能代表帳號其他應用的請求；已取得的 RapidAPI 額度標頭也只是上次回應的估計，帳號端即時餘額仍需在後台核對。先前 Basic 方案畫面顯示 100 次／月硬上限，但方案條款可能變動。人工分類也仍待逐筆核對。

另有獨立的 09:20 報表 LaunchAgent，設定為 `data/com.woobe.gummy.report.handtools-20261004-08.plist`，已安裝並載入。它只在當日計畫批次成功且留存來源欄位對帳 PASS 後，從資料庫輸出 `exports/daily/handtools-20261004-08-YYYYMMDD-runN.xlsx`；不呼叫 RapidAPI，也不會因報表失敗重抓商品。第一天從已重新讀回驗證的 `exports/amazon-portfolio-run6-verified-20261004.xlsx` 承接人工 K–M 欄，往後從前一日報表承接；前一日計畫批次的參數或來源對帳若有異常，或其報表缺失，都會停止，避免靜默承接不可信資料或遺失人工結論。同名檔案拒絕覆寫。2026-10-03 手動空跑已回報 `NO_PLAN_FOR_DATE`、exit code 0、stderr 空白，這仍不代表 10-04 09:20 的日曆觸發已成功。報表依賴此 Mac 的 Codex 附帶試算表工具，搬到其他電腦或雲端時需另行部署該執行環境。

從第二天起，報表工作在承接 K–M 人工欄位前，會先將前一日整份 XLSX 與截至前一批次的已存快照逐頁重驗。人工欄位純文字變動可通過；若來源標題、分類分頁、商品列等非人工資料被修改或缺頁，當日報表停止且不產生新 XLSX，也不會重抓 RapidAPI。隔離兩日整合測試已驗證「前日人工欄已填」可承接，以及「前日來源標題遭改」會拒絕；10-05 的正式日曆觸發仍待實際觀察。

同一重驗已前移到 `run-plan` 的 RapidAPI 採集前，避免前日報表缺失或內容遭改後仍先耗用當日額度。10-05 起，09:10 工作在計畫狀態檢查通過後核對前日累計 XLSX；只有報表也通過，才繼續採集前配額預檢並發出請求。預設報表目錄為 `exports/daily`；若手動把報表輸出到別處，執行 `run-plan` 時須用 `--report-output-dir` 指向同一目錄。已安裝的採集與報表 LaunchAgent 經唯讀檢查均指向本專案 `exports/daily`，因此不需修改已載入設定。隔離真實來源形狀的測試驗證了「前日報表缺失／來源標題被改」都在採集前停止，完好報表則允許 dry-run；這不代表 10-05 實際請求已完成。

10-04 凌晨將報表 LaunchAgent 更新為 `--wait-seconds 300`，其餘計畫 SHA-256、09:20 時間、輸出目錄與人工覆核來源保持不變；新設定已通過 `plutil`、權限為 600、重新載入後 `/v1/plan` 回報 `LOADED_AND_MATCHED`。若 Mac 喚醒後採集與報表幾乎同時觸發，報表最多等待五分鐘讓當日批次完成；等待只查本機資料庫，不呼叫 RapidAPI。批次失敗會立刻停止報表，逾時也不會重抓；CLI 手動 `plan-report` 預設不等待，`--dry-run` 即使指定等待也只檢查當前狀態。這是喚醒競速的護欄，仍須以實際 09:10／09:20 執行結果驗證。

採集器會讀取 RapidAPI 回應的 `x-rate-limit-rapid-free-plans-hard-limit-remaining` 與 `x-ratelimit-requests-remaining`；兩者同時存在時採較小值。已知剩餘數不足以完成新批次或剩餘明細並保留 1 次餘裕時，會在下一次網路請求前停止；無效額度欄位也會停止。同批次後續回應若漏掉標頭，已知值仍會保守扣減。新版將這把 API 金鑰的估計值與最後收到標頭的 UTC 時間存進資料庫；重啟與隔日排程也會沿用，後續標頭即使顯示更高數字，也不會自動提高估計。資料庫只存金鑰雜湊，不存金鑰或完整 HTTP 標頭。這兩個欄位代表方案／免費方案額度，和短時間限流用的 `x-ratelimit-remaining` 不同，定義見 [RapidAPI 回應標頭文件](https://docs.rapidapi.com/docs/response-headers)。若供應商從未提供額度欄位，採集器仍受本機 30 次／每日 5 次硬限制，但不能宣稱已核對帳號額度。上次觀測值也無法看見其他應用後來的用量或方案重置，排程前後仍應到後台核對；若舊估計因方案重置而過低，先人工核實後再處理，不自動放寬上限。

本機每日 5 次帳本以 UTC 換日，台灣日期早於 UTC 日期換日的 00:00–07:59 時段，不把前一個 UTC 日已用額度誤算到當日 09:10 的計畫。10-04 00:00 實測 `run-plan --dry-run` 回報 `BEFORE_DAILY_WINDOW`、`plan-report --dry-run` 回報 `WAITING_FOR_BATCH`，額度仍 9/30、UTC 前一日 5/5；計畫狀態則維持 READY。這不會放寬 09:10 執行時資料庫的實際額度預檢。

10-04 09:20 之後，先查看 `data/plan-handtools-20261004-08.stdout.log`／`.stderr.log` 與 `data/report-handtools-20261004-08.stdout.log`／`.stderr.log`，再執行 `gummy plan-validate --plan plans/hand-tools-20261004-08.json`、`gummy quota` 及實際批次的 `verify-run`，確認當日 XLSX 已出現並通過讀回驗證。若要提前停用，須分別對 `~/Library/LaunchAgents/com.woobe.gummy.plan.handtools-20261004-08.plist` 與 `~/Library/LaunchAgents/com.woobe.gummy.report.handtools-20261004-08.plist` 執行 `launchctl bootout gui/$(id -u) <檔案>`；這只卸載工作，不刪除已採集資料。電腦關機或未登入時不能保證依時執行，漏跑會讓後續取樣停住，不會自動補打額度。

本機 `.env` 已設定 `GUMMY_ACTIVE_PLAN`，查驗頁的「採集排程」區與唯讀 `/v1/plan` 會分別顯示採集和報表 LaunchAgent 是否載入且設定匹配，並顯示最新計畫報表是否存在。新報表在匯出讀回驗證 PASS 後另存 `.xlsx.verified.json`，記錄來源、截至批次及報表 SHA-256；查驗頁只在紀錄與檔案吻合時標示程式讀回驗證通過。檔案存在但缺紀錄或已變更時仍顯示「內容仍須核對」。這不是數位簽章，也不能代替來源資料或人工分類驗收；舊報表不會自動補認證。

另有同一聊天中的 Codex 跟進排程，10-04 至 10-08 每天 09:30 檢查 09:10 採集與 09:20 報表結果、來源對帳、報表分頁和人工待驗欄；報表缺失時先查原因，只能從已存資料安全重產，不主動重打 RapidAPI。這個跟進與兩個 LaunchAgent 彼此獨立；Mac 與 Codex app 未運行時，跟進也可能延後或缺席，不能據此認定採集或報表已成功。

本機資料庫採 WAL 模式，不能只複製 `data/gummy.db` 當備份，否則可能漏掉仍在 WAL 的正式批次。需要保留已存快照與額度帳本時，在專案目錄執行 `.venv/bin/python -m scripts.backup_sqlite`；指令使用 SQLite 的線上備份功能，驗證完整性與外鍵，產生權限 600 的新檔於權限 700 的 `data/backups/`，同名檔拒絕覆寫，且不發 RapidAPI 請求。這份資料庫備份不含 XLSX 人工 K–M 欄、`.env` 或計畫檔；人工編修後仍須另外保存當日 XLSX 原檔。備份目前是手動指令，不應把它誤認為已排程的每日異地備份。

## 人工對帳

下一批完成後，以實際 `run_id` 匯出：

```bash
.venv/bin/gummy export-audit --source rapidapi --run-id <RUN_ID> \
  --output exports/amazon-<RUN_ID>-audit.csv
```

稽核表將供應商商品明細中**參與轉換的選定欄位**與本機快照並列：ASIN、標題、原始價格與解析後 USD、品牌、材質、公制／英制、尺寸、顏色、評分及評價數。自後續新批次起，來源證據另外保留 `product_information`／`product_details` 各最多 64 個短文字屬性（欄名與值各最多 120 字元），優先保留參與映射的欄位；若搜尋結果或商品明細有最長 120 字元的 `sales_volume` 文字，亦分別留存為非精確購買提示。其他屬性只供離線評估，查驗頁標成未分類，不進入規格組合或銷量統計。不保存完整供應商 JSON、HTTP headers、API key、cookie 或評論全文；新批次在入庫同一交易中保存證據。已完成的舊批次不會補出未保存欄位；例如 run_id=3 沒有來源證據，`evidence_status=not_recorded_for_this_run`，不能以當日重新查詢冒充當時原始回應。

人工逐筆核對時，先核對來源值與快照的大小寫／價格正規化，再用 ASIN 比對商品表與規格組合表，重算群組商品數與平均價格。標題 Metric／SAE 僅是未驗證候選，不得填進來源結構化欄位；群組相加也不得當成不重複商品數。此證據只覆蓋商品明細的選定欄位，尚不覆蓋搜尋結果完整排序、原始回應全文或原表的銷量／排名。真實來源留存條款需在啟用前確認。

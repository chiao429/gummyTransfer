# 本機與雲端 worker 的選擇

## 結論

先本機驗證，再分階段容器化採集核心與唯讀 API。這裡 worker 指「執行一批工作後退出的 Python 程式」，不限定 Cloudflare Workers。gummy 提供 HTTP 查詢，與批次 worker 分開啟動。現行五日計畫、macOS 排程及 XLSX 產生器仍是本機試點，不能把容器可執行視為整條每日報表鏈已可上雲。

| 選項 | 目前適合度 | 取捨 |
|---|---|---|
| 本機 Python + SQLite | 第一階段採用 | 零雲端資源費、啟動快；電腦關機就不執行 |
| 本機 Docker + PostgreSQL | 第二階段選用 | 接近正式資料庫；需啟動 Docker、管理 volume |
| GCP Cloud Run Job + Service + Cloud SQL | 後續建議 | CPython 容器可沿用；需 IAM、資料庫與實際計費設定 |
| Cloudflare Python Workers | 本版不採用 | Python runtime／套件支援模型不同，要另驗證 SQL driver 與工作執行方式；不能把這個 Dockerfile 直接部署過去 |

目前不需要 queue、Kubernetes、Redis、瀏覽器叢集或付費 proxy。

## 本機 PostgreSQL 的選配驗證

`compose.postgres.yml` 只啟動本機 PostgreSQL，埠限制為 127.0.0.1，資料存於 named volume。先在本機環境或 `.env` 設定隨機的 `POSTGRES_PASSWORD`；勿把密碼寫進 compose。資料庫預設 user/database 均為 gummy。

```bash
docker compose -f compose.postgres.yml up -d
.venv/bin/python -m pip install -c requirements.lock.txt -e '.[dev,postgres]'
```

將 `.env` 的 DATABASE_URL 改成 `postgresql+psycopg://gummy:<URL編碼後的密碼>@127.0.0.1:5432/gummy`，然後用 demo source 測試。SQLite 資料不會自動移轉；正式遷移時，尤其要連同 `quota_budgets`、`api_attempts` 與新增的 `provider_quota_state` 一起轉移，或用更保守的剩餘上限，不能換 DB 就當額度重置。雲端應以獨立 migration 身分建立此表，不能假設執行中服務會自動升級 schema。

2026-10-03 已用一次性 `postgres:16-alpine` 容器、獨立 `gummy_test` 資料庫及 `127.0.0.1:15432` 完成本機實測；容器和臨時密碼已清除。`tests/test_postgres_integration.py` 只有在 `GUMMY_TEST_POSTGRES_URL` 指向本機 `gummy_test` 時才執行，避免寫入正式資料庫；請透過忽略版本控制的安全環境提供 URL，不要把密碼寫進指令歷史。測試涵蓋 demo 入庫／冪等、來源證據、cloud 模式讀取 API 的授權與文件關閉，以及 8 個並行預留中只有 3 個取得配額。此測試沒有呼叫 RapidAPI，也不等於 Cloud SQL 已驗證。

## 應用容器

`Dockerfile` 使用 Python 3.12、非 root 使用者、已固定版本的依賴限制。預設命令啟動 API，PORT 預設 8080；批次執行時覆寫 entrypoint 之後的參數為 `collect --source demo --run-key container-demo`。

```bash
docker build -t gummy-amazon:local .
```

本機測試 API 容器時需透過 env-file 傳入 GUMMY_API_TOKEN，並為 `/app/data` 掛載持久 volume。不要把含真實 secrets 的 env-file 複製進映像檔。沒有 token 的 0.0.0.0 API 啟動會被拒絕。

`gummy-amazon:local-test` 已在本機完成映像建置；容器可啟動 CLI、以 UID 10001 執行，未設定 `GUMMY_API_TOKEN` 時拒絕對外監聽。更新後的 `gummy-amazon:e2e` 又在內部專用 Docker 網路實測完整鏈路：容器 `init-db`、`collect --source demo` 得到 2 筆、`verify-run` 為 `PASS`，容器 API 的 `/health` 200、未授權產品讀取 401、授權後 2 筆、雲端模式 `/docs` 404。測試資料庫額度 0/30，沒有呼叫 RapidAPI；容器、網路及臨時密碼已清除。`.dockerignore` 排除本機 `outputs/` 報表，避免進入建置內容。尚未在 Cloud Run 執行，也未完成正式映像掃描或固定基底映像 digest。

## 後續 GCP 上線順序

1. 確認專案、區域與預算；若有台灣資料落地需求，再確認全部服務、備份與觀測資料的區域符合要求。
2. 建立 Artifact Registry，建置及掃描 image；固定 image digest。
3. 建立 Cloud SQL PostgreSQL、私有連線與備份；用獨立 migration 身分初始化 schema，業務服務不擁有 DDL 權限。
4. 將 `APP_ENV=cloud` 和 PostgreSQL 連線配置交給執行環境。gummy API 使用 reader DB 身分；worker 使用 writer DB 身分。Cloud Run 的檔案系統不能放本版 SQLite 額度帳本。
5. Secret Manager 保管 RAPIDAPI_KEY、DB 認證與 GUMMY_API_TOKEN；worker 才可讀取 RapidAPI secret。服務 API 需 Cloud Run IAM 驗證、HTTPS；MVP token 作額外保護，後續以 OIDC 對接。
6. 先完成下方「每日採集與報表的上雲缺口」，再建立 Cloud Run Job；初期 tasks=1、parallelism=1、max retries=0，使用一個共用配額 DB，先以 demo 和隔離 PostgreSQL 驗證，接著才在已確認供應商額度的條件下手動執行有限真實批次。**不要直接把 `python -m gummy collect --source rapidapi` 設為每日工作**：該命令未指定參數時使用預設套筒扳手查詢、最多 2 件及執行當天的 run-key，並不讀取目前五日分類計畫。
7. 建立 Cloud Run Service，命令 `python -m gummy serve --host 0.0.0.0`；讀已完成批次，不觸發採集。設定連線池、最大實例數與內部／受認證入口。
8. 完成金鑰遮蔽、真實資料核對、quota 並行測試、停止與恢復演練後，才加 Cloud Scheduler。Scheduler 呼叫 Job 的身分只需執行指定 Job 的權限；同日 key 重送不重抓。
9. 在 Secret、DB 斷線、429、時間逾期等情境確認不發布壞報表、不無限重試，並設定 log-based alerts。

Cloud Run 的任務重試與本版 run-key 不能取代完整的生產級排程追蹤。正式每日排程應加入明確 logical_date、資料範圍 scope、stale run 回收流程、schema migration 與告警；這些目前都是後續工作。

## 每日採集與報表的上雲缺口

目前 `run-plan` 可依計畫日期、固定 run-key、SHA-256 與資料庫額度護欄執行本機試點，但 `Dockerfile` 只打包 Python 程式，沒有打包 `plans/`；現有兩個 LaunchAgent 及其 09:10／09:20 觸發也只在這台 Mac 上。上雲前需把經審核的有限期計畫以固定版本放進 Job、在 Job 執行環境明確設定 Asia/Taipei 日期與 UTC 額度日的關係，並對漏跑、同日重送、失敗批次及配額耗盡做 PostgreSQL 整合測試。不可用「建立了 Job」推論分類計畫已生效。

`plan-report` 在 `APP_ENV=cloud` 會明確拒絕執行；它呼叫本機 `scripts/export_portfolio_local.sh` 和 Codex 附帶的 `@oai/artifact-tool`，兩者均不在目前 Docker image 中。要有每日雲端 XLSX，需另選可正式打包的報表執行環境與持久輸出位置，再驗證來源對帳、活頁簿讀回、人工 K–M 欄承接及同名檔防覆寫。現階段仍由本機已載入的排程輸出；GCP 服務部署不得宣稱已包含報表。

上雲驗收順序：隔離 PostgreSQL 的 demo Job → 有限計畫的無外部請求 dry-run → 共用額度與失敗停止測試 → 經授權的少量真實採集與人工核對 → 可持久保存並讀回的報表 → 最後才啟用 Cloud Scheduler。每一步保留執行紀錄與來源證據，費用、資料敏感度及區域要求先確認後再建立 GCP 資源。

成本先延後評估，但不能把 Cloud Run 的運算免費額度等同「整個架構免費」：Cloud SQL、儲存、網路、secret、registry 等需依配置核算。本次沒有建立 GCP 資源或啟用雲端排程；本機有限期 LaunchAgent 試驗另見 LOCAL_SCHEDULE.md。

參考：[Cloud Run Jobs](https://docs.cloud.google.com/run/docs/create-jobs)、[Cloudflare Python 套件支援](https://developers.cloudflare.com/workers/languages/python/packages/)。

# Windows Docker 資料契約

Windows sender 的 `full` MQTT 訊息包含 `docker`。背景採集使用執行帳號的 Docker CLI 與目前 context，可能連到遠端 daemon。可在受保護的 `.env` 設定 `DOCKER_CLI_PATH` 為管理員保護的完整執行檔路徑；執行檔、目錄鏈與非系統依賴不得允許一般使用者替換，不應直接指定使用者可寫的 Docker 安裝目錄。未設定時先搜尋 PATH，再搜尋目前帳號的 Docker Desktop 與 Program Files 標準位置。SYSTEM 排程不會自動看到其他使用者的安裝路徑，需明確指定。排程帳號需要 CLI 與 context 存取權；不需要另開 TCP 連接埠。`DOCKER_POLL_INTERVAL_SEC` 控制採集間隔，預設 30 秒，最短 1 秒。兩個 CLI 命令各有 10 秒逾時。

`status` 為 `ok` 時代表採集成功，`containers` 空陣列代表沒有容器；`error` 代表找不到 CLI、命令、逾時或輸出解析失敗。失敗時清空容器，不沿用舊成功資料。`observed_at` 是採集開始的 Unix 秒數，尚未採集時為 null。最多回報 30 個容器，超過時 `truncated` 為 true；文字欄位最多 128 字元。

同時提供 `version: 1`、`provider: docker`、本機 `node` 與成功時的 `guests`，供虛擬化讀取端使用；失敗時不包含 `guests`。guest 的 `memory_basis` 為 `docker_cli`，表示使用 CLI 報告的數值；CPU 和記憶體用量沿用該數值，記憶體百分比由用量除以上限計算。未提供的 uptime、vCPU 和 I/O 速率為 null。

每個容器包含完整 64 位十六進位 `id`、`name`、`image`、`state`、`cpu_percent`、`memory_usage_bytes` 和 `memory_limit_bytes`。缺少統計與停止容器的數值為 null；Docker 輸出可能經過四捨五入。Windows daemon 的記憶體統計是 private working set，可能沒有上限值，參見 [Docker stats 文件](https://docs.docker.com/reference/cli/docker/container/stats/)。ESP 訊息不包含此欄位。

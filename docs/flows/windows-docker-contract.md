# Windows Docker 資料契約

Windows sender 的 `full` MQTT 訊息包含 `docker`。背景採集使用執行帳號的 Docker CLI 與目前 context，可能連到遠端 daemon。排程帳號需要 CLI 與 context 存取權；不需要另開 TCP 連接埠。`DOCKER_POLL_INTERVAL_SEC` 控制採集間隔，預設 30 秒，最短 1 秒。兩個 CLI 命令各有 10 秒逾時。

`status` 為 `ok` 時代表採集成功，`containers` 空陣列代表沒有容器；`unavailable` 代表找不到 CLI，`error` 代表命令、逾時或輸出解析失敗。失敗時清空容器，不沿用舊成功資料。`observed_at` 是採集開始的 Unix 秒數，尚未採集時為 null。最多回報 30 個容器，超過時 `truncated` 為 true；文字欄位最多 128 字元。

每個容器包含完整 `id`、`name`、`image`、`state`、`cpu_percent`、`memory_usage_bytes` 和 `memory_limit_bytes`。缺少統計與停止容器的數值為 null；Docker 輸出可能經過四捨五入。Windows daemon 的記憶體統計是 private working set，可能沒有上限值，參見 [Docker stats 文件](https://docs.docker.com/reference/cli/docker/container/stats/)。ESP 訊息不包含此欄位。

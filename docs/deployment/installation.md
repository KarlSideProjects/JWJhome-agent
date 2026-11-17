# 電腦採集端安裝與維護

[返回首頁](../../README.md) · [文件中心](../README.md) · [掌機顯示端安裝](https://github.com/KarlSideProjects/JWJhome-handheld/blob/main/docs/installation.md)

本文件說明 Windows／Linux／PVE 採集端的安裝、權限變更與維護。採集端透過 MQTT 傳送資料，掌機顯示端需使用同一個 broker 與相符的主題設定。

- [Linux／PVE 安裝](#在-linux-或-pve-安裝-sender)，script：[bootstrap-source.sh](../../bootstrap-source.sh)。
- [Windows 安裝](#windows-sender-安裝與注意事項)，script：[bootstrap-source.ps1](../../bootstrap-source.ps1)。

## 在 Linux 或 PVE 安裝 Sender

在 Debian、Ubuntu、Raspberry Pi OS 或 Proxmox VE 的 Shell 執行下列命令；需要 `curl`，非 root 使用者需要 `sudo`。安裝器需要 Python 3.11 以上與 `flock`。一般 Linux 使用 Docker Compose v2，PVE 使用原生 systemd；已辨識的既有安裝會保留 `.env` 與 Docker 覆寫檔。未知目錄或有衝突的舊 PVE Docker 容器會停止安裝。一般 Linux 支援 x64 與 64 位元 ARM64，PVE 原生安裝支援 x64。

```bash
(f=$(mktemp) && trap 'rm -f "$f"' EXIT && curl -fsSL 'https://raw.githubusercontent.com/KarlSideProjects/JWJhome-agent/master/bootstrap-source.sh' -o "$f" && if [ "$(id -u)" = 0 ]; then bash "$f"; else sudo bash "$f"; fi)
```

安裝器從公開主分支解析單一 commit，下載該 commit 的來源並檢查封存檔路徑。來源及安裝器都依賴 GitHub 與 HTTPS，沒有獨立簽章。下載或檢查失敗不會更新現有程式；啟動失敗時會嘗試回復先前的程式與服務。失敗時保留的備份可能包含私有資料，請勿公開。

測試指定候選時，將下方 `COMMIT` 換成該候選完整的 40 位小寫 commit SHA；下載的 bootstrap 與它安裝的來源都會使用同一個 SHA。請在 root Shell 執行：

```bash
(COMMIT='<40 位小寫 commit SHA>'; f=$(mktemp) && trap 'rm -f "$f"' EXIT && curl -fsSL "https://raw.githubusercontent.com/KarlSideProjects/JWJhome-agent/$COMMIT/bootstrap-source.sh" -o "$f" && bash "$f" --commit "$COMMIT")
```

指定的 SHA 格式錯誤時，安裝器會在下載來源前停止。若要下載後直接以 `curl | bash` 執行，同樣必須將 SHA 同時放在網址和 `--commit` 參數中；下載失敗時使用暫存檔方式可避免執行不完整的腳本。

PVE 安裝入口會先檢查舊的 `sys-agent` Docker 容器。容器存在時會停止，請先完成下方遷移，再執行安裝。它會更新下載目錄中的程式，保留既有 `.env` 與私有 PVE 設定。

PVE 原生來源安裝會準備本機節點與 cluster ID。它以 root 使用者及 root 主要群組排程執行固定的 `/usr/bin/pvesh` 讀取 VM、CT 與磁碟 SMART，將快照目錄及檔案交給專用唯讀群組，再由非 root sender 讀取並透過 MQTT 發送。本機路徑不需要 PVE API token 或 CA；既有 token、CA 與私有設定檔不會主動刪除。PVE Docker 與舊編譯版的 HTTPS API 路徑仍使用原有 token 與 CA 設定。

### 安裝檔會做什麼

下載命令需要 root 權限，以便安裝程式、建立服務與唯讀群組。一般 Linux 使用 Python Docker sender；PVE 使用原生 systemd sender。Windows 安裝器準備 Python、uv、LibreHardwareMonitor 與開機排程。

PVE 安裝會檢查舊 Docker 容器、準備本機識別、保留 MQTT 設定，並安裝 `hwmonitor-mqtt-pve-snapshot.service`、對應 timer 與 `hwmonitor-mqtt-pve.service`。快照位於 `/run/hwmonitor-mqtt-pve/snapshot.json`。來源升級會驗證程式與目錄的 root 擁有權及寫入權限，並以隔離環境執行採集程式；直接安裝器遇到既有 runtime 會拒絕覆蓋，請使用上方來源安裝命令升級。升級失敗時會嘗試回復先前的程式、服務、排程及快照；回復也可能失敗，私有備份與錯誤日誌需妥善保管。

資料保留實際採樣時間；快照過期時 guest 資料會顯示錯誤並保留上次結果，SMART 會顯示不可用。單顆磁碟採集失敗時會標示部分失敗並保留該磁碟先前警報與舊採樣時間；資料不完整時不會宣稱健康。這些行為已有模擬測試與受限本機權限驗證，完整 systemd 和 PVE 實機驗收仍待進行。

### PVE 服務啟動權限

若安裝完成後沒有監控資料，且服務日誌出現 `200/CHDIR` 或 `Permission denied`，可能是舊安裝器在 `umask 077` 下建立了服務無法讀取的程式目錄或 Python 環境。新版安裝器會修正這些執行檔案的權限，保留既有 MQTT 與 PVE token 設定；在 PVE root shell 重跑上方下載安裝步驟。

### 舊版 PVE HTTPS 連線檢查

HTTPS 連線必須通過設定 CA 的信任鏈與主機名稱驗證。[Python 3.13 起的預設嚴格憑證檢查](https://docs.python.org/3/library/ssl.html#ssl.create_default_context)可能拒絕缺少 key usage 擴充的既有 PVE CA。採集端只在 OpenSSL 明確回報此原因 (驗證碼 `92`) 時，針對該 PVE 連線取消嚴格格式檢查並重試一次；CA 信任、憑證有效期及主機名稱驗證仍保留，不會改動 PVE 的憑證或全域 TLS 設定。

此說明適用仍使用 HTTPS API 的舊版設定。本機快照路徑不需要此連線。

### 從舊 Docker sender 遷移到 PVE 原生服務

以下遷移步驟尚待 PVE 實機重裝驗證。先退出監控安裝程序，再以 root 執行。此步驟會暫停舊監控；保留舊容器作為回復備份，不影響 CT／VM：

```bash
docker stop sys-agent && docker rename sys-agent sys-agent-before-native
```

重新執行上方下載安裝命令。停止但仍名為 `sys-agent` 的容器也會阻擋安裝；不要執行會停止其他服務的 `docker compose down`。若備份名稱已存在，先檢查既有備份，不要覆寫。

安裝後檢查服務與日誌，並在 R36 開啟 PVE 主機確認 CT／VM 清單：

```bash
systemctl status hwmonitor-mqtt-pve --no-pager
journalctl -u hwmonitor-mqtt-pve -n 30 --no-pager
```

若需回復保留的舊容器，先停用新版，再恢復舊容器名稱與啟動。若新服務尚未建立，略過第一個指令：

```bash
systemctl disable --now hwmonitor-mqtt-pve
docker rename sys-agent-before-native sys-agent && docker start sys-agent
```

ARM64 與 PVE 來源安裝仍需各自實機驗證。Windows 請使用 [Windows sender 安裝方式](#windows-sender-安裝與注意事項)。

## Windows Sender 安裝與注意事項

在 64 位元「系統管理員」PowerShell 執行以下命令。首次執行會隱藏 MQTT 密碼輸入，並建立僅 Administrators 與 SYSTEM 可讀的 `.env`；已辨識的既有設定會保留。安裝器下載固定 commit 的 Python 來源，準備 Python、uv、LibreHardwareMonitor 與 PawnIO，註冊開機排程並啟動 sender。未知目錄會停止安裝。

```powershell
& ([scriptblock]::Create((Invoke-WebRequest -UseBasicParsing -Uri 'https://raw.githubusercontent.com/KarlSideProjects/JWJhome-agent/master/bootstrap-source.ps1' -ErrorAction Stop).Content))
```

檢查排程狀態：

```powershell
Get-ScheduledTaskInfo -TaskName HWMonitorMQTTSender
```

### 重要注意事項

- 不要同時啟動多個 sender 實例，否則 viewer 可能出現 CPU/GPU 資料跳動或 `NA` 閃爍。
- 若看到 `Access is denied (0x80070005)`，請改用系統管理員 PowerShell。
- 若看到 `WinError 740`，代表目前權限不足以啟動 LibreHardwareMonitor。
- Windows sender 以 `LibreHardwareMonitorLib.dll`（`pythonnet`）在程序內讀取 CPU/GPU 溫度。LibreHardwareMonitor 0.9.5 起移除 WMI provider，並改用 [PawnIO](https://pawnio.eu/) 驅動取代 WinRing0；未安裝 PawnIO 時 GPU 溫度仍可讀取，CPU 溫度會缺少。安裝器會靜默安裝固定版本與 SHA-256 的 PawnIO 2.2.0。
- 舊版 LibreHardwareMonitor 的 WMI namespace（`root/LibreHardwareMonitor`）與 `nvidia-smi` 仍作為 fallback。
- 安裝後通常不需要重開機；若安裝器顯示 PawnIO 需要重新開機，重開後 CPU 溫度才會出現。

### 清理重複 sender（必要時）

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'hwmonitor_mqtt\.agents\.agent_sender_(windows|async)' -or $_.CommandLine -match 'start_windows_sender\.ps1' } |
  Select-Object ProcessId, Name, CommandLine

# 確認後再停止
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'hwmonitor_mqtt\.agents\.agent_sender_(windows|async)' -or $_.CommandLine -match 'start_windows_sender\.ps1' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

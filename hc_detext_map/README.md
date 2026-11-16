# hc_detext_map

迴轉壽司軌道餐盤追蹤。**三個完全獨立的專案**,各自 git、各自部署、各自的 `CLAUDE.md`。
彼此只透過 MQTT 和 HTTP/WS 溝通,沒有共用程式碼。跨層契約各自複製一份在 `*/docs/`。

| 專案 | 角色 |
|------|------|
| [`server/`](server/)   | Fusion 引擎:收 MQTT → 去重 → 還原軌道餐盤 → 偵測消失的盤並歸桌。結果推 backend(HTTP),即時位置推 frontend(WS)。 |
| [`backend/`](backend/) | REST API + MariaDB/MySQL(phpMyAdmin)。存 pickup、場次、帳單、版本化地圖、異常、修正、帳號。 |
| [`frontend/`](frontend/) | Vue 3 + Vite 儀表板 + 地圖編輯器。Nginx 反向代理。 |

各專案的 `docs/` 內有它需要的契約:`mqtt-contract.md`(Pi↔server)、`api-contract.md`、`map-schema.md`。
契約有異動時,三份要一起更新。

## 一鍵啟動(本機開發,免 Docker)

`start.py` 用純 Python broker(amqtt)+ SQLite 把三個專案一起跑起來,Windows / macOS / Linux 皆可:

```
python start.py --install     # 第一次:裝 Python + npm 依賴
python start.py               # 啟動 broker + backend + server + 假 Pi + frontend
python start.py --stop        # 停掉上一次啟動的全部服務
python start.py --scenario occlusion --no-frontend
```

啟動後開 http://localhost:5173 。各服務 log 在 `logs/*.log`。Ctrl+C 全部停止。

> 正式部署仍用各專案自己的 `docker-compose.yml`(Mosquitto / MariaDB)。`start.py` 只是開發便利工具。

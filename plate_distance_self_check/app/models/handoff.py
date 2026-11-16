"""MQTT handoff domain model for the two-camera distance self-check.

The upstream camera publishes a locked plate result.  The downstream camera
matches arrivals in FIFO order, without using color, and only then compares
its independently locked color with the upstream reference.
"""

from __future__ import annotations

import csv
import hashlib
import json
import statistics
import threading
import time
from collections import Counter, deque
from pathlib import Path
from uuid import uuid4

import paho.mqtt.client as mqtt


TOPIC_ROOT = "plate-self-check/handoff/v1"
HEARTBEAT_SECONDS = 2.0
HEARTBEAT_MISSING_SECONDS = 6.0
EVENT_FLICKER_SECONDS = 1.0
# The cameras in this installation are normally about four seconds apart.
# This bootstrap estimate lets pickup detection work from the first plate;
# after three successful handoffs it is replaced by the session median.
DEFAULT_TRAVEL_SECONDS = 3.8
TRAVEL_WINDOW_MIN_SLACK_SECONDS = 1.25
TRAVEL_WINDOW_MAX_SLACK_SECONDS = 2.0
ALIGNMENT_GAP_COST = 1.25
ALIGNMENT_MAX_TIME_ERROR_SECONDS = 2.5


def align_monotonic_events(
    upstream: list[dict],
    downstream: list[dict],
    expected_travel_s: float = DEFAULT_TRAVEL_SECONDS,
) -> list[dict]:
    """Globally align two ordered camera streams without allowing overtakes.

    A match pays normalized travel-time error.  Skipping either side pays a
    fixed gap cost.  Unlike online FIFO, a missing plate therefore does not
    force every following plate to shift by one position.
    """
    n, m = len(upstream), len(downstream)
    inf = float("inf")
    dp = [[inf] * (m + 1) for _ in range(n + 1)]
    previous: list[list[tuple[int, int, str] | None]] = [
        [None] * (m + 1) for _ in range(n + 1)
    ]
    dp[0][0] = 0.0
    for i in range(1, n + 1):
        dp[i][0] = dp[i - 1][0] + ALIGNMENT_GAP_COST
        previous[i][0] = (i - 1, 0, "skip_upstream")
    for j in range(1, m + 1):
        dp[0][j] = dp[0][j - 1] + ALIGNMENT_GAP_COST
        previous[0][j] = (0, j - 1, "skip_downstream")

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            choices = [
                (dp[i - 1][j] + ALIGNMENT_GAP_COST, i - 1, j, "skip_upstream"),
                (dp[i][j - 1] + ALIGNMENT_GAP_COST, i, j - 1, "skip_downstream"),
            ]
            travel = (
                float(downstream[j - 1]["first_seen_at"])
                - float(upstream[i - 1]["detected_at"])
            )
            error = abs(travel - float(expected_travel_s))
            if error <= ALIGNMENT_MAX_TIME_ERROR_SECONDS:
                match_cost = error / TRAVEL_WINDOW_MIN_SLACK_SECONDS
                choices.append((dp[i - 1][j - 1] + match_cost, i - 1, j - 1, "match"))
            best = min(choices, key=lambda item: item[0])
            dp[i][j] = best[0]
            previous[i][j] = (best[1], best[2], best[3])

    operations = []
    i, j = n, m
    while i or j:
        step = previous[i][j]
        if step is None:  # pragma: no cover - defensive guard
            break
        pi, pj, operation = step
        item = {"operation": operation}
        if operation in {"match", "skip_upstream"}:
            item["upstream"] = upstream[i - 1]
        if operation in {"match", "skip_downstream"}:
            item["downstream"] = downstream[j - 1]
        if operation == "match":
            item["travel_time_s"] = round(
                float(downstream[j - 1]["first_seen_at"])
                - float(upstream[i - 1]["detected_at"]),
                4,
            )
        operations.append(item)
        i, j = pi, pj
    operations.reverse()
    return operations


def build_handoff_markdown(summary: dict, rows: list[dict]) -> str:
    """Build the human-readable handoff report from saved result rows."""
    matched_rows = [
        row for row in rows if row.get("handoff_status") == "fifo_paired"
    ]
    correct_rows = [row for row in matched_rows if str(row.get("color_correct")) == "1"]
    missing_rows = [row for row in rows if row.get("handoff_status") == "downstream_missing"]
    pickup_rows = [
        row for row in rows
        if row.get("handoff_status") == "removed_between_cameras"
    ]
    upstream_take_rows = [
        row for row in rows if row.get("handoff_status") == "removed_at_upstream"
    ]
    downstream_take_rows = [
        row for row in rows if row.get("handoff_status") == "removed_at_downstream"
    ]
    extra_rows = [row for row in rows if row.get("handoff_status") == "downstream_extra"]
    travel_times = [
        float(row["travel_time_s"])
        for row in matched_rows if str(row.get("travel_time_s", "")).strip()
    ]
    downstream_color_counts: dict[str, int] = {}
    comparison_counts: dict[tuple[str, str], int] = {}
    for row in rows:
        downstream_color = str(row.get("downstream_color", "")).strip()
        upstream_color = str(row.get("upstream_color", "")).strip()
        if downstream_color:
            downstream_color_counts[downstream_color] = (
                downstream_color_counts.get(downstream_color, 0) + 1
            )
        if upstream_color and downstream_color:
            key = (upstream_color, downstream_color)
            comparison_counts[key] = comparison_counts.get(key, 0) + 1
    lines = [
        "# 兩鏡頭銜接分析",
        "",
        "## 整體結果",
        "",
        "| 項目 | 結果 |",
        "|---|---:|",
        f"| cam-2 收到的 cam-3 盤數 | {summary['upstream_received']} |",
        f"| FIFO 配對（身分未驗證） | {summary['fifo_paired']} |",
        f"| 下游漏盤／未完成 | {summary['downstream_missing']} |",
        f"| 疑似在兩鏡頭間拿走 | {summary.get('removed_between_cameras', 0)} |",
        f"| 疑似在上游 cam-3 拿走 | {summary.get('removed_at_upstream', 0)} |",
        f"| 疑似在下游 cam-2 拿走 | {summary.get('removed_at_downstream', 0)} |",
        f"| 下游多餘盤 | {summary['downstream_extra']} |",
        f"| FIFO 配對率（非身分準確率） | {summary['fifo_pairing_rate']:.1%} |" if summary.get("fifo_pairing_rate") is not None else "| FIFO 配對率（非身分準確率） | 無資料 |",
        f"| FIFO 配對後顏色一致率 | {summary['color_agreement_rate_among_fifo_pairs']:.1%} |" if summary.get("color_agreement_rate_among_fifo_pairs") is not None else "| FIFO 配對後顏色一致率 | 無資料 |",
        "| 已由真值確認為同一實體盤 | 0 |",
        "",
        "計算方式：",
        "",
        "```text",
        "FIFO 配對率 = FIFO 配對盤數 / cam-2 實際收到的 cam-3 盤數",
        "顏色一致率 = 上下游顏色一致盤數 / FIFO 配對盤數",
        "FIFO 配對與顏色一致都不能證明是同一實體盤；身分準確率需要獨立真值。",
        "```",
        "",
        "> 注意：目前分母是 cam-2 已收到的 MQTT 盤數；cam-3 辨識到但 MQTT 發送失敗的盤，不會進入此分母，應搭配下方 MQTT 發送失敗數一起判讀。",
        "",
        "## 下游獨立判斷與上游差異",
        "",
        "cam-2 的模型、顏色投票與最終鎖定皆獨立執行；cam-3 傳來的顏色不參與 cam-2 判色或選擇配對，只在兩邊都完成後比較。",
        "",
        "### cam-2 自己鎖定的顏色",
        "",
        "| cam-2 顏色 | 盤數 |",
        "|---|---:|",
    ]
    if downstream_color_counts:
        for color, count in sorted(downstream_color_counts.items()):
            lines.append(f"| {color} | {count} |")
    else:
        lines.append("| 無資料 | 0 |")
    lines.extend([
        "",
        "### cam-3 收到值 → cam-2 自己判斷",
        "",
        "| cam-3 顏色 | cam-2 顏色 | 盤數 | 比較結果 |",
        "|---|---|---:|---|",
    ])
    if comparison_counts:
        for (upstream_color, downstream_color), count in sorted(comparison_counts.items()):
            result = "一致" if upstream_color == downstream_color else "不一致"
            lines.append(
                f"| {upstream_color} | {downstream_color} | {count} | {result} |"
            )
    else:
        lines.append("| 無資料 | 無資料 | 0 | 無法比較 |")
    lines.extend([
        "",
        "`downstream_color` 是 cam-2 自己的判斷；`upstream_color` 是 MQTT 收到的參考值；`color_correct` 只表示兩者是否一致。",
        "",
        "## 每圈結果",
        "",
        "| Lap | 上游盤數 | FIFO 配對 | 顏色一致 | 中途拿走 | 漏盤／未完成 | 多餘盤 | FIFO 配對率 | 顏色一致率 |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    lap_ids = sorted({int(row["lap_id"]) for row in rows if str(row.get("lap_id", "")).strip()})
    for lap_id in lap_ids:
        lap_rows = [row for row in rows if str(row.get("lap_id", "")) == str(lap_id)]
        lap_upstream = [row for row in lap_rows if str(row.get("upstream_pass_id", "")).strip()]
        lap_matched = [
            row for row in lap_rows
            if row.get("handoff_status") == "fifo_paired"
        ]
        lap_correct = [row for row in lap_matched if str(row.get("color_correct")) == "1"]
        lap_missing = sum(row.get("handoff_status") == "downstream_missing" for row in lap_rows)
        lap_pickup = sum(
            row.get("handoff_status") == "removed_between_cameras"
            for row in lap_rows
        )
        lap_extra = sum(row.get("handoff_status") == "downstream_extra" for row in lap_rows)
        handoff_rate = len(lap_matched) / len(lap_upstream) if lap_upstream else None
        color_rate = len(lap_correct) / len(lap_matched) if lap_matched else None
        lines.append(
            f"| {lap_id} | {len(lap_upstream)} | {len(lap_matched)} | {len(lap_correct)} "
            f"| {lap_pickup} | {lap_missing} | {lap_extra} | "
            f"{f'{handoff_rate:.1%}' if handoff_rate is not None else '無資料'} | "
            f"{f'{color_rate:.1%}' if color_rate is not None else '無資料'} |"
        )
    if not lap_ids:
        lines.append("| - | 0 | 0 | 0 | 0 | 0 | 0 | 無資料 | 無資料 |")

    lines.extend([
        "",
        "## 每盤銜接結果",
        "",
        "| Lap | 順序 | cam-3 Plate ID | cam-2 Plate ID | 上游顏色 | 下游顏色 | 結果 | 行程時間 | 下游 Tracker IDs |",
        "|---:|---:|---:|---:|---|---|---|---:|---|",
    ])
    def row_order(row):
        sequence = str(row.get("sequence", "")).strip()
        return (int(row.get("lap_id") or 0), int(sequence) if sequence else 10**9)
    for row in sorted(rows, key=row_order):
        status = str(row.get("handoff_status", ""))
        if status == "fifo_paired":
            result = (
                "FIFO 配對／顏色一致／身分未驗證"
                if str(row.get("color_correct")) == "1"
                else "FIFO 配對／顏色不一致／身分未驗證"
            )
        elif status == "removed_between_cameras":
            result = "疑似在兩鏡頭間拿走"
        elif status == "removed_at_upstream":
            result = "疑似在上游 cam-3 拿走"
        elif status == "removed_at_downstream":
            result = "疑似在下游 cam-2 拿走"
        elif status == "downstream_missing":
            result = "下游漏盤／未完成"
        else:
            result = "下游多餘盤"
        travel = row.get("travel_time_s", "")
        travel_text = f"{float(travel):.3f} s" if str(travel).strip() else "-"
        lines.append(
            f"| {row.get('lap_id', '-') or '-'} | {row.get('sequence', '-') or '-'} "
            f"| {row.get('upstream_pass_id', '-') or '-'} | {row.get('downstream_pass_id', '-') or '-'} "
            f"| {row.get('upstream_color', '-') or '-'} | {row.get('downstream_color', '-') or '-'} "
            f"| {result} | {travel_text} | {row.get('downstream_tracker_ids', '-') or '-'} |"
        )

    mismatches = [row for row in matched_rows if str(row.get("color_correct")) != "1"]
    lines.extend(["", "## 拿走位置判定", ""])
    color_names = {"orange": "O", "yellow": "Y", "light_green": "G"}
    def append_take_summary(label: str, take_rows: list[dict]) -> None:
        if not take_rows:
            lines.append(f"- {label}：0 盤。")
            return
        counts = Counter(str(row.get("upstream_color") or row.get("downstream_color") or "") for row in take_rows)
        colors = "、".join(
            f"{color_names.get(color, color)} × {count}"
            for color, count in sorted(counts.items())
        )
        sequences = "、".join(
            f"#{row.get('sequence')}"
            for row in sorted(take_rows, key=lambda item: int(item.get("sequence") or 0))
            if str(row.get("sequence", "")).strip()
        )
        suffix = f"；上游順序 {sequences}" if sequences else ""
        lines.append(f"- {label}：{len(take_rows)} 盤（{colors}）{suffix}。")
    append_take_summary("上游 cam-3 範圍拿走", upstream_take_rows)
    append_take_summary("兩鏡頭之間拿走", pickup_rows)
    append_take_summary("下游 cam-2 範圍拿走", downstream_take_rows)

    lines.extend(["", "### 兩鏡頭之間明細", ""])
    if pickup_rows:
        pickup_counts = Counter(str(row.get("upstream_color", "")) for row in pickup_rows)
        lines.append(f"- 判定有拿走：共 {len(pickup_rows)} 盤。")
        lines.append(
            "- 顏色數量：" + "、".join(
                f"{color_names.get(color, color)} × {count}"
                for color, count in sorted(pickup_counts.items())
            ) + "。"
        )
        lines.append(
            "- 上游順序：" + "、".join(
                f"#{row.get('sequence')} {color_names.get(str(row.get('upstream_color', '')), row.get('upstream_color', ''))}"
                for row in sorted(pickup_rows, key=lambda item: int(item.get("sequence") or 0))
            ) + "。"
        )
    else:
        lines.append("- 未判定有盤子在兩鏡頭之間被拿走。")
    lines.append(
        "- 判定依據：上游已完成鎖定，且超過本次行程時間窗仍未抵達下游；"
        "若 cam-2 本身漏辨識，仍可能形成相同現象，需搭配影像異常紀錄確認。"
    )

    lines.extend(["", "## 異常摘要", ""])
    if not mismatches and not missing_rows and not extra_rows and not pickup_rows and not upstream_take_rows and not downstream_take_rows:
        lines.append("- 本次沒有顏色不一致、下游漏盤或下游多餘盤。")
    else:
        for row in mismatches:
            lines.append(
                f"- 顏色不一致：L{row.get('lap_id')}，cam-3 P{row.get('upstream_pass_id')} "
                f"`{row.get('upstream_color')}` → cam-2 P{row.get('downstream_pass_id')} `{row.get('downstream_color')}`。"
            )
        for row in missing_rows:
            lines.append(
                f"- 下游漏盤／未完成：L{row.get('lap_id')}，cam-3 P{row.get('upstream_pass_id')}，順序 {row.get('sequence')}。"
            )
        for row in pickup_rows:
            lines.append(
                f"- 疑似中途拿走：L{row.get('lap_id')}，順序 {row.get('sequence')}，"
                f"cam-3 P{row.get('upstream_pass_id')} `{row.get('upstream_color')}`。"
            )
        for row in upstream_take_rows:
            lines.append(
                f"- 疑似上游拿走：L{row.get('lap_id')}，順序 {row.get('sequence')}，"
                f"P{row.get('upstream_pass_id')} `{row.get('upstream_color')}`。"
            )
        for row in downstream_take_rows:
            lines.append(
                f"- 疑似下游拿走：L{row.get('lap_id')}，順序 {row.get('sequence')}，"
                f"cam-2 P{row.get('downstream_pass_id')} `{row.get('downstream_color')}`。"
            )
        for row in extra_rows:
            lines.append(
                f"- 下游多餘盤：L{row.get('lap_id')}，cam-2 P{row.get('downstream_pass_id')}，沒有可配對的上游資料。"
            )

    lines.extend(["", "## 行程時間", ""])
    if travel_times:
        lines.extend([
            f"- FIFO 配對樣本（身分未驗證）：{len(travel_times)}",
            f"- 最短：{min(travel_times):.3f} 秒",
            f"- 中位數：{statistics.median(travel_times):.3f} 秒",
            f"- 最長：{max(travel_times):.3f} 秒",
            "- 行程時間窗已用於中途拿走判定與後續盤重新對齊。",
        ])
    else:
        lines.append("- 無 FIFO 配對樣本。")

    lines.extend([
        "",
        "## MQTT 穩定性",
        "",
        "| Camera | 連線 | 斷線 | 重連 | 發送成功 | 發送失敗 | 收到訊息 | 重複 | 亂序 | 事件閃爍 | Heartbeat 中斷 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for camera_id, stats in summary.get("mqtt", {}).items():
        lines.append(
            f"| {camera_id} | {stats.get('connect_count', 0)} | {stats.get('disconnect_count', 0)} "
            f"| {stats.get('reconnect_count', 0)} | {stats.get('publish_success', 0)} "
            f"| {stats.get('publish_failure', 0)} | {stats.get('messages_received', 0)} "
            f"| {stats.get('duplicate_messages', 0)} | {stats.get('out_of_order_events', 0)} "
            f"| {stats.get('event_flickers', 0)} | {stats.get('heartbeat_gaps', 0)} |"
        )
    lines.extend([
        "",
        "判定說明：發送失敗包含未連上 broker、斷線期間發布、MQTT 回傳錯誤或發送例外；事件閃爍是同一盤同類事件在 1 秒內重複；Heartbeat 中斷是超過 6 秒未收到對方狀態。",
        "",
    ])
    # Show each camera's own judgment independently of MQTT/FIFO pairing.
    lines.extend([
        "", "## 上游單獨判斷（cam-3）", "",
        "此表只列 cam-3 自己鎖定並送出的結果，不使用下游結果修正。", "",
        "| Lap | 順序 | 上游 Plate ID | 上游判斷顏色 | 信心值 | MQTT 發送時間 |",
        "|---:|---:|---:|---|---:|---:|",
    ])
    upstream_seen: set[tuple[str, str]] = set()
    upstream_rows = []
    for row in sorted(rows, key=row_order):
        pass_id = str(row.get("upstream_pass_id", "")).strip()
        if not pass_id:
            continue
        key = (str(row.get("upstream_session_id", "")), pass_id)
        if key in upstream_seen:
            continue
        upstream_seen.add(key)
        upstream_rows.append(row)
    if upstream_rows:
        for row in upstream_rows:
            lines.append(
                f"| {row.get('lap_id', '-') or '-'} | {row.get('sequence', '-') or '-'} "
                f"| {row.get('upstream_pass_id', '-') or '-'} | {row.get('upstream_color', '-') or '-'} "
                f"| {row.get('upstream_confidence', '-') or '-'} "
                f"| {row.get('upstream_published_at', '-') or '-'} |"
            )
    else:
        lines.append("| - | - | - | 無資料 | - | - |")

    lines.extend([
        "", "## 下游單獨判斷（cam-2）", "",
        "此表只列 cam-2 自己實際偵測並鎖定的結果，不以上游顏色作為判斷輸入。", "",
        "| Lap | 下游 Plate ID | 下游判斷顏色 | 信心值 | Tracker IDs | 第一次看到時間 | 鎖定時間 |",
        "|---:|---:|---|---:|---|---:|---:|",
    ])
    downstream_seen: set[tuple[str, str]] = set()
    downstream_rows = []
    for row in sorted(rows, key=row_order):
        pass_id = str(row.get("downstream_pass_id", "")).strip()
        if not pass_id:
            continue
        key = (str(row.get("lap_id", "")), pass_id)
        if key in downstream_seen:
            continue
        downstream_seen.add(key)
        downstream_rows.append(row)
    if downstream_rows:
        def downstream_pass_order(row):
            value = str(row.get("downstream_pass_id", "")).strip()
            return int(value) if value.isdigit() else 10**9

        downstream_rows.sort(key=downstream_pass_order)
        for row in downstream_rows:
            lines.append(
                f"| {row.get('lap_id', '-') or '-'} | {row.get('downstream_pass_id', '-') or '-'} "
                f"| {row.get('downstream_color', '-') or '-'} | {row.get('downstream_confidence', '-') or '-'} "
                f"| {row.get('downstream_tracker_ids', '-') or '-'} "
                f"| {row.get('downstream_first_seen_at', '-') or '-'} "
                f"| {row.get('downstream_locked_at', '-') or '-'} |"
            )
    else:
        lines.append("| - | - | 無資料 | - | - | - | - |")

    upstream_colors = [str(row.get("upstream_color", "")) for row in upstream_rows]
    downstream_colors = [str(row.get("downstream_color", "")) for row in downstream_rows]
    compared = min(len(upstream_colors), len(downstream_colors))
    independently_equal = sum(
        upstream_colors[index] == downstream_colors[index]
        for index in range(compared)
    )
    lines.extend(["", "### 兩邊獨立順序比較", ""])
    if compared:
        lines.append(
            f"- 依兩邊各自 Plate ID 順序比較：{independently_equal}/{compared} "
            f"（{independently_equal / compared:.1%}）顏色一致。"
        )
        if len(upstream_colors) != len(downstream_colors):
            lines.append(
                f"- 盤數不同：上游 {len(upstream_colors)} 盤，下游 {len(downstream_colors)} 盤。"
            )
    else:
        lines.append("- 沒有足夠資料可比較。")

    return "\n".join(lines)


class HandoffMQTT:
    HC_COLOR_CODES = {
        "orange": "#f28c28", "yellow": "#f2cf3a",
        "light_green": "#88d66c", "green": "#4cae7a",
        "red": "#e0564c", "gold": "#d4af37", "black": "#2f333d",
        "white": "#f2f2f2", "unknown": "#8892a6",
    }

    def __init__(
        self,
        role: str,
        camera_id: str,
        peer_camera_id: str,
        output_directory: Path,
        host: str = "127.0.0.1",
        port: int = 1883,
        username: str = "",
        password: str = "",
        qos: int = 1,
        matching_mode: str = "fifo",
    ) -> None:
        self.role = role
        self.camera_id = camera_id
        self.peer_camera_id = peer_camera_id
        self.output_directory = Path(output_directory)
        self.host = host
        self.port = int(port)
        self.qos = int(qos)
        if matching_mode not in {"fifo", "dp"}:
            raise ValueError("matching_mode must be 'fifo' or 'dp'")
        self.matching_mode = matching_mode
        self.session_id = f"{camera_id}-{time.strftime('%Y%m%d-%H%M%S')}"
        self._lock = threading.RLock()
        self._connected = False
        self._ever_connected = False
        self._stopped = False
        self._last_heartbeat_sent = 0.0
        self._next_sequence = 1
        self._pending: deque[dict] = deque()
        self._matched_by_pass: dict[int, dict] = {}
        self._upstream_lifecycle: dict[tuple[str, int], dict] = {}
        self._downstream_lifecycle: dict[int, dict] = {}
        self._seen_message_ids: set[str] = set()
        self._last_event_signature: dict[tuple[str, str], float] = {}
        self._peer_last_heartbeat: dict[str, float] = {}
        self._travel_samples: list[float] = []
        self._live_alignment_operations: list[dict] = []
        self._alignment_candidates: list[dict] = []
        self._reported_unarrived: set[tuple[str, int]] = set()
        self.rows: list[dict] = []
        self.events: list[dict] = []
        self.stats = {
            camera_id: self._empty_stats(),
            peer_camera_id: self._empty_stats(),
        }

        callback_api = getattr(mqtt, "CallbackAPIVersion", None)
        if callback_api is not None:
            self.client = mqtt.Client(
                callback_api.VERSION2,
                client_id=f"self-check-{camera_id}-{uuid4().hex[:8]}",
                protocol=mqtt.MQTTv311,
            )
        else:  # pragma: no cover - paho 1.x compatibility
            self.client = mqtt.Client(
                client_id=f"self-check-{camera_id}-{uuid4().hex[:8]}",
                protocol=mqtt.MQTTv311,
            )
        if username:
            self.client.username_pw_set(username, password)
        # hc_detext_map uses this retained status as its liveness source. A
        # client has one LWT, so the interoperable status topic owns it.
        self.client.will_set(
            f"sushi/status/{self.camera_id}",
            json.dumps({"state": "offline", "reason": "lwt"}),
            qos=self.qos,
            retain=True,
        )
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    @staticmethod
    def _empty_stats() -> dict:
        return {
            "connect_count": 0,
            "disconnect_count": 0,
            "reconnect_count": 0,
            "publish_success": 0,
            "publish_failure": 0,
            "messages_received": 0,
            "duplicate_messages": 0,
            "out_of_order_events": 0,
            "event_flickers": 0,
            "heartbeat_gaps": 0,
            "last_heartbeat": None,
        }

    def start(self) -> None:
        self.client.connect_async(self.host, self.port, keepalive=15)
        self.client.loop_start()

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        try:
            self._publish_hc_status("offline")
            self._publish("status", {"state": "offline"})
            self.client.disconnect()
        finally:
            self.client.loop_stop()
            self.save()

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties=None):
        ok = not bool(getattr(reason_code, "is_failure", False))
        if isinstance(reason_code, (int, float)):
            ok = int(reason_code) == 0
        if not ok:
            return
        with self._lock:
            stats = self.stats[self.camera_id]
            stats["connect_count"] += 1
            if self._ever_connected:
                stats["reconnect_count"] += 1
            self._ever_connected = True
            self._connected = True
        client.subscribe(f"{TOPIC_ROOT}/event/+", qos=self.qos)
        client.subscribe(f"{TOPIC_ROOT}/status/+", qos=self.qos)
        self._publish("status", {"state": "online"})
        self._publish_hc_status("online")

    def _on_disconnect(self, _client, _userdata, _disconnect_flags=None,
                       _reason_code=None, _properties=None):
        with self._lock:
            if self._connected and not self._stopped:
                self.stats[self.camera_id]["disconnect_count"] += 1
            self._connected = False

    def _publish(self, kind: str, body: dict) -> bool:
        payload = {
            "schema": 1,
            "message_id": uuid4().hex,
            "camera_id": self.camera_id,
            "role": self.role,
            "session_id": self.session_id,
            "kind": kind,
            "timestamp": time.time(),
            **body,
        }
        topic_group = "status" if kind in {"status", "heartbeat"} else "event"
        try:
            info = self.client.publish(
                f"{TOPIC_ROOT}/{topic_group}/{self.camera_id}",
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                qos=self.qos,
                retain=False,
            )
            ok = info.rc == mqtt.MQTT_ERR_SUCCESS
        except Exception:
            ok = False
        self.stats[self.camera_id][
            "publish_success" if ok else "publish_failure"
        ] += 1
        self.events.append({**payload, "direction": "sent", "publish_ok": int(ok)})
        return ok

    def _publish_hc_status(self, state: str) -> bool:
        payload = {"state": state, "reason": "ok"}
        try:
            info = self.client.publish(
                f"sushi/status/{self.camera_id}",
                json.dumps(payload, separators=(",", ":")),
                qos=self.qos,
                retain=True,
            )
            return info.rc == mqtt.MQTT_ERR_SUCCESS
        except Exception:
            return False

    def _publish_hc_event(
        self, event: str, plate_id: int, color: str, local_pass_id: int,
        extra: dict | None = None,
    ) -> bool:
        """Publish hc_detext_map's Pi-to-fusion v1 event contract."""
        operational_color = str(color or "unknown").lower()
        payload = {
            "event": event,
            "plate_id": int(plate_id),
            "class": operational_color,
            "color": self.HC_COLOR_CODES.get(
                operational_color, self.HC_COLOR_CODES["unknown"]
            ),
            "source": "plate_distance_self_check",
            "role": self.role,
            "session_id": self.session_id,
            "local_pass_id": int(local_pass_id),
            "raw_operational_color": operational_color,
            **(extra or {}),
        }
        topic = f"sushi/events/{self.camera_id}"
        try:
            info = self.client.publish(
                topic,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                qos=self.qos,
                retain=False,
            )
            ok = info.rc == mqtt.MQTT_ERR_SUCCESS
        except Exception:
            ok = False
        self.events.append({
            "schema": "hc_detext_map/v1", "camera_id": self.camera_id,
            "timestamp": time.time(), "kind": event, "topic": topic,
            **payload, "direction": "sent_hc", "publish_ok": int(ok),
        })
        return ok

    def _removal_event_id(self, state: dict) -> str:
        return (
            f"{self.session_id}:{self.camera_id}:"
            f"{int(state['pass_id'])}:local-removal"
        )

    def _hc_plate_id(self, state: dict) -> int:
        if self.role == "upstream" and state.get("handoff_sequence") is not None:
            return int(state["handoff_sequence"])
        return int(state["pass_id"])

    def suspect_local_removal(self, state: dict) -> None:
        color = str(
            state.get("final_color")
            or state.get("handoff_color")
            or "unknown"
        )
        self._publish_hc_event(
            "catch_suspected", self._hc_plate_id(state), color,
            int(state["pass_id"]),
            {"event_id": self._removal_event_id(state), "status": "suspected"},
        )

    def revoke_local_removal(self, state: dict) -> None:
        color = str(
            state.get("final_color")
            or state.get("handoff_color")
            or "unknown"
        )
        self._publish_hc_event(
            "catch_revoked", self._hc_plate_id(state), color,
            int(state["pass_id"]),
            {"event_id": self._removal_event_id(state), "status": "revoked"},
        )

    def tick(self) -> None:
        now = time.time()
        if now - self._last_heartbeat_sent >= HEARTBEAT_SECONDS:
            self._last_heartbeat_sent = now
            self._publish_hc_status("online")
            self._publish(
                "heartbeat",
                {
                    "state": "online",
                    "mqtt_stats": self._public_stats(self.stats[self.camera_id]),
                },
            )
        for camera_id, last_seen in list(self._peer_last_heartbeat.items()):
            stats = self.stats.setdefault(camera_id, self._empty_stats())
            if now - last_seen > HEARTBEAT_MISSING_SECONDS:
                marker = int(now // HEARTBEAT_MISSING_SECONDS)
                if stats.get("_gap_marker") != marker:
                    stats["heartbeat_gaps"] += 1
                    stats["_gap_marker"] = marker
        if self.role == "downstream" and self.matching_mode == "dp":
            with self._lock:
                self._recompute_live_alignment()
        elif self.role == "downstream":
            with self._lock:
                self._expire_overdue_pending(now)

    def _recompute_live_alignment(self) -> None:
        if self.role != "downstream" or self.matching_mode != "dp":
            return
        upstream = sorted(
            (
                {**item, "detected_at": float(item.get("timestamp", 0.0))}
                for item in self._upstream_lifecycle.values()
                if not item.get("removed")
            ),
            key=lambda item: int(item.get("sequence", 0)),
        )
        downstream = sorted(
            self._downstream_lifecycle.values(),
            key=lambda item: (
                float(item.get("first_seen_at", 0.0)),
                int(item.get("pass_id", 0)),
            ),
        )
        operations = align_monotonic_events(
            upstream, downstream, DEFAULT_TRAVEL_SECONDS
        )
        selected = {
            (int(item["upstream"].get("upstream_pass_id", 0)),
             int(item["downstream"].get("pass_id", 0)))
            for item in operations if item["operation"] == "match"
        }
        candidates = []
        for source in upstream:
            for target in downstream:
                travel = (
                    float(target["first_seen_at"])
                    - float(source["detected_at"])
                )
                error = abs(travel - DEFAULT_TRAVEL_SECONDS)
                allowed = error <= ALIGNMENT_MAX_TIME_ERROR_SECONDS
                pair = (
                    int(source.get("upstream_pass_id", 0)),
                    int(target.get("pass_id", 0)),
                )
                candidates.append({
                    "source_camera_id": self.peer_camera_id,
                    "source_local_pass_id": pair[0],
                    "source_sequence": source.get("sequence", ""),
                    "target_camera_id": self.camera_id,
                    "target_local_pass_id": pair[1],
                    "travel_time_s": round(travel, 4),
                    "expected_travel_time_s": DEFAULT_TRAVEL_SECONDS,
                    "time_error_s": round(error, 4),
                    "total_cost": (
                        round(error / TRAVEL_WINDOW_MIN_SLACK_SECONDS, 6)
                        if allowed else None
                    ),
                    "hard_gate_results": {"travel_time": allowed},
                    "selected": pair in selected,
                    "rejection_reason": "" if allowed else "travel_time_outlier",
                })
        bindings = {}
        for item in operations:
            if item["operation"] != "match":
                continue
            source = item["upstream"]
            target = item["downstream"]
            source["matched_at"] = float(target["first_seen_at"])
            source["travel_time_s"] = float(item["travel_time_s"])
            bindings[int(target["pass_id"])] = source
        self._matched_by_pass = bindings
        self._live_alignment_operations = operations
        self._alignment_candidates = candidates

    def _travel_window(self) -> tuple[float, float]:
        """Return the current arrival estimate and symmetric tolerance."""
        if len(self._travel_samples) >= 3:
            estimate = statistics.median(self._travel_samples)
            spread = statistics.pstdev(self._travel_samples)
            slack = max(TRAVEL_WINDOW_MIN_SLACK_SECONDS, spread * 3.0)
            return estimate, min(TRAVEL_WINDOW_MAX_SLACK_SECONDS, slack)
        return DEFAULT_TRAVEL_SECONDS, TRAVEL_WINDOW_MIN_SLACK_SECONDS

    @staticmethod
    def _unarrived_row(upstream: dict, status: str) -> dict:
        return {
            "upstream_session_id": upstream.get("session_id", ""),
            "upstream_pass_id": upstream.get("upstream_pass_id", ""),
            "downstream_pass_id": "",
            "lap_id": upstream.get("lap_id", ""),
            "sequence": upstream.get("sequence", ""),
            "upstream_color": upstream.get("color", ""),
            "downstream_color": "",
            "color_correct": "",
            "handoff_status": status,
            "travel_time_s": "",
            "upstream_confidence": upstream.get("confidence", ""),
            "downstream_confidence": "",
            "downstream_tracker_ids": "",
            "upstream_published_at": upstream.get("timestamp", ""),
            "downstream_mqtt_received_at": upstream.get("received_at", ""),
        }

    def _report_unarrived(self, upstream: dict, timed_out: bool) -> None:
        key = (
            str(upstream.get("session_id", "")),
            int(upstream.get("upstream_pass_id", 0)),
        )
        if key in self._reported_unarrived:
            return
        self._reported_unarrived.add(key)
        # Only an upstream plate that reached its final line can be considered
        # a pickup between cameras.  Early detections that vanished upstream
        # remain ordinary incomplete detections.
        status = (
            "removed_between_cameras"
            if timed_out and upstream.get("final_received")
            else "downstream_missing"
        )
        self.rows.append(self._unarrived_row(upstream, status))

    def _expire_overdue_pending(self, now: float) -> None:
        estimate, slack = self._travel_window()
        while self._pending:
            oldest = self._pending[0]
            expected = float(oldest["timestamp"]) + estimate
            if now <= expected + slack:
                break
            expired = self._pending.popleft()
            self._report_unarrived(expired, timed_out=True)

    def start_lap(self, lap_id: int) -> None:
        self._publish("lap_start", {"lap_id": int(lap_id)})

    def publish_locked_plate(self, state: dict) -> None:
        if self.role != "upstream" or not state.get("final_color"):
            return
        sequence = self._next_sequence
        self._next_sequence += 1
        ok = self._publish(
            "plate_locked",
            {
                "lap_id": int(state["lap_id"]),
                "upstream_pass_id": int(state["pass_id"]),
                "sequence": sequence,
                "color": str(state["final_color"]),
                "confidence": state.get("final_confidence_median"),
                "observations": int(state.get("final_observations", 0)),
            },
        )
        print(
            f"UPSTREAM #{sequence}: P{int(state['pass_id'])} "
            f"color={state['final_color']} mqtt={'ok' if ok else 'failed'}"
        )

    def publish_detected_plate(
        self, state: dict, color: str, confidence: float | None, observations: int
    ) -> bool:
        """Publish a reversible upstream lifecycle as soon as one plate is credible."""
        if self.role != "upstream" or not color:
            return False
        if state.get("handoff_detected"):
            previous = str(state.get("handoff_color", ""))
            if previous != str(color):
                state["handoff_color"] = str(color)
                self._publish("plate_updated", {
                    "lap_id": int(state["lap_id"]),
                    "upstream_pass_id": int(state["pass_id"]),
                    "sequence": int(state["handoff_sequence"]),
                    "color": str(color),
                    "confidence": confidence,
                    "observations": int(observations),
                })
                self._publish_hc_event(
                    "update", int(state["handoff_sequence"]), str(color),
                    int(state["pass_id"]),
                    {
                        "handoff_id": (
                            f"{self.session_id}:{int(state['handoff_sequence'])}"
                        ),
                        "status": "confirmed",
                    },
                )
            return True
        sequence = self._next_sequence
        self._next_sequence += 1
        state["handoff_detected"] = True
        state["handoff_sequence"] = sequence
        state["handoff_color"] = str(color)
        ok = self._publish("plate_detected", {
            "lap_id": int(state["lap_id"]),
            "upstream_pass_id": int(state["pass_id"]),
            "sequence": sequence,
            "color": str(color),
            "confidence": confidence,
            "observations": int(observations),
        })
        print(
            f"UPSTREAM DETECTED #{sequence}: P{int(state['pass_id'])} "
            f"color={color} mqtt={'ok' if ok else 'failed'}"
        )
        self._publish_hc_event(
            "entry", sequence, str(color), int(state["pass_id"]),
            {
                "handoff_id": f"{self.session_id}:{sequence}",
                "status": "provisional",
                "color_source": "upstream_local",
            },
        )
        return ok

    def publish_final_plate(self, state: dict) -> None:
        """Finalize an already announced upstream ID after the exit line."""
        if self.role != "upstream" or not state.get("final_color"):
            return
        if not state.get("handoff_detected"):
            self.publish_detected_plate(
                state,
                str(state["final_color"]),
                state.get("final_confidence_median"),
                int(state.get("final_observations", 0)),
            )
        if state.get("handoff_final"):
            return
        state["handoff_final"] = True
        self._publish("plate_final", {
            "lap_id": int(state["lap_id"]),
            "upstream_pass_id": int(state["pass_id"]),
            "sequence": int(state["handoff_sequence"]),
            "color": str(state["final_color"]),
            "confidence": state.get("final_confidence_median"),
            "observations": int(state.get("final_observations", 0)),
        })
        self._publish_hc_event(
            "exit", int(state["handoff_sequence"]),
            str(state["final_color"]), int(state["pass_id"]),
            {
                "handoff_id": (
                    f"{self.session_id}:{int(state['handoff_sequence'])}"
                ),
                "status": "confirmed",
            },
        )

    def publish_removed_plate(self, state: dict) -> None:
        """Cancel an early ID that disappeared before crossing the exit line."""
        if (
            self.role != "upstream"
            or not state.get("handoff_detected")
            or state.get("handoff_final")
            or state.get("handoff_removed")
        ):
            return
        state["handoff_removed"] = True
        self._publish("plate_removed", {
            "lap_id": int(state["lap_id"]),
            "upstream_pass_id": int(state["pass_id"]),
            "sequence": int(state["handoff_sequence"]),
            "color": str(state.get("handoff_color", "")),
            "reason": "disappeared_before_exit",
        })
        self._publish_hc_event(
            "catch", int(state["handoff_sequence"]),
            str(state.get("handoff_color", "unknown")), int(state["pass_id"]),
            {"event_id": self._removal_event_id(state), "status": "confirmed"},
        )

    def _on_message(self, _client, _userdata, message) -> None:
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except Exception:
            return
        camera_id = str(payload.get("camera_id", ""))
        if not camera_id or camera_id == self.camera_id:
            return
        with self._lock:
            stats = self.stats.setdefault(camera_id, self._empty_stats())
            stats["messages_received"] += 1
            message_id = str(payload.get("message_id", ""))
            if message_id in self._seen_message_ids:
                stats["duplicate_messages"] += 1
                return
            self._seen_message_ids.add(message_id)
            kind = str(payload.get("kind", ""))
            now = time.time()
            if kind in {"heartbeat", "status"}:
                previous = self._peer_last_heartbeat.get(camera_id)
                if previous is not None and now - previous > HEARTBEAT_MISSING_SECONDS:
                    stats["heartbeat_gaps"] += 1
                self._peer_last_heartbeat[camera_id] = now
                stats["last_heartbeat"] = payload.get("timestamp")
                remote_stats = payload.get("mqtt_stats")
                if isinstance(remote_stats, dict):
                    for key in (
                        "connect_count", "disconnect_count", "reconnect_count",
                        "publish_success", "publish_failure",
                    ):
                        if key in remote_stats:
                            stats[key] = remote_stats[key]
            semantic_id = str(
                payload.get("upstream_pass_id", payload.get("state", ""))
            )
            signature = (camera_id, f"{kind}:{semantic_id}")
            previous_event = self._last_event_signature.get(signature)
            if previous_event is not None and now - previous_event < EVENT_FLICKER_SECONDS:
                if kind in {
                    "lap_start", "plate_locked", "plate_detected",
                    "plate_final", "plate_removed", "status",
                }:
                    stats["event_flickers"] += 1
            self._last_event_signature[signature] = now
            received_event = {
                **payload,
                "received_at": now,
                "direction": "received",
                "publish_ok": "",
            }
            self.events.append(received_event)
            if (
                self.role == "downstream"
                and camera_id == self.peer_camera_id
                and kind in {
                    "plate_detected", "plate_updated", "plate_final",
                    "plate_removed", "plate_locked",
                }
            ):
                sequence = int(payload.get("sequence", 0))
                lifecycle_key = (
                    str(payload.get("session_id", "")),
                    int(payload.get("upstream_pass_id", 0)),
                )
                if kind in {"plate_detected", "plate_locked"}:
                    if lifecycle_key in self._upstream_lifecycle:
                        return
                    if self._pending and sequence <= int(self._pending[-1]["sequence"]):
                        stats["out_of_order_events"] += 1
                    entry = {
                        **payload,
                        "received_at": now,
                        "final_received": kind == "plate_locked",
                    }
                    self._upstream_lifecycle[lifecycle_key] = entry
                    self._pending.append(entry)
                elif lifecycle_key in self._upstream_lifecycle:
                    entry = self._upstream_lifecycle[lifecycle_key]
                    if kind in {"plate_updated", "plate_final"}:
                        entry.update({
                            "color": payload.get("color", entry.get("color", "")),
                            "confidence": payload.get(
                                "confidence", entry.get("confidence", "")
                            ),
                            "observations": payload.get(
                                "observations", entry.get("observations", "")
                            ),
                            "final_received": kind == "plate_final"
                            or entry.get("final_received", False),
                            "final_timestamp": payload.get("timestamp", "")
                            if kind == "plate_final"
                            else entry.get("final_timestamp", ""),
                        })
                        if kind == "plate_final":
                            # The arrival window may expire slightly before
                            # the upstream plate reaches its final line.  Once
                            # final confirmation arrives, upgrade that timed-
                            # out row from generic missing to pickup evidence.
                            for result in self.rows:
                                if (
                                    str(result.get("upstream_session_id", ""))
                                    == lifecycle_key[0]
                                    and int(result.get("upstream_pass_id") or 0)
                                    == lifecycle_key[1]
                                    and result.get("handoff_status")
                                    == "downstream_missing"
                                    and not result.get("downstream_pass_id")
                                ):
                                    result["handoff_status"] = (
                                        "removed_between_cameras"
                                    )
                        for result in self.rows:
                            if (
                                str(result.get("upstream_session_id", ""))
                                == lifecycle_key[0]
                                and int(result.get("upstream_pass_id") or 0)
                                == lifecycle_key[1]
                                and result.get("handoff_status") == "fifo_paired"
                            ):
                                result["upstream_color"] = entry.get("color", "")
                                downstream_color = str(
                                    result.get("downstream_color", "")
                                )
                                result["color_correct"] = int(
                                    bool(downstream_color)
                                    and downstream_color == entry.get("color", "")
                                )
                    elif kind == "plate_removed":
                        entry["removed"] = True
                        self._pending = deque(
                            item for item in self._pending if item is not entry
                        )
                        matched_locally = any(
                            binding is entry
                            for binding in self._matched_by_pass.values()
                        )
                        if not matched_locally:
                            key = (
                                str(entry.get("session_id", "")),
                                int(entry.get("upstream_pass_id", 0)),
                            )
                            if key not in self._reported_unarrived:
                                self._reported_unarrived.add(key)
                                self.rows.append(self._unarrived_row(
                                    entry, "removed_at_upstream"
                                ))
                        for local_pass_id, binding in list(
                            self._matched_by_pass.items()
                        ):
                            if binding is entry:
                                binding["removed"] = True
                if self.matching_mode == "dp":
                    self._recompute_live_alignment()

    def downstream_binding(self, pass_id: int) -> dict | None:
        with self._lock:
            binding = self._matched_by_pass.get(int(pass_id))
            return dict(binding) if binding is not None else None

    def publish_downstream_entry(
        self, state: dict, color: str, provisional: bool = False
    ) -> bool:
        """Publish one shared-ID entry, then update it after local confirmation."""
        if self.role != "downstream":
            return False
        operational_color = str(color or "unknown")
        if operational_color == "unknown":
            return False
        binding = self._matched_by_pass.get(int(state["pass_id"]))
        if binding is None:
            return False
        handoff_id = (
            f"{binding.get('session_id', '')}:{int(binding.get('sequence', 0))}"
        )
        if state.get("hc_entry_published"):
            if provisional or state.get("hc_color_confirmed"):
                return False
            state["hc_color_confirmed"] = True
            return self._publish_hc_event(
                "update", int(state["pass_id"]), operational_color,
                int(state["pass_id"]),
                {
                    "handoff_id": handoff_id,
                    "status": "confirmed",
                    "color_source": "downstream_local",
                },
            )
        state["hc_entry_published"] = True
        state["hc_color_confirmed"] = not provisional
        return self._publish_hc_event(
            "entry", int(state["pass_id"]), operational_color,
            int(state["pass_id"]),
            {
                "handoff_id": handoff_id,
                "status": "provisional" if provisional else "confirmed",
                "color_source": (
                    "upstream_reference" if provisional else "downstream_local"
                ),
            },
        )

    def match_downstream_entry(self, state: dict) -> str:
        """Register a local entry and return its current provisional binding."""
        if self.role != "downstream":
            return ""
        pass_id = int(state["pass_id"])
        with self._lock:
            self._downstream_lifecycle.setdefault(pass_id, {
                "pass_id": pass_id,
                "lap_id": int(state.get("lap_id", 0)),
                "first_seen_at": float(state.get("first_seen_unix", time.time())),
                "status": "detected",
                "color": "",
                "observations": 0,
                "tracker_ids": [],
            })
            if self.matching_mode == "dp":
                self._recompute_live_alignment()
                binding = self._matched_by_pass.get(pass_id)
                return str(binding.get("color", "")) if binding else ""
            if pass_id in self._matched_by_pass:
                binding = self._matched_by_pass[pass_id]
                color = str(binding.get("color", ""))
                self.publish_downstream_entry(state, color, provisional=True)
                return color
            now = time.time()
            self._expire_overdue_pending(now)
            if not self._pending:
                return ""
            estimate, slack = self._travel_window()
            expected = float(self._pending[0]["timestamp"]) + estimate
            if now < expected - slack:
                return ""
            upstream = self._pending.popleft()
            matched_at = now
            travel_time = matched_at - float(upstream["timestamp"])
            self._travel_samples.append(travel_time)
            upstream["matched_at"] = matched_at
            upstream["travel_time_s"] = travel_time
            binding = upstream
            self._matched_by_pass[pass_id] = binding
            self._publish("plate_claimed", {
                "upstream_session_id": upstream.get("session_id", ""),
                "upstream_pass_id": upstream.get("upstream_pass_id", ""),
                "sequence": upstream.get("sequence", ""),
                "downstream_pass_id": pass_id,
                "downstream_first_seen_at": state.get("first_seen_unix", ""),
            })
            color = str(upstream.get("color", ""))
            self.publish_downstream_entry(state, color, provisional=True)
            return color

    def finalize_downstream(self, state: dict) -> None:
        if self.role != "downstream":
            return
        pass_id = int(state["pass_id"])
        with self._lock:
            self.publish_downstream_entry(
                state, str(state.get("final_color") or "unknown")
            )
            if not state.get("hc_exit_published"):
                state["hc_exit_published"] = True
                self._publish_hc_event(
                    "exit", pass_id,
                    str(state.get("final_color") or "unknown"), pass_id,
                )
            self._downstream_lifecycle[pass_id] = {
                "pass_id": pass_id,
                "lap_id": int(state.get("lap_id", 0)),
                "first_seen_at": float(state.get("first_seen_unix", time.time())),
                "status": "completed",
                "color": str(state.get("final_color") or ""),
                "observations": int(state.get("final_observations", 0)),
                "confidence": state.get("final_confidence_median"),
                "tracker_ids": sorted(state.get("tracker_ids", set())),
            }
            if self.matching_mode == "dp":
                self._recompute_live_alignment()
            upstream = self._matched_by_pass.get(pass_id)
            if upstream is not None and upstream.get("removed"):
                self.rows.append({
                    "upstream_session_id": upstream.get("session_id", ""),
                    "upstream_pass_id": upstream.get("upstream_pass_id", ""),
                    "downstream_pass_id": pass_id,
                    "lap_id": upstream.get("lap_id", state["lap_id"]),
                    "sequence": upstream.get("sequence", ""),
                    "upstream_color": upstream.get("color", ""),
                    "downstream_color": state.get("final_color") or "",
                    "color_correct": "",
                    "handoff_status": "upstream_removed",
                    "travel_time_s": "",
                    "upstream_confidence": upstream.get("confidence", ""),
                    "downstream_confidence": state.get("final_confidence_median"),
                    "downstream_tracker_ids": " ".join(
                        str(value) for value in sorted(state.get("tracker_ids", set()))
                    ),
                })
                return
            if upstream is None:
                result_row = {
                    "upstream_session_id": "",
                    "upstream_pass_id": "",
                    "downstream_pass_id": pass_id,
                    "lap_id": int(state["lap_id"]),
                    "sequence": "",
                    "upstream_color": "",
                    "downstream_color": state.get("final_color") or "",
                    "color_correct": "",
                    "handoff_status": "downstream_extra",
                    "travel_time_s": "",
                    "upstream_confidence": "",
                    "downstream_confidence": state.get("final_confidence_median"),
                    "downstream_tracker_ids": " ".join(
                        str(value) for value in sorted(state.get("tracker_ids", set()))
                    ),
                }
                self.rows.append(result_row)
                self._publish(
                    "downstream_result",
                    {
                        "status": "downstream_extra",
                        "downstream_pass_id": pass_id,
                        "downstream_color": state.get("final_color") or "",
                    },
                )
                return
            upstream_color = str(upstream.get("color", ""))
            downstream_color = str(state.get("final_color") or "")
            result_row = {
                "upstream_session_id": upstream.get("session_id", ""),
                "upstream_pass_id": upstream.get("upstream_pass_id", ""),
                "downstream_pass_id": pass_id,
                "lap_id": upstream.get("lap_id", state["lap_id"]),
                "sequence": upstream.get("sequence", ""),
                "upstream_color": upstream_color,
                "downstream_color": downstream_color,
                "color_correct": int(bool(downstream_color) and upstream_color == downstream_color),
                "handoff_status": "fifo_paired",
                "travel_time_s": round(float(upstream["travel_time_s"]), 4),
                "upstream_confidence": upstream.get("confidence", ""),
                "downstream_confidence": state.get("final_confidence_median"),
                "downstream_tracker_ids": " ".join(
                    str(value) for value in sorted(state.get("tracker_ids", set()))
                ),
                "upstream_published_at": upstream.get("timestamp", ""),
                "downstream_mqtt_received_at": upstream.get("received_at", ""),
                "mqtt_delay_ms": round(
                    (float(upstream.get("received_at", upstream["timestamp"]))
                     - float(upstream["timestamp"])) * 1000.0,
                    3,
                ),
                "downstream_first_seen_at": state.get("first_seen_unix", ""),
                "downstream_locked_at": time.time(),
            }
            self.rows.append(result_row)
            self._publish(
                "downstream_result",
                {
                    "status": "fifo_paired",
                    "upstream_session_id": upstream.get("session_id", ""),
                    "upstream_pass_id": upstream.get("upstream_pass_id", ""),
                    "sequence": upstream.get("sequence", ""),
                    "downstream_pass_id": pass_id,
                    "downstream_color": downstream_color,
                    "color_match": int(
                        bool(downstream_color) and upstream_color == downstream_color
                    ),
                },
            )

    def finalize_downstream_removed(self, state: dict) -> None:
        """Record a plate that appeared at cam-2 but vanished before its exit."""
        if self.role != "downstream":
            return
        pass_id = int(state["pass_id"])
        with self._lock:
            if any(
                str(row.get("downstream_pass_id", "")) == str(pass_id)
                for row in self.rows
            ):
                return
            votes = Counter(state.get("local_color_votes") or {})
            local_color = votes.most_common(1)[0][0] if votes else ""
            self.publish_downstream_entry(state, local_color or "unknown")
            if not state.get("hc_catch_published"):
                state["hc_catch_published"] = True
                self._publish_hc_event(
                    "catch", pass_id, local_color or "unknown", pass_id,
                    {"event_id": self._removal_event_id(state),
                     "status": "confirmed"},
                )
            self._downstream_lifecycle[pass_id] = {
                "pass_id": pass_id,
                "lap_id": int(state.get("lap_id", 0)),
                "first_seen_at": float(state.get("first_seen_unix", time.time())),
                "status": "removed_local",
                "color": local_color,
                "observations": int(sum(votes.values())),
                "tracker_ids": sorted(state.get("tracker_ids", set())),
            }
            if self.matching_mode == "dp":
                self._recompute_live_alignment()
            upstream = self._matched_by_pass.get(pass_id)
            row = {
                "upstream_session_id": upstream.get("session_id", "") if upstream else "",
                "upstream_pass_id": upstream.get("upstream_pass_id", "") if upstream else "",
                "downstream_pass_id": pass_id,
                "lap_id": upstream.get("lap_id", state.get("lap_id", "")) if upstream else state.get("lap_id", ""),
                "sequence": upstream.get("sequence", "") if upstream else "",
                "upstream_color": upstream.get("color", "") if upstream else "",
                "downstream_color": local_color,
                "color_correct": "",
                "handoff_status": "removed_at_downstream",
                "travel_time_s": round(float(upstream.get("travel_time_s", 0.0)), 4) if upstream else "",
                "upstream_confidence": upstream.get("confidence", "") if upstream else "",
                "downstream_confidence": "",
                "downstream_tracker_ids": " ".join(
                    str(value) for value in sorted(state.get("tracker_ids", set()))
                ),
                "upstream_published_at": upstream.get("timestamp", "") if upstream else "",
                "downstream_mqtt_received_at": upstream.get("received_at", "") if upstream else "",
                "downstream_first_seen_at": state.get("first_seen_unix", ""),
                "downstream_locked_at": "",
            }
            self.rows.append(row)
            self._publish("downstream_result", {
                "status": "removed_at_downstream",
                "upstream_session_id": row["upstream_session_id"],
                "upstream_pass_id": row["upstream_pass_id"],
                "sequence": row["sequence"],
                "downstream_pass_id": pass_id,
                "downstream_color": local_color,
            })

    def display_lines(self) -> list[str]:
        peer = self.stats.get(self.peer_camera_id, {})
        return [
            f"handoff={self.role} mqtt={'up' if self._connected else 'down'}",
            f"pending={len(self._pending)} result_rows={len(self.rows)} "
            f"peer_flicker={peer.get('event_flickers', 0)}",
        ]

    @staticmethod
    def _public_stats(stats: dict) -> dict:
        return {key: value for key, value in stats.items() if not key.startswith("_")}

    def _authoritative_alignment(self) -> tuple[dict, list[dict]]:
        """Build the final non-greedy result from complete local lifecycles."""
        upstream_all = sorted(
            self._upstream_lifecycle.values(),
            key=lambda item: int(item.get("sequence", 0)),
        )
        upstream_removed = [item for item in upstream_all if item.get("removed")]
        upstream_final = [
            {
                **item,
                "detected_at": float(item.get("timestamp", 0.0)),
            }
            for item in upstream_all
            if item.get("final_received") and not item.get("removed")
        ]
        downstream_all = sorted(
            self._downstream_lifecycle.values(),
            key=lambda item: (float(item.get("first_seen_at", 0.0)), int(item["pass_id"])),
        )
        uncertain = [
            item for item in downstream_all
            if item.get("status") == "removed_local"
            and int(item.get("observations", 0)) < 2
        ]
        downstream_qualified = [item for item in downstream_all if item not in uncertain]
        operations = align_monotonic_events(
            upstream_final, downstream_qualified, DEFAULT_TRAVEL_SECONDS
        )
        rows: list[dict] = []
        for item in upstream_removed:
            rows.append({
                "status": "removed_at_upstream",
                "sequence": item.get("sequence", ""),
                "upstream_pass_id": item.get("upstream_pass_id", ""),
                "downstream_pass_id": "",
                "upstream_color": item.get("color", ""),
                "downstream_color": "",
                "travel_time_s": "",
            })
        for operation in operations:
            kind = operation["operation"]
            upstream = operation.get("upstream")
            downstream = operation.get("downstream")
            if kind == "match":
                status = (
                    "removed_at_downstream"
                    if downstream.get("status") == "removed_local"
                    else "matched"
                )
            elif kind == "skip_upstream":
                status = "removed_between_cameras"
            elif downstream.get("status") == "removed_local":
                status = "uncertain_downstream_unmatched"
            else:
                status = "downstream_extra"
            rows.append({
                "status": status,
                "sequence": upstream.get("sequence", "") if upstream else "",
                "upstream_pass_id": upstream.get("upstream_pass_id", "") if upstream else "",
                "downstream_pass_id": downstream.get("pass_id", "") if downstream else "",
                "upstream_color": upstream.get("color", "") if upstream else "",
                "downstream_color": downstream.get("color", "") if downstream else "",
                "travel_time_s": operation.get("travel_time_s", ""),
                "downstream_observations": downstream.get("observations", "") if downstream else "",
            })
        for item in uncertain:
            rows.append({
                "status": "uncertain_low_evidence",
                "sequence": "",
                "upstream_pass_id": "",
                "downstream_pass_id": item.get("pass_id", ""),
                "upstream_color": "",
                "downstream_color": item.get("color", ""),
                "travel_time_s": "",
                "downstream_observations": item.get("observations", 0),
            })
        counts = Counter(row["status"] for row in rows)
        summary = {
            "algorithm": "monotonic_dynamic_programming",
            "expected_travel_s": DEFAULT_TRAVEL_SECONDS,
            "matched": counts["matched"],
            "removed_at_upstream": counts["removed_at_upstream"],
            "removed_between_cameras": counts["removed_between_cameras"],
            "removed_at_downstream": counts["removed_at_downstream"],
            "downstream_extra": counts["downstream_extra"],
            "uncertain": counts["uncertain_low_evidence"]
            + counts["uncertain_downstream_unmatched"],
        }
        return summary, rows

    def _save_authoritative_alignment(self) -> None:
        if self.role != "downstream":
            return
        if self.matching_mode == "dp":
            self._recompute_live_alignment()
        summary, rows = self._authoritative_alignment()
        configuration = {
            "matching_mode": self.matching_mode,
            "expected_travel_s": DEFAULT_TRAVEL_SECONDS,
            "gap_cost": ALIGNMENT_GAP_COST,
            "max_time_error_s": ALIGNMENT_MAX_TIME_ERROR_SECONDS,
        }
        configuration_hash = hashlib.sha256(
            json.dumps(configuration, sort_keys=True).encode("utf-8")
        ).hexdigest()
        summary["configuration"] = configuration
        summary["configuration_hash"] = configuration_hash
        (self.output_directory / "sequence_alignment.json").write_text(
            json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.output_directory / "alignment_candidates.json").write_text(
            json.dumps(self._alignment_candidates, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        decisions = [
            {
                "decision_id": f"{self.session_id}:{index}",
                "run_id": self.session_id,
                "algorithm_name": "monotonic_dynamic_programming",
                "algorithm_version": 1,
                "configuration_hash": configuration_hash,
                "decision_type": row["status"],
                "location": {
                    "source_camera_id": self.peer_camera_id,
                    "target_camera_id": self.camera_id,
                },
                "source_event_refs": [row.get("upstream_pass_id")]
                if row.get("upstream_pass_id") else [],
                "target_event_refs": [row.get("downstream_pass_id")]
                if row.get("downstream_pass_id") else [],
                "reason_codes": [
                    "insufficient_evidence"
                    if row["status"].startswith("uncertain")
                    else "alignment_wrong_gap"
                    if row["status"] == "downstream_extra"
                    else row["status"]
                ],
                "confidence": None,
                "provisional_or_final": "final",
                "created_at": time.time(),
            }
            for index, row in enumerate(rows, start=1)
        ]
        (self.output_directory / "algorithm_decisions.json").write_text(
            json.dumps(decisions, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        color_names = {"orange": "O", "yellow": "Y", "light_green": "G"}
        lines = [
            "# 權威序列對齊結果", "",
            "此結果使用整批單調動態規劃；即時 MQTT FIFO 僅作暫定通知。", "",
            "| 狀態 | 數量 |", "|---|---:|",
            f"| 正常完成 | {summary['matched']} |",
            f"| 上游範圍拿走 | {summary['removed_at_upstream']} |",
            f"| 兩鏡頭之間拿走 | {summary['removed_between_cameras']} |",
            f"| 下游範圍拿走 | {summary['removed_at_downstream']} |",
            f"| 下游多餘事件 | {summary['downstream_extra']} |",
            f"| 不確定 | {summary['uncertain']} |", "",
            "| 上游序號 | 上游顏色 | 下游 Pass | 下游顏色 | 狀態 | 行程秒數 | 觀測數 |",
            "|---:|---|---:|---|---|---:|---:|",
        ]
        labels = {
            "matched": "正常完成",
            "removed_at_upstream": "上游拿走",
            "removed_between_cameras": "鏡頭間拿走",
            "removed_at_downstream": "下游拿走",
            "downstream_extra": "下游多餘",
            "uncertain_low_evidence": "低證據不確定",
            "uncertain_downstream_unmatched": "未配對不確定",
        }
        for row in rows:
            lines.append(
                f"| {row.get('sequence') or '-'} "
                f"| {color_names.get(row.get('upstream_color'), row.get('upstream_color') or '-')} "
                f"| {row.get('downstream_pass_id') or '-'} "
                f"| {color_names.get(row.get('downstream_color'), row.get('downstream_color') or '-')} "
                f"| {labels.get(row['status'], row['status'])} "
                f"| {row.get('travel_time_s') or '-'} "
                f"| {row.get('downstream_observations') or '-'} |"
            )
        (self.output_directory / "sequence_alignment_summary.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )

    def snapshot(self) -> None:
        """Persist a live snapshot without declaring pending plates missing."""
        with self._lock:
            self.save(finalize_pending=False)

    def save(self, finalize_pending: bool = True) -> None:
        self.output_directory.mkdir(parents=True, exist_ok=True)
        lifecycle_direction = "sent" if self.role == "upstream" else "received"
        lifecycle_events = [
            event for event in self.events
            if event.get("kind") in {
                "plate_detected", "plate_updated", "plate_final",
                "plate_removed", "plate_locked",
            }
            and event.get("direction") == lifecycle_direction
        ]
        lifecycle_by_key: dict[tuple[str, str], dict] = {}
        for event in lifecycle_events:
            key = (
                str(event.get("session_id", "")),
                str(event.get("upstream_pass_id", "")),
            )
            item = lifecycle_by_key.setdefault(key, {
                "session_id": key[0],
                "lap_id": event.get("lap_id", ""),
                "sequence": event.get("sequence", ""),
                "upstream_pass_id": key[1],
                "detected_color": "",
                "final_color": "",
                "status": "detected",
                "detected_at": "",
                "final_at": "",
                "removed_at": "",
            })
            kind = event.get("kind")
            if kind in {"plate_detected", "plate_locked"}:
                item["detected_color"] = event.get("color", "")
                item["detected_at"] = event.get("timestamp", "")
                if kind == "plate_locked":
                    item["final_color"] = event.get("color", "")
                    item["final_at"] = event.get("timestamp", "")
                    item["status"] = "final"
            elif kind == "plate_updated":
                item["detected_color"] = event.get("color", "")
            elif kind == "plate_final":
                item["final_color"] = event.get("color", "")
                item["final_at"] = event.get("timestamp", "")
                item["status"] = "final"
            elif kind == "plate_removed":
                item["removed_at"] = event.get("timestamp", "")
                item["status"] = "removed"
        lifecycle_fields = [
            "session_id", "lap_id", "sequence", "upstream_pass_id",
            "detected_color", "final_color", "status", "detected_at",
            "final_at", "removed_at",
        ]
        with (self.output_directory / "upstream_lifecycle.csv").open(
            "w", newline="", encoding="utf-8-sig"
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=lifecycle_fields)
            writer.writeheader()
            writer.writerows(lifecycle_by_key.values())

        inventory = [
            event for event in lifecycle_events
            if event.get("kind") in {"plate_final", "plate_locked"}
        ]
        inventory_fields = [
            "session_id", "lap_id", "sequence", "upstream_pass_id", "color",
            "confidence", "observations", "timestamp", "publish_ok",
        ]
        with (self.output_directory / "upstream_inventory.csv").open(
            "w", newline="", encoding="utf-8-sig"
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=inventory_fields)
            writer.writeheader()
            writer.writerows(
                {key: event.get(key, "") for key in inventory_fields}
                for event in inventory
            )
        if self.role == "upstream":
            inventory_lines = [
                "# 上游自動盤子清單", "",
                f"共確認並發布 {len(inventory)} 盤。", "",
                "| 順序 | Plate ID | 顏色 | Confidence | Observations | MQTT |",
                "|---:|---:|---|---:|---:|---|",
            ]
            for event in inventory:
                inventory_lines.append(
                    f"| {event.get('sequence')} | {event.get('upstream_pass_id')} "
                    f"| {event.get('color')} | {event.get('confidence')} "
                    f"| {event.get('observations')} "
                    f"| {'成功' if event.get('publish_ok') else '失敗'} |"
                )
            (self.output_directory / "upstream_inventory_summary.md").write_text(
                "\n".join(inventory_lines) + "\n", encoding="utf-8"
            )
        if (
            self.role == "downstream"
            and finalize_pending
            and self.matching_mode == "fifo"
        ):
            self._expire_overdue_pending(time.time())
            existing_missing = {
                (row.get("upstream_session_id"), row.get("upstream_pass_id"))
                for row in self.rows
            }
            for upstream in self._pending:
                key = (upstream.get("session_id"), upstream.get("upstream_pass_id"))
                if key in existing_missing:
                    continue
                self._report_unarrived(upstream, timed_out=False)
                existing_missing.add(key)
            finalized_downstream_ids = {
                int(row["downstream_pass_id"])
                for row in self.rows
                if str(row.get("downstream_pass_id", "")).strip()
            }
            for downstream_pass_id, upstream in self._matched_by_pass.items():
                if downstream_pass_id in finalized_downstream_ids:
                    continue
                key = (upstream.get("session_id"), upstream.get("upstream_pass_id"))
                if key in existing_missing:
                    continue
                self.rows.append({
                    "upstream_session_id": upstream.get("session_id", ""),
                    "upstream_pass_id": upstream.get("upstream_pass_id", ""),
                    "downstream_pass_id": downstream_pass_id,
                    "lap_id": upstream.get("lap_id", ""),
                    "sequence": upstream.get("sequence", ""),
                    "upstream_color": upstream.get("color", ""),
                    "downstream_color": "",
                    "color_correct": "",
                    "handoff_status": "downstream_missing",
                    "travel_time_s": round(float(upstream.get("travel_time_s", 0.0)), 4),
                    "upstream_confidence": upstream.get("confidence", ""),
                    "downstream_confidence": "",
                    "downstream_tracker_ids": "",
                })
                existing_missing.add(key)

        fields = [
            "upstream_session_id", "upstream_pass_id", "downstream_pass_id",
            "lap_id", "sequence", "upstream_color", "downstream_color",
            "color_correct", "handoff_status", "pairing_method",
            "identity_status", "travel_time_s",
            "upstream_confidence", "downstream_confidence",
            "downstream_tracker_ids",
            "upstream_published_at", "downstream_mqtt_received_at",
            "mqtt_delay_ms", "downstream_first_seen_at", "downstream_locked_at",
        ]
        for row in self.rows:
            if row.get("handoff_status") == "fifo_paired":
                row["pairing_method"] = "fifo"
                row["identity_status"] = "unverified"
            else:
                row.setdefault("pairing_method", "")
                row.setdefault("identity_status", "not_applicable")

        with (self.output_directory / "handoff_results.csv").open(
            "w", newline="", encoding="utf-8-sig"
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(self.rows)

        with (self.output_directory / "mqtt_events.jsonl").open(
            "w", encoding="utf-8"
        ) as handle:
            for event in self.events:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")

        received_keys = {
            (str(event.get("session_id", "")), str(event.get("upstream_pass_id", "")))
            for event in self.events
            if event.get("kind") in {"plate_detected", "plate_locked"}
            and event.get("direction") == "received"
        }
        sent = len(received_keys)
        fifo_paired = sum(
            row["handoff_status"] == "fifo_paired" for row in self.rows
        )
        correct = sum(row.get("color_correct") == 1 for row in self.rows)
        pickup_rows = [
            row for row in self.rows
            if row.get("handoff_status") == "removed_between_cameras"
        ]
        upstream_take_rows = [
            row for row in self.rows
            if row.get("handoff_status") == "removed_at_upstream"
        ]
        downstream_take_rows = [
            row for row in self.rows
            if row.get("handoff_status") == "removed_at_downstream"
        ]
        mqtt_stats = {
            key: self._public_stats(value) for key, value in self.stats.items()
        }
        mqtt_health = {
            "publish_failure": sum(
                int(item.get("publish_failure", 0))
                for item in mqtt_stats.values()
            ),
            "disconnect_count": sum(
                int(item.get("disconnect_count", 0))
                for item in mqtt_stats.values()
            ),
            "duplicate_messages": sum(
                int(item.get("duplicate_messages", 0))
                for item in mqtt_stats.values()
            ),
            "out_of_order_events": sum(
                int(item.get("out_of_order_events", 0))
                for item in mqtt_stats.values()
            ),
        }
        mqtt_health["healthy"] = all(
            value == 0 for value in mqtt_health.values()
        )
        summary = {
            "role": self.role,
            "camera_id": self.camera_id,
            "peer_camera_id": self.peer_camera_id,
            "upstream_received": sent,
            "fifo_paired": fifo_paired,
            "color_agreement_count": correct,
            "identity_verified_count": 0,
            "identity_unverified_count": fifo_paired,
            "downstream_missing": sum(row["handoff_status"] == "downstream_missing" for row in self.rows),
            "removed_between_cameras": len(pickup_rows),
            "removed_at_upstream": len(upstream_take_rows),
            "removed_at_downstream": len(downstream_take_rows),
            "removed_at_upstream_color_counts": dict(sorted(Counter(
                str(row.get("upstream_color", ""))
                for row in upstream_take_rows
                if str(row.get("upstream_color", "")).strip()
            ).items())),
            "removed_at_downstream_color_counts": dict(sorted(Counter(
                str(row.get("downstream_color", ""))
                for row in downstream_take_rows
                if str(row.get("downstream_color", "")).strip()
            ).items())),
            "removed_color_counts": dict(sorted(Counter(
                str(row.get("upstream_color", ""))
                for row in pickup_rows
                if str(row.get("upstream_color", "")).strip()
            ).items())),
            "removed_sequences": [
                int(row["sequence"])
                for row in sorted(
                    pickup_rows, key=lambda item: int(item.get("sequence") or 0)
                )
                if str(row.get("sequence", "")).strip()
            ],
            "downstream_extra": sum(row["handoff_status"] == "downstream_extra" for row in self.rows),
            "upstream_removed": sum(
                item.get("status") == "removed"
                for item in lifecycle_by_key.values()
            ),
            "fifo_pairing_rate": fifo_paired / sent if sent else None,
            "color_agreement_rate_among_fifo_pairs": (
                correct / fifo_paired if fifo_paired else None
            ),
            "identity_accuracy": None,
            "identity_accuracy_reason": "ground_truth_not_provided",
            "travel_time_median_s": statistics.median(self._travel_samples) if self._travel_samples else None,
            "downstream_local_color_counts": dict(
                sorted(
                    Counter(
                        str(row.get("downstream_color", ""))
                        for row in self.rows
                        if str(row.get("downstream_color", "")).strip()
                    ).items()
                )
            ),
            "mqtt_health": mqtt_health,
            "mqtt": mqtt_stats,
        }
        (self.output_directory / "handoff_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (self.output_directory / "handoff_analysis_summary.md").write_text(
            build_handoff_markdown(summary, self.rows), encoding="utf-8"
        )
        if self.matching_mode == "dp":
            self._save_authoritative_alignment()

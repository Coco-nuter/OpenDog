#!/usr/bin/env python3
"""
ActivityWatch 实时监控脚本
实时显示电脑当前的操作：活跃窗口、浏览器标签、键盘/鼠标活动等
"""

import requests
import time
import json
from datetime import datetime, timedelta
from collections import defaultdict

# ActivityWatch 默认配置
AW_SERVER = "http://127.0.0.1:5600"
API_BASE = "/api/0"

# 使用 Session 复用 TCP 连接，避免重复握手，这会大幅提高请求速度
session = requests.Session()


def get_buckets():
    """获取所有数据桶"""
    try:
        resp = session.get(f"{AW_SERVER}{API_BASE}/buckets/", timeout=5)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.ConnectionError:
        print("错误：无法连接到 ActivityWatch，请确保 aw-server 正在运行")
        return None
    except requests.exceptions.RequestException as e:
        print(f"错误：{e}")
        return None


def get_events(bucket_id, limit=10):
    """获取指定 bucket 的最新事件"""
    try:
        resp = session.get(
            f"{AW_SERVER}{API_BASE}/buckets/{bucket_id}/events",
            params={"limit": limit},
            timeout=5
        )
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException as e:
        print(f"获取事件失败：{e}")
        return []


def get_current_activity():
    """获取当前活动（活跃窗口、浏览器等）"""
    buckets = get_buckets()
    if not buckets:
        return None

    # 查找窗口和浏览器相关的 bucket
    window_bucket = None
    browser_bucket = None
    afk_bucket = None

    for bucket_id, info in buckets.items():
        name = info.get("name") or ""
        combined = (name + " " + bucket_id).lower()
        if "afk" in combined and "aw-watcher-afk" in combined:
            afk_bucket = bucket_id
        elif "currentwindow" in combined or "aw-watcher-window" in combined:
            window_bucket = bucket_id
        elif "browser" in combined or "aw-watcher-web" in combined:
            browser_bucket = bucket_id

    result = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "window": None,
        "browser": None,
        "afk_status": None,
    }

    # 获取当前窗口信息
    if window_bucket:
        events = get_events(window_bucket, limit=1)
        if events:
            event = events[0]
            data = event.get("data", {})
            result["window"] = {
                "title": data.get("title", "Unknown"),
                "app": data.get("app", "Unknown"),
                "class": data.get("class", data.get("classname", "Unknown")),
            }

    # 获取浏览器标签信息
    if browser_bucket:
        events = get_events(browser_bucket, limit=1)
        if events:
            event = events[0]
            data = event.get("data", {})
            result["browser"] = {
                "title": data.get("title", "Unknown"),
                "url": data.get("url", "Unknown"),
                "domain": data.get("domain", "Unknown"),
            }

    # 获取 AFK 状态
    if afk_bucket:
        events = get_events(afk_bucket, limit=1)
        if events:
            event = events[0]
            result["afk_status"] = event.get("data", {}).get("afk", "unknown")

    return result


def get_hourly_summary():
    """获取过去 1 小时的活动摘要"""
    buckets = get_buckets()
    if not buckets:
        return None

    window_bucket = None
    for bucket_id, info in buckets.items():
        name = info.get("name") or ""
        combined = (name + " " + bucket_id).lower()
        if "currentwindow" in combined or "aw-watcher-window" in combined:
            window_bucket = bucket_id
            break

    if not window_bucket:
        return None

    # 获取过去 1 小时的事件
    end = datetime.now()
    start = end - timedelta(hours=1)

    try:
        resp = session.get(
            f"{AW_SERVER}{API_BASE}/buckets/{window_bucket}/events",
            params={
                "start": start.isoformat(),
                "end": end.isoformat(),
                "limit": 1000
            },
            timeout=5
        )
        resp.raise_for_status()
        events = resp.json()

        # 统计应用使用时间
        app_time = defaultdict(int)
        for event in events:
            app = event.get("data", {}).get("app", "Unknown")
            # 计算持续时间（秒）
            duration = event.get("duration", 0)
            app_time[app] += duration

        # 排序
        sorted_apps = sorted(app_time.items(), key=lambda x: x[1], reverse=True)
        return sorted_apps[:10]

    except requests.exceptions.RequestException as e:
        print(f"获取摘要失败：{e}")
        return None


def clear_screen():
    """清屏"""
    print("\033[2J\033[H", end="")


def main():
    print("=" * 60)
    print("ActivityWatch 实时监控")
    print("=" * 60)
    print("按 Ctrl+C 退出\n")

    # 检查连接
    buckets = get_buckets()
    if buckets is None:
        print("\n提示：启动 ActivityWatch 的命令：")
        print("  - Windows: 运行 ActivityWatch 程序")
        print("  - Linux: aw-server")
        print("  - macOS: open /Applications/ActivityWatch.app")
        return

    print(f"已发现 {len(buckets)} 个数据桶")
    for bucket_id, info in buckets.items():
        print(f"  - {bucket_id}")
    print()

    last_update = None
    update_count = 0

    try:
        while True:
            activity = get_current_activity()
            if not activity:
                time.sleep(1)
                continue

            # 清屏（每 2 秒刷新一次，但只在内容变化时清屏）
            current_display = json.dumps(activity, sort_keys=True)
            if current_display != last_update:
                clear_screen()
                update_count += 1

            print("=" * 60)
            print(f"ActivityWatch 实时监控  |  刷新 #{update_count}")
            print(f"时间：{activity['timestamp']}")
            print("=" * 60)

            # 显示窗口信息
            print("\n[当前窗口]")
            if activity["window"]:
                win = activity["window"]
                print(f"  应用：{win['app']}")
                print(f"  标题：{win['title']}")
                print(f"  类名：{win['class']}")
            else:
                print("  (无数据)")

            # 显示浏览器信息
            print("\n[浏览器]")
            if activity["browser"]:
                browser = activity["browser"]
                print(f"  标题：{browser['title']}")
                print(f"  域名：{browser['domain']}")
                print(f"  URL: {browser['url']}")
            else:
                print("  (无数据或未安装浏览器扩展)")

            # 显示 AFK 状态
            print("\n[状态]")
            afk = activity["afk_status"]
            if afk is not None:
                status = "🔴 离开" if afk else "🟢 在线"
                print(f"  {status}")
            else:
                print("  (无数据)")

            # 每小时显示一次摘要
            if update_count % 30 == 0:  # 约每分钟（2 秒刷新一次）
                print("\n[过去 1 小时应用使用 Top 5]")
                summary = get_hourly_summary()
                if summary:
                    for i, (app, seconds) in enumerate(summary[:5], 1):
                        minutes = seconds / 60
                        print(f"  {i}. {app}: {minutes:.1f} 分钟")
                else:
                    print("  (无数据)")

            print("\n" + "=" * 60)
            print("按 Ctrl+C 退出")
            print("=" * 60)

            last_update = current_display
            time.sleep(1)

    except KeyboardInterrupt:
        print("\n\n已退出实时监控")


if __name__ == "__main__":
    main()

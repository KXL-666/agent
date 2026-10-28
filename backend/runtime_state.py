"""共享运行时状态。

拆分 main.py 时提取的全局运行时变量：Sprint 运行标志、当前任务句柄、
工作流图实例、场景节奏常量与系统资源统计缓存。

各 router 模块通过 ``import runtime_state`` 读写这些状态，避免循环依赖。
修改时直接赋值模块属性（如 ``runtime_state.running = True``），
不要再使用 ``global`` 声明。
"""
from __future__ import annotations

import asyncio
import time

import llm
from graph import build_sprint_graph

BASE_SCENE_PACE = llm.SCENE_PACE
FAST_SCENE_PACE = 0.12

# Sprint 是否正在运行（用于路由层判定能否发起新任务/修订等）
running: bool = False

# 当前 Sprint 对应的 asyncio.Task，用于手动取消
sprint_task: asyncio.Task | None = None

# 工作流图实例（LangGraph），全局复用
graph = build_sprint_graph()

# 系统资源统计缓存（网络字节数与时间戳，用于计算速率）
net_last: dict[str, float] = {"ts": time.time(), "bytes": 0}

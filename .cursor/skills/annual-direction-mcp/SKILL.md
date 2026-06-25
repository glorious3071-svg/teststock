---
name: annual-direction-mcp
description: >-
  通过 annual-direction MCP 服务完成年初定方向全流程：查状态、启动初稿、多轮追问、定稿、回测。
  当用户要做年初定方向、定战略配置、ETF年度配置、或通过对话敲定投资方向时触发。
---

# 年初定方向 MCP 工作流

用户在本对话框中提问时，**必须调用 `annual-direction` MCP 工具**完成交互，不要自行编造配置。

## 工具清单

| 工具 | 用途 |
|------|------|
| `annual_direction_status` | 查看某年会话是否存在、是否定稿 |
| `annual_direction_prepare` | 仅看数据就绪报告（可选） |
| `annual_direction_start` | 收集数据 + LLM 初稿 |
| `annual_direction_chat` | **传递用户的每一句追问/确认** |
| `annual_direction_backtest` | 定稿后用 100 万等资金回测 |

## 标准流程

1. 用户说「做 2011 年初定方向」→ 调用 `annual_direction_start(year=2011)`
2. 将返回的 `agent_reply` 和 `allocation_summary` 转述给用户
3. 用户追问（改仓位、换 ETF、质疑宏观）→ 原话传入 `annual_direction_chat(year=2011, message="...")`
4. 用户说「确认」「定稿」→ `annual_direction_chat` 传该消息
5. 返回 `finalized: true` 后，问用户是否回测 → `annual_direction_backtest(year=2011)`

## 注意

- 每轮用户输入都要通过 `annual_direction_chat` 转发，不要跳过 MCP 自己改配置
- 已定稿年份要重开 → `annual_direction_start(year=..., fresh=true)`
- 回测前必须 `finalized: true`，否则先引导用户定稿
- 回测模式年份自动禁用网络搜索，知识截止为上年 12-31

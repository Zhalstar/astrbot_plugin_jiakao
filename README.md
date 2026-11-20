<div align="center">

![:name](https://count.getloli.com/@astrbot_plugin_jiakao?name=astrbot_plugin_jiakao&theme=minecraft&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

# astrbot_plugin_jiakao

_✨ 驾考刷题插件 ✨_  

[![License](https://img.shields.io/badge/License-Proprietary-red.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-4.26%2B-orange.svg)](https://github.com/Soulter/AstrBot)
[![GitHub](https://img.shields.io/badge/作者-Zhalslar-blue)](https://github.com/Zhalslar)

</div>

## 🤝 介绍

驾考刷题插件，支持在群聊、私聊等聊天会话中进行科目一、科目四答题，可自动判题、显示解析、发送下一题并按会话记录排行榜

## 📦 安装

在astrbot的插件市场搜索astrbot_plugin_jiakao，点击安装即可

## ⌨️ 使用说明

### 命令表

| 命令 | 说明 |
| --- | --- |
| `驾考 [科一\|科四]` | 开始指定科目答题，省略科目时默认为科一 |
| `A` / `B` / `AC` / `A C` | 回答当前题目，判断题也支持“正确、错误、对、错” |
| `驾考下一题` | 跳过当前题目并发送下一题 |
| `驾考解析` | 公布当前题答案和解析，并发送下一题 |
| `驾考排行` | 显示当前会话答题排行榜 |
| `驾考结束` | 结束当前答题会话 |

每位参与者每题只能提交一次。首位答对者得 1 分，公布答案和解析后自动发送下一题；单题超时后公布答案并暂停，避免无人答题时持续刷屏。

QQ 官方机器人会为判断题和单选题显示回调按钮，可直接点击作答；多选题仍需发送答案字母组合，例如 `AC`。

### 配置表

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `question_timeout_seconds` | `90` | 单题超时时间，设置为 `0` 表示不限制时间 |
| `recent_question_count` | `50` | 每个会话避免重复的近期题目数量 |

### 示例图

## 👥 贡献指南

- 🌟 Star 这个项目！（点右上角的星星，感谢支持！）
- 🐛 提交 Issue 报告问题
- 💡 提出新功能建议
- 🔧 提交 Pull Request 改进代码

## 📌 注意事项

- 想第一时间得到反馈的可以来作者的插件反馈群（QQ群）：460973561（不点star不给进）
- 插件首次加载时会下载并缓存固定版本的[纯净驾考](https://github.com/nujgnoix/chun-jing-jia-kao)题库。题目配图会在使用时永久缓存到插件数据目录，并在当前题作答期间预取下一题配图
- 题库整理时间约为 2022 年前后，法规可能已经变化。本插件仅供日常练习，不构成考试或法律依据，实际考试请以公安部最新官方题库为准

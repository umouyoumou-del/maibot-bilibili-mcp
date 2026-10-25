# 只读 Bilibili MCP Server

为「麦麦刷B站插件」内置的 MCP 服务端，只暴露四个只读工具：

| Tool | 用途 |
| --- | --- |
| `bili_search` | 按关键词搜索视频 |
| `bili_video_info` | 读取视频详情、UP 主和播放数据 |
| `bili_subtitle` | 读取公开字幕或 AI 字幕原文 |
| `bili_comments` | 分页读取评论区，支持热度/最新排序和楼中楼回复 |

`bili_comments` 参数：

- `bvid`：视频 BV 号
- `num`：读取条数，最多 100
- `sort`：`hot`（热度）或 `new`（最新）
- `include_replies`：是否包含热评下的楼中楼回复

## 运行

```bash
python vendor/bilibili_mcp/mcp_server.py
```

使用 stdio 传输，由插件内的 MCP 客户端拉起，不需要手动启动。

## 可选登录凭证

匿名状态即可搜索、读取视频信息和公开评论；部分字幕和风控接口需要登录。
把以下 JSON 放到插件数据目录（或通过 `BILI_CREDENTIAL_FILE` 指定路径）即可：

```json
{
  "sessdata": "...",
  "bili_jct": "...",
  "buvid3": "...",
  "buvid4": "...",
  "dedeuserid": "...",
  "ac_time_value": "..."
}
```

凭证只在本机读取，本插件不会进行上传视频、发评论、私信等写操作。

## 来源与许可

基于 [AGI_Ananas/bilibili-mcp](https://github.com/adoresever/bilibili-mcp) 的 MIT 实现裁剪，
Copyright (c) 2026 AGI_Ananas。完整许可见同目录 `LICENSE`。

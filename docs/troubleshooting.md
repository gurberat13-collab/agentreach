# 常见问题排查

## Twitter/X: bird CLI 连接失败

**症状：** `bird search` 或其他命令返回错误

**原因：** bird CLI 需要 AUTH_TOKEN 和 CT0 环境变量才能访问 Twitter API。如果你的网络环境需要代理才能访问 x.com，需要配置代理。

**解决方案：**

### 方案 1：设置环境变量代理

```bash
export HTTP_PROXY="http://user:pass@host:port"
export HTTPS_PROXY="http://user:pass@host:port"
bird search "test" -n 1
```

### 方案 2：使用全局代理工具

让代理工具接管所有网络流量，这样 bird 的请求也会走代理：

```bash
# macOS — ClashX / Surge 开启"增强模式"
# Linux — proxychains 或 tun2socks
proxychains bird search "test" -n 1
```

### 方案 3：不用 bird，用 Exa 搜索替代

bird 不可用时，可以直接用 Exa 搜索 Twitter 内容：

```bash
mcporter call 'exa.web_search_exa(query: "site:x.com 搜索词", numResults: 5)'
```

### 方案 4：检查认证

```bash
bird check
```

> 如果返回 "Missing credentials"，需要设置 AUTH_TOKEN 和 CT0 环境变量。

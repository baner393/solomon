# SenseNova 4Key 轮换代理

三个 Node.js 代理进程（零依赖，仅 node 内置模块）：
-  — 端口 3456：通用池（视觉/文本），多 Key 轮换 + 排队 + 各模型独立冷却
-  — 端口 3458：flash-lite 文本池（含流级空闲超时/客户端断开放 Key 等生产加固）
-  — 备用池

## 配置

编辑每个文件顶部的  数组，填入你的 SenseNova API Key（https://console.sensecore.cn 申请）。

## 运行

node sensenova-proxy.js          # 3456
node sensenova-flashlite-proxy.js  # 3458

## 建议用 systemd user service 常驻

~/.config/systemd/user/sensenova-proxy.service:
  [Service]
  ExecStart=/usr/bin/node %h/solomon/scripts/sensenova-proxy/sensenova-proxy.js
  WorkingDirectory=%h/solomon/scripts/sensenova-proxy
  Restart=always
（flashlite 同理，另建一个 service 文件）
然后：systemctl --user enable --now sensenova-proxy sensenova-flashlite-proxy

健康检查：curl http://127.0.0.1:3456/health

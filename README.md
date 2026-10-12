# 🏡 家庭网络智能运维 Agent (Home Network Agent)

基于大语言模型（LLM）的 24/7 家庭网络智能管家。专为包含 **主路由 + 旁路由 (OpenWrt Nikki/Mihomo) + 专属代理 WiFi** 的家庭网络架构设计。

通过将闲置笔记本改造成家庭服务器，Agent 常驻运行在家庭局域网内，通过 **飞书机器人（WebSocket 长连接，免公网 IP）** 随时随地向你汇报网络健康状况、秒级测速切节点、透视连接设备与流量，并在出现断网或节点故障时自动自愈并告警。

---

## 🗺️ 网络拓扑与工作架构

```text
 ┌────────────────────────────────────────────────────────┐
 │           你的手机 / 飞书客户端 (5G / 任意网络)          │
 └───────────────────────────┬────────────────────────────┘
                             │ (飞书长连接，无需公网IP或内网穿透)
                             ▼
 ┌────────────────────────────────────────────────────────┐
 │   闲置笔记本 (Ubuntu 家庭服务器，连客厅主路由，24/7常驻)  │
 │                                                        │
 │   - LLM 自然语言推理大脑 (支持 DeepSeek / Qwen / Gemini) │
 │   - Watchdog 后台定时看门狗 (心跳检测、自愈、晨间日报)   │
 └─────────────┬──────────────────────────┬───────────────┘
               │                          │
   Nikki REST  │                          │ SSH / ubus
   (API :9090) │                          │ (端口 :22)
               ▼                          ▼
 ┌────────────────────────────────────────────────────────┐
 │        书房 OpenWrt 旁路由 (运行 Nikki / Mihomo)        │
 │                                                        │
 │   - 发射专属 VPN WiFi (手机/电视连接后走规则分流)      │
 │   - 策略组与节点池 (支持并发测速与热切换)              │
 │   - 活跃连接与设备流量统计                             │
 └────────────────────────────────────────────────────────┘
```

---

## 💻 闲置笔记本 (Ubuntu) 基础设置

安装 Ubuntu 22.04 / 24.04 LTS 后，进行以下两项优化：

### 1. 设置合盖不休眠（作为无头服务器运行）
在终端中编辑：
```bash
sudo nano /etc/systemd/logind.conf
```
找到以下配置行（若有 `#` 请去掉），修改为：
```ini
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
```
保存并退出后重启登录服务：
```bash
sudo systemctl restart systemd-logind
```

### 2. 绑定静态 IP
建议在客厅主路由后台，为笔记本的 MAC 地址分配一个固定的 IP（例如 `192.168.1.100`），保证内网通信地址恒定。

---

## 🛠️ 飞书机器人准备工作 (3 分钟搞定)

1. 打开 [飞书开放平台开发者后台](https://open.feishu.cn/app) 点击 **创建企业自建应用**。
2. 进入应用后台：
   - **添加机器人能力**：在左侧导航栏点击「添加应用能力」-> 选择「机器人」。
   - **开启权限**：进入「权限管理」，搜索并开启：
     - `im:message` (获取与发送单聊/群聊消息)
     - `im:message.p2p_msg:readonly` (读取单聊消息)
   - **配置事件监听（长连接模式）**：
     - 进入「事件与回调」->「事件配置」
     - 订阅方式选择 **WebSocket 长连接模式**（无需配置请求网址 URL！）
     - 添加事件：`im.message.receive_v1`（接收消息 v2.0）
3. 在「凭证与基础信息」中获取 **App ID** 和 **App Secret**。
4. 在「版本管理与发布」中点击 **申请发布**（自建应用自己审批即可生效）。

---

## ⚙️ 配置文件说明

复制配置模板：
```bash
cp config/config.example.yaml config/config.yaml
```

打开 `config/config.yaml` 填写你的参数：
```yaml
nikki:
  host: "192.168.1.2"          # 旁路由的局域网 IP
  port: 9090                   # Nikki (Mihomo) 控制端口
  secret: ""                   # 如果设置了密钥则填写

openwrt:
  host: "192.168.1.2"
  port: 22
  username: "root"
  password: "your_password"    # 旁路由 SSH 密码
  wifi_interface: "wlan0"      # 专属 WiFi 的网卡名 (通常为 wlan0)

llm:
  provider: "openai"
  base_url: "https://api.deepseek.com/v1"
  api_key: "sk-xxxxxxxx"
  model: "deepseek-chat"

feishu:
  app_id: "cli_xxxxxxxxxxxx"
  app_secret: "xxxxxxxxxxxx"
  admin_open_ids:
    - "ou_xxxxxxxxxxxxxxxxxxxx" # 你的飞书 OpenID (白名单保护)
```

---

## 🚀 启动与运行

### 方式一：本地一键联调自测
在运行主服务前，可以用自测脚本快速验证通信：
```bash
pip install -r requirements.txt
python run_test.py
```
若三项检查均输出 `✅`，说明与旁路由 API、SSH 通信完全正常！

### 方式二：Docker Compose 一键后台部署（推荐）
在笔记本服务器上直接运行：
```bash
docker compose up -d --build
```
查看运行日志：
```bash
docker compose logs -f
```

---

## 💬 手机飞书交互示例

在飞书直接给你的机器人发消息：

| 你的输入 | Agent 的行为与答复 |
| :--- | :--- |
| **“家里网卡了，帮我查查怎么回事”** | 自动执行分层 Ping 与 DNS 测试，汇报是光猫宽带断了、局域网不通还是当前节点超时。 |
| **“测一下现在的节点，哪个最快帮我换上”** | 并发探测所有节点延迟，列出排名前 3 的节点，自动执行切换并在飞书反馈。 |
| **“现在有哪些设备连在旁路由 WiFi 上？”** | 通过 SSH 查询 OpenWrt 连接列表，展示设备名、IP、MAC 和信号强度 (dBm)。 |
| **“谁在占网速？”** | 查询活跃连接，列出每台连网设备的当前连接数、下载/上传数据量以及正在访问的域名。 |
| **“把代理模式改成全局代理”** | 调用 Nikki API 将模式秒级切换为 `Global`。 |
| **“重启一下旁路由的代理服务”** | 通过 SSH 执行 `/etc/init.d/nikki restart` 并汇报状态。 |

---

## 🛡️ 自动化告警与自愈特性

- **断网告警**：国内基础网络中断时，第一时间向手机推送红色紧急卡片。
- **故障自愈**：当前节点连续 2 次超时，自动测速并切换到最佳备选节点，推送通知。
- **早间日报**：每天早晨 09:00 主动推送家庭网络体检报告。

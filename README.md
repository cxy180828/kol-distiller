# KOL Distiller

自动蒸馏加密货币KOL的交易思路，生成AI Agent，让他们帮你分析标的。

## 它能做什么

1. **自动抓取** — 输入KOL的Twitter handle，自动拉取历史推文
2. **AI分类** — 用大模型过滤噪音，只保留交易相关内容
3. **蒸馏画像** — 提炼KOL的交易风格、分析框架、入场模式、风控习惯
4. **多Agent讨论** — 给一个标的（如BTC），多个KOL Agent各自分析，给出各自思路和汇总

## 快速开始

### 1. 环境准备

```bash
# Python 3.10+
python3 -m pip install -r requirements.txt
```

### 2. 配置

```bash
cp config.example.yaml config.yaml
```

编辑 `config.yaml`，填入：

- **LLM API**：你的中转站地址、Key、模型名
- **Twitter Cookie**：X小号的 auth_token 和 ct0

#### 获取Twitter Cookie

1. 用小号登录 x.com
2. F12打开开发者工具 → Application → Cookies → x.com
3. 复制 `auth_token` 和 `ct0` 的值

### 3. 使用

```bash
# 添加一个KOL（会自动抓取+分类+蒸馏，需要几分钟）
python main.py add @某KOL

# 查看已添加的KOL
python main.py list

# 让所有KOL讨论BTC
python main.py discuss BTC

# 指定几个KOL讨论ETH
python main.py discuss ETH @kol_a @kol_b

# 手动更新某个KOL（抓新推文+重新蒸馏）
python main.py update @某KOL

# 批量抓取所有KOL新推文（不蒸馏）
python main.py update-all

# 只重新蒸馏
python main.py distill @某KOL
python main.py distill-all

# 查看KOL状态
python main.py status @某KOL
```

### 4. 设置定时任务（可选）

```bash
chmod +x setup_cron.sh
./setup_cron.sh
```

安装后，每6小时自动执行：
- 增量抓取所有KOL新推文
- 分类标注
- 如果一周内交易推文多，自动触发重新蒸馏
- 每周日全量重新蒸馏

## 项目结构

```
kol-distiller/
├── main.py              # 主入口（所有命令）
├── config.yaml          # 你的配置（不提交git）
├── config.example.yaml  # 配置模板
├── requirements.txt     # Python依赖
├── setup_cron.sh        # cron安装脚本
├── src/
│   ├── config.py        # 配置管理
│   ├── scraper.py       # Twitter抓取
│   ├── llm_client.py    # LLM调用客户端
│   ├── classifier.py    # 推文分类
│   ├── distiller.py     # Profile蒸馏
│   ├── market_data.py   # Binance行情数据
│   └── discussion.py    # 多Agent讨论引擎
├── kols/                # KOL数据目录
│   └── 某KOL/
│       ├── profile.md       # 蒸馏出的交易画像（核心）
│       ├── tweets_raw.jsonl # 原始推文
│       ├── tweets_tagged.jsonl  # 分类后的推文
│       ├── meta.json        # 元数据
│       └── history/         # 历史profile版本
├── discussions/         # 讨论记录
└── logs/                # 日志
```

## 工作原理

### 蒸馏流程

```
推文抓取 → LLM逐条分类 → 过滤噪音 → LLM总结蒸馏 → Profile人格文件
                ↓
        trade_opinion（交易观点）
        market_analysis（市场分析）
        macro（宏观判断）
        review（复盘）
        noise（丢弃）
```

### 讨论流程

```
用户输入标的(BTC) → 自动拉取实时行情 → 各KOL Agent独立分析 → 汇总共识/分歧
                                              ↓
                                    每个Agent的System Prompt
                                    = 该KOL的Profile画像
```

## 配置说明

### LLM配置

支持任何OpenAI兼容格式的API（中转站、DeepSeek、Moonshot等）：

```yaml
llm:
  base_url: "https://你的地址/v1"
  api_key: "sk-xxx"
  model: "你的模型名"
```

### 温度参数

- `temperature_classify: 0.1` — 分类用低温度，要确定性
- `temperature_distill: 0.3` — 蒸馏用中低温度，要准确
- `temperature_discuss: 0.7` — 讨论用高温度，要有个性

## 注意事项

- Twitter Cookie会过期，如果抓取报错请更新cookie
- 首次添加KOL比较慢（500条推文分类需要几分钟）
- 蒸馏质量取决于KOL推文的信息密度——话多但有用的KOL效果最好
- 建议先添加2-3个风格差异大的KOL，讨论效果更好

## License

MIT

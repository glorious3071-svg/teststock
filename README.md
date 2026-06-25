# teststock

A 股 ETF 战略配置与回测项目：宏观数据入库、年初定方向 Agent、简单回测引擎。

## 环境

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-db.txt
cp .env.example .env   # 填入 Tushare / MySQL / LLM 配置
```

## MySQL（Podman）

```bash
podman run -d --name teststock-mysql -p 3306:3306 \
  -e MYSQL_DATABASE=teststock -e MYSQL_USER=teststock -e MYSQL_PASSWORD=teststock \
  -e MYSQL_ROOT_PASSWORD=teststock_root \
  -v teststock-mysql-data:/var/lib/mysql mysql:8.0
```

按 `sql/` 下 schema 建表后，运行 `scripts/import_*.py` 导入数据。

## 年初定方向 Agent

```bash
python scripts/run_annual_direction.py run 2012
```

MCP 服务（Cursor）：

```bash
python -m mcp_servers.annual_direction
```

## 回测

```bash
python scripts/run_annual_backtest.py 2011 --cash 1000000
```

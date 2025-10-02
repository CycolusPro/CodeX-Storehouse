# Starbrew Inventory Service

Starbrew Inventory Service 是一个面向星巴克这类餐饮连锁场景打造的轻量级库存管理系统。项目使用 **FastAPI** + **SQLite** 架构，既能快速在单体门店部署，也能随着业务发展迁移到云端数据库。代码模块化、类型清晰，便于维护与二次开发。

## 功能概览

- 🔐 **系统健康检查**：通过 `/health` 端点获取服务运行状态。
- 📦 **商品管理**：增删改查饮品配料、物料 SKU，支持补货阈值设置。
- 🏬 **仓储/门店管理**：维护仓库、门店、档口等多个库存地点。
- 🔁 **出入库流水**：记录每一次调拨、损耗、盘点调整，自动同步库存余额。
- 📊 **库存余额报表**：实时汇总各地点库存数量，可用于 BI 报表对接。
- 🚨 **低库存预警**：当库存低于补货阈值时主动标记，协助门店及时补货。

## 目录结构

```
├── LICENSE
├── README.md
├── pyproject.toml
└── src
    └── inventory_service
        ├── __init__.py
        ├── api.py              # FastAPI 路由定义
        ├── config.py           # 环境配置（.env）
        ├── crud.py             # 业务逻辑与数据库读写
        ├── database.py         # SQLAlchemy 异步引擎与会话
        ├── main.py             # 应用入口，提供 uvicorn 启动器
        ├── management.py       # 数据库初始化脚本
        ├── models.py           # ORM 模型定义
        └── schemas.py          # Pydantic 数据模型
```

## 快速开始

### 1. 创建虚拟环境并安装依赖

```bash
python -m venv .venv
source .venv/bin/activate  # Windows 使用 .venv\Scripts\activate
pip install -e .[dev]
```

### 2. 配置环境变量（可选）

复制 `.env.example` 为 `.env`，根据需要调整数据库地址、跨域策略等参数。

```bash
cp .env.example .env
```

### 3. 初始化数据库

```bash
starbrew-init-db
```

> 默认使用项目根目录下的 `inventory.db`（SQLite + aiosqlite）。生产环境可通过 `DATABASE_URL` 指向 MySQL、PostgreSQL 等数据库。

### 4. 启动服务

```bash
starbrew-inventory
```

服务启动后可通过 `http://localhost:8000/docs` 访问自动生成的 Swagger 文档进行调试。

## 核心接口说明

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| GET | `/health` | 服务健康检查 |
| POST | `/products` | 新增商品 |
| GET | `/products` | 商品列表 |
| PUT | `/products/{id}` | 更新商品信息 |
| DELETE | `/products/{id}` | 删除商品 |
| POST | `/locations` | 新增门店/仓库 |
| GET | `/inventory/balances` | 查询库存余额 |
| POST | `/inventory/adjustments` | 出入库调整（正数入库，负数出库） |
| GET | `/inventory/low-stock` | 低库存预警 |

更多字段说明请参考 `src/inventory_service/schemas.py` 中的 Pydantic 模型。

## 部署建议

- **容器化**：项目提供 `Dockerfile` 与 `docker-compose.yml` 示例，支持一键容器化部署。
- **数据库**：开发阶段使用 SQLite；生产可切换至托管 PostgreSQL/MySQL。仅需修改 `.env` 中的 `DATABASE_URL`。
- **监控与日志**：FastAPI 原生日志可配合 ELK/CloudWatch 等系统，实现集中式监控。
- **认证扩展**：项目默认开放访问，可根据业务需要在 API 层加入 JWT/OAuth2 认证。

## 测试

```bash
pytest
```

测试使用 `httpx.AsyncClient` 针对核心 API 行为编写端到端用例，确保库存增减逻辑正确。

## 许可证

该项目基于 [MIT License](LICENSE) 开源，欢迎在保留许可证的前提下自由使用与修改。

# 简易库存管理服务

这是一个基于 Flask 的极简库存管理程序，提供最基础的库存录入、入库和出库功能，并会自动记录最近的入库/出库时间。它可以部署在任意支持 Python 的服务器上，其他设备可以通过 HTTP 接口或网页表单来访问。现在应用内置了用户系统，所有页面和 API 都需要登录后才能使用。

## 功能特性

- 📦 通过弹窗快速新增或设置库存商品的数量与单位
- ⬆️ 入库：增加某个商品的数量
- ⬇️ 出库：减少某个商品的数量并防止出现负库存
- 🌐 基于 Bootstrap 5 打造的响应式控制面板，可直接在浏览器操作
- 🧭 仪表盘中的库存详情支持就地编辑：可直接修改数量、调整单位或删除 SKU
- 🕒 自动追踪每个商品最近的入库与出库时间，并在界面与 API 中展示
- 📊 首页提供统计卡片与出入库动态列表，快速了解库存概况
- 🧾 最近动态卡片会展示新增 SKU、入库/出库数量、盘点调整及删除记录（最新 5 条）
- 📥 支持通过 CSV 模板批量导入 SKU，完成后会显示导入摘要
- 📤 一键导出当前库存清单与完整历史动态为表格文件，方便盘点归档
- 🔐 基于会话的身份验证与角色权限控制，确保敏感操作仅对授权用户开放
- 🔗 RESTful API，便于被 App、快捷指令等其他平台集成
- 💾 使用 JSON 文件持久化库存数据，并以 JSON Lines 日志形式记录全部操作历史

## 快速开始

1. **安装依赖**

   ```bash
   python -m venv .venv
   source .venv/bin/activate  # Windows 使用 .venv\\Scripts\\activate
   pip install -r requirements.txt
   ```

2. **启动服务**

   ```bash
   flask --app inventory_app.app run --host 0.0.0.0 --port 5000
   ```

   服务启动后，可在 `http://localhost:5000` 访问网页界面。

3. **首次登录**

   系统会自动创建一个默认超级管理员账号 `admin / admin`。请使用该账号登录后立即修改密码，并在“用户管理”页面按需创建其他管理员或员工账号。

> ℹ️ 登录界面位于 `/login`。在未登录状态下访问任意页面或 API 都会被重定向至此页面。

## 身份验证与权限

系统基于服务器端会话维持登录状态，每个账号绑定一个角色：

| 角色          | 权限概览                                                                 |
| ------------- | ------------------------------------------------------------------------ |
| `super_admin` | 具备全部权限，可管理库存、导入导出、查看统计、管理用户、清空历史记录。     |
| `admin`       | 可管理库存、导入导出、查看统计，但无法管理用户或清空历史记录。           |
| `staff`       | 仅能查看库存数据、执行出库操作及下载导出文件。                           |

> 🔁 会话有效期为 14 天，手动退出登录或清除浏览器 Cookie 会立即失效。

## API 说明

所有 API 都需要在登录状态下访问；不同端点还会根据角色限制操作。请求和响应默认使用 JSON。

### 获取会话（登录）

API 调用前需要先通过登录接口建立会话，可使用浏览器或命令行工具（如 `curl`）完成：

```bash
# Step 1: 登录并保存 Cookie
curl -c cookies.txt -X POST http://localhost:5000/login \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=admin&password=admin"

# Step 2: 使用保存的 Cookie 访问 API
curl -b cookies.txt http://localhost:5000/api/items
```

如果需要在脚本中调用 API，请在第一次登录后缓存 Cookie 文件或在应用内实现基于表单的登录流程。

### 获取库存列表

- `GET /api/items`
- **权限要求：** 任意登录用户（`staff` 及以上）
- 响应示例：

  ```json
  [
    {
      "name": "螺丝",
      "quantity": 100,
      "unit": "盒",
      "last_in": "2024-05-20T09:31:22.551274+00:00",
      "last_out": "2024-05-21T01:03:10.028741+00:00",
      "created_at": "2024-05-18T08:12:02.102932+00:00",
      "created_quantity": 100,
      "last_in_delta": 20,
      "last_out_delta": 5
    },
    {
      "name": "扳手",
      "quantity": 10,
      "unit": "把",
      "last_in": "2024-05-18T15:08:09.431220+00:00",
      "last_out": null,
      "created_at": "2024-05-17T10:02:44.112311+00:00",
      "created_quantity": 10,
      "last_in_delta": 10,
      "last_out_delta": null
    }
  ]
  ```

### 新增或设置库存

- `POST /api/items`
- **权限要求：** `admin` 或 `super_admin`
- 请求体：

  ```json
  {"name": "螺丝", "quantity": 50, "unit": "盒"}
  ```

### 更新现有库存（数量/单位）

- `PUT /api/items/<name>`
- **权限要求：** `admin` 或 `super_admin`
- 请求体：

  ```json
  {"quantity": 60, "unit": "箱"}
  ```

  当目标 SKU 不存在时返回 404。

### 入库

- `POST /api/items/<name>/in`
- **权限要求：** `admin` 或 `super_admin`
- 请求体：

  ```json
  {"quantity": 25}
  ```

### 出库

- `POST /api/items/<name>/out`
- **权限要求：** 任意登录用户（`staff` 及以上）
- 请求体：

  ```json
  {"quantity": 10}
  ```

  如果出库数量超过当前库存，将返回错误提示。

### 删除库存

- `DELETE /api/items/<name>`
- **权限要求：** `admin` 或 `super_admin`
- 删除后会在操作日志中记录一条删除事件，便于追溯。

### 批量导入库存

- `POST /api/items/import`
- **权限要求：** `admin` 或 `super_admin`
- 支持两种请求方式：
  - `multipart/form-data` 上传 CSV 文件（字段包含 `name`, `quantity`, `unit`）。
  - 直接提交 JSON 数组：`[{"name": "咖啡豆", "quantity": 50, "unit": "袋"}]`
- 返回导入成功的条目及数量统计。对于网页端，页面右上角的“批量操作”菜单会调用 `/import` 表单端点进行上传，并将结果摘要显示在库存列表上方。

### 导出库存清单

- `GET /api/items/export`
- **权限要求：** 任意登录用户（`staff` 及以上）
- 返回 UTF-8 带 BOM 的 CSV 文件，包含 SKU、数量、单位、最近出入库等字段，可直接用于盘点。

### 下载导入模板

- `GET /api/items/template`
- **权限要求：** 任意登录用户（`staff` 及以上）
- 获取示例 CSV 模板，便于按格式填写批量导入数据。

### 查询操作历史

- `GET /api/history`
- **权限要求：** 任意登录用户（`staff` 及以上）
- 可选查询参数：`limit`（整数，限制返回条目数量）
- 响应示例：

  ```json
  [
    {
      "timestamp": "2024-05-22T10:10:12.102932+00:00",
      "action": "in",
      "name": "咖啡豆",
      "meta": {
        "delta": 5,
        "new_quantity": 20,
        "unit": "袋"
      }
    },
    {
      "timestamp": "2024-05-22T09:58:44.551274+00:00",
      "action": "create",
      "name": "咖啡豆",
      "meta": {
        "quantity": 15,
        "unit": "袋"
      }
    }
  ]
  ```

所有操作历史会写入 `inventory_data.history.jsonl`（或自定义路径）文件中，便于长久追踪与外部系统读取。

### 导出操作历史

- `GET /api/history/export`
- **权限要求：** 任意登录用户（`staff` 及以上）
- 以 CSV 形式输出完整历史记录，包含事件名称、动作、详情及原始元数据 JSON 字段，方便在 Excel 或其他工具中进行分析。

### 导出统计报表

- `GET /api/history/stats/export`
- **权限要求：** `admin` 或 `super_admin`
- 基于历史记录生成指定时间段的入库、出库及净变动统计，返回 CSV 文件便于进一步分析。

## 运行测试

```bash
pytest
```

## 部署提示

- 生产环境中请使用 WSGI 服务（如 Gunicorn 或 uWSGI）进行部署。
- 如需持久化数据，可将 `inventory_data.json` 文件放置在持久化存储目录，并在创建应用时指定路径：

  ```python
  from inventory_app import create_app

  app = create_app("/path/to/data/inventory_data.json")
  ```

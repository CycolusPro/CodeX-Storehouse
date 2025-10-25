# 简易库存管理服务

这是一个基于 Flask 构建的轻量级库存系统，既提供操作友好的网页控制台，也暴露完整的 REST 风格接口，帮助小型团队快速搭建“能记录、能追踪、能导出”的入出库流程。项目使用纯 JSON 文件保存库存、门店、分类以及日志，部署成本极低。

## 功能特性

- 🏬 **多门店与分类管理**：支持创建多个门店、为商品打上分类标签，并在控制台之间快速切换。
- 🚨 **库存阈值提醒**：SKU 可配置低库存阈值，仪表盘会自动高亮提醒。
- 📦 **灵活的库存操作**：支持新增/盘点、入库、出库、删除等动作，并自动记录操作人、变化数量、门店与分类信息。
- 📈 **统计分析面板**：内置图表数据源，可按日或月导出入/出库统计报表。
- 🔐 **三角色权限体系**：`super_admin`、`admin`、`staff` 三种角色覆盖日常场景，确保敏感操作仅授权可见。
- 🔄 **批量导入导出**：可使用 CSV 模板导入商品，也可导出 XLS 格式的当前库存、操作历史与统计报表。
- 🕒 **完整操作日志**：所有操作都会写入 JSON Lines 格式的历史文件，便于审计追溯。
- 🌐 **响应式界面**：前端基于 Bootstrap 5，桌面与移动端皆可轻松操作。

## 快速开始

1. **安装依赖**

   ```bash
   python -m venv .venv
   source .venv/bin/activate  # Windows 使用 .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **启动服务**

   ```bash
   flask --app inventory_app.app run --host 0.0.0.0 --port 5000
   ```

   浏览器访问 `http://localhost:5000` 进入控制台。

3. **首次登录**

   首次启动会自动创建超级管理员账号 `admin / admin`。请登录后立刻修改密码，并在“用户管理”页面创建日常使用账号。

> ℹ️ 登录界面位于 `/login`。未登录访问任何页面或 API 会被重定向到登录页。

## 身份验证与权限

系统基于服务器端会话维持登录状态，每个账号绑定一个角色：

| 角色          | 权限概览                                                                 |
| ------------- | ------------------------------------------------------------------------ |
| `super_admin` | 拥有全部权限：管理库存、门店、分类、导入导出、查看统计、维护用户及清理历史。 |
| `admin`       | 可管理库存、门店分类、导入导出、查看统计，但不能维护用户或清理历史。         |
| `staff`       | 仅能查看库存、执行出库与导出操作。                                       |

> 🔁 会话有效期 14 天，主动退出或清除浏览器 Cookie 会立即失效。

## 数据存储

默认情况下，库存数据保存在 `inventory_data.json`，操作日志写入 `inventory_data.history.jsonl`，用户数据位于 `users_data.json`。通过 `create_app(storage_path, user_storage_path)` 可以自定义存储路径。

## API 说明

所有 API 均要求已登录会话，不同端点还会基于角色做权限控制。除特殊说明，API 默认收发 JSON。

### 获取会话（登录）

使用表单登录并在 Cookie 中保留会话信息：

```bash
# Step 1: 登录并保存 Cookie
curl -c cookies.txt -X POST http://localhost:5000/login \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=admin&password=admin"

# Step 2: 使用会话访问 API
curl -b cookies.txt http://localhost:5000/api/items
```

也可以直接调用 JSON 接口完成登录与退出，响应会包含当前角色与权限信息：

- `POST /api/auth/login` —— 请求体：`{"username": "admin", "password": "admin"}`。
  - 成功后返回 `username`、`role` 以及 `permissions` 字段，并自动下发会话 Cookie。
- `POST /api/auth/logout` —— 退出当前会话，返回退出账号信息。
- `GET /api/auth/session` —— 查询登录状态，未登录返回 `{"authenticated": false}`。

> 📱 所有 `/api/` 接口现在同时支持 **HTTP Basic Auth**，可直接通过 `Authorization: Basic <base64(username:password)>` 头部访问，适合 iOS 快捷指令等无需管理 Cookie 的客户端。服务端会针对同一用户/路径节流访问日志，避免频繁记录。

### 门店与分类管理

- `POST /stores` —— **权限：** `super_admin`
  - JSON 请求体：`{"name": "华东仓"}`
  - 返回新门店的 `id`，后续可在查询与操作时通过 `store_id` 指定。
- `POST /stores/<store_id>/delete` —— **权限：** `super_admin`
  - 可选 `cascade=true`，同时删除门店下的所有库存。
- `POST /categories` —— **权限：** `admin` 及以上
  - JSON 请求体：`{"name": "耗材"}`，创建后会返回 `id`。
- `POST /categories/<category_id>/delete` —— **权限：** `admin` 及以上
  - 同样支持 `cascade=true`，删除分类并清空分类引用。

### 库存类接口

除非特别说明，以下端点都接受 `store_id`（查询参数或 JSON 字段）与 `category`（JSON 字段）来限定门店/分类。

- `GET /api/items` —— **权限：** 任意登录用户
  - 可选参数：`store_id`、`category_id`
  - 返回当前门店/分类下全部 SKU，包含阈值、最近出入库时间等字段。
- `POST /api/items` —— **权限：** `admin` 或 `super_admin`
  - 请求体示例：

    ```json
    {
      "name": "螺丝",
      "quantity": 50,
      "unit": "盒",
      "threshold": 10,
      "store_id": "default",
      "category": "hardware"
    }
    ```

  - 会根据 `name + store_id` 创建或覆盖 SKU，返回最新库存。
- `PUT /api/items/<name>` —— **权限：** `admin` 或 `super_admin`
  - 请求体需包含 `quantity`，可选 `unit`、`threshold`、`category`、`store_id`。
- `POST /api/items/<name>/in` —— **权限：** `admin` 或 `super_admin`
  - 请求体：`{"quantity": 25, "store_id": "default"}`。
- `POST /api/items/<name>/out` —— **权限：** 任意登录用户
  - 请求体：`{"quantity": 10, "store_id": "default"}`。若出库量超出库存将返回错误。
- `DELETE /api/items/<name>` —— **权限：** `admin` 或 `super_admin`
  - 支持通过查询参数 `store_id` 指定门店。

### 批量导入导出

- `POST /api/items/import` —— **权限：** `admin` 或 `super_admin`
  - 支持 `multipart/form-data` 上传 CSV 文件，或直接提交 JSON 数组（每项包含 `name`、`quantity`、`unit`，可选 `threshold` 与 `category`）。
  - 可在请求体或查询参数提供 `store_id`，未提供则使用当前选中门店。
  - 返回导入成功的 SKU 列表及数量。
- `GET /api/items/template` —— **权限：** 任意登录用户
  - 下载示例 CSV 模板（含名称、数量、单位、阈值提醒、库存分类字段）。
- `GET /api/items/export` —— **权限：** 任意登录用户
  - 可选 `store_id`，返回 XLS 表格，字段包含门店、分类、阈值与最近出入库信息。

### 操作历史与统计

- `GET /api/history` —— **权限：** 任意登录用户
  - 参数：`store_id`、`limit`。
  - 返回最新的操作记录（动作类型、时间、操作者、数量变化等）。
- `GET /api/history/export` —— **权限：** 任意登录用户
  - 参数：`store_id`。
  - 导出完整历史 XLS 表格，附带入/出库量、操作者、门店分类等维度。
- `GET /api/history/stats/export` —— **权限：** `admin` 或 `super_admin`
  - 参数：`store_id`、`mode=day|month`、`start=YYYY-MM-DD`、`end=YYYY-MM-DD`。
  - 返回指定时间范围的入库量、出库量与净变动统计。

### 快捷指令与自动化调用

为了方便 iOS/iPadOS 快捷指令及其他自动化脚本集成，应用新增了免会话的令牌认证与简化接口：

- `POST /api/auth/token`
  - JSON 请求体需包含 `username`、`password`，可选 `expires_in`（单位：秒，默认 3600，最大 30 天）。
  - 返回 `token`、`expires_at`、`expires_in` 以及当前用户角色信息。
  - 之后可在任意 API 请求头加入 `Authorization: Bearer <token>`，或通过查询参数 `api_token=<token>` 完成认证。

基于令牌认证，系统提供了一组针对快捷指令场景封装的端点：

- `GET /api/shortcuts/profile`
  - 返回当前用户信息、权限、全部门店与分类列表，方便在快捷指令中构建选择器。
- `GET /api/shortcuts/items/summary`
  - 查询参数：`name`（必填）、`store_id`（可选）。
  - 返回单个 SKU 的库存数量、单位、分类/门店名称、是否触发低库存等摘要。
- `POST /api/shortcuts/items/adjust`
  - JSON 请求体示例：

    ```json
    {
      "name": "测试零件",
      "action": "out",
      "quantity": 3,
      "store_id": "default"
    }
    ```

  - `action` 支持 `set`（重置数量）、`in`（入库）、`out`（出库）、`transfer`（门店调拨）。
  - `transfer` 额外需要 `target_store_id` 字段。
  - 所有成功响应均包含 `status="success"` 以及最新库存快照；错误响应会返回 `status="error"` 与具体错误码。

### 网页端辅助接口

- `POST /stores/select` —— **权限：** 登录用户
  - 更改当前会话选中的门店。
- `POST /import` —— **权限：** `admin` 或 `super_admin`
  - 表单上传 CSV，成功后在仪表盘显示导入摘要。

## 运行测试

```bash
pytest
```

## 部署提示

- 生产环境推荐使用 WSGI 服务（如 Gunicorn 或 uWSGI）托管应用。
- 可使用 `inventory_app.create_app("/path/to/inventory.json", "/path/to/users.json")` 指定自定义的持久化文件路径。

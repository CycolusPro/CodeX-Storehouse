# 简易库存管理服务

这是一个基于 Flask 的极简库存管理程序，提供最基础的库存录入、入库和出库功能，并会自动记录最近的入库/出库时间。它可以部署在任意支持 Python 的服务器上，其他设备可以通过 HTTP 接口或网页表单来访问。

## 功能特性

- 📦 新增或设置库存商品的数量与单位
- ⬆️ 入库：增加某个商品的数量
- ⬇️ 出库：减少某个商品的数量并防止出现负库存
- 🌐 基于 Bootstrap 5 打造的响应式控制面板，可直接在浏览器操作
- 🧭 仪表盘中的库存详情支持就地编辑：可直接修改数量、调整单位或删除 SKU
- 🕒 自动追踪每个商品最近的入库与出库时间，并在界面与 API 中展示
- 📊 首页提供统计卡片与出入库动态列表，快速了解库存概况
- 🧾 最近动态卡片会展示新增 SKU、入库与出库的数量明细
- 🔗 RESTful API，便于被 App、快捷指令等其他平台集成
- 💾 使用 JSON 文件持久化库存数据

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

## API 说明

所有请求均使用 JSON 作为输入输出格式。

### 获取库存列表

- `GET /api/items`
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
- 请求体：

  ```json
  {"name": "螺丝", "quantity": 50, "unit": "盒"}
  ```

### 更新现有库存（数量/单位）

- `PUT /api/items/<name>`
- 请求体：

  ```json
  {"quantity": 60, "unit": "箱"}
  ```

  当目标 SKU 不存在时返回 404。

### 入库

- `POST /api/items/<name>/in`
- 请求体：

  ```json
  {"quantity": 25}
  ```

### 出库

- `POST /api/items/<name>/out`
- 请求体：

  ```json
  {"quantity": 10}
  ```

  如果出库数量超过当前库存，将返回错误提示。

### 删除库存

- `DELETE /api/items/<name>`
- 删除后不再保留该 SKU 的历史记录。

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

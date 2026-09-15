# 闸门群开度推演工作台

纯标准库实现（Python 3.11+，无第三方依赖）：`http.server` 后端 + SQLite 持久化 + 浏览器原生页面。

## 运行

```bash
python3 -m gatebench.app [端口] [数据库文件]   # 默认 127.0.0.1:8077, gatebench.db
# 打开 http://127.0.0.1:8077
```

## 测试

```bash
python3 -m unittest tests.test_gatebench -v
```

## 模型与约束

- 状态：各门开度离散到 0.1 m 网格；泄量 `Q = Σ coefᵢ·eᵢ·√head`。
- 任何中间状态不得违反：
  1. **禁振开度带**（按水头区间生效）：带把每门开度范围分割为互不连通的区间，搜索前按起点钳制到所在连通区间，从机制上杜绝穿越禁振带；
  2. **总泄量爬升率**（m³/s/步）；
  3. **单门速率限制**（m/步）；
  4. **相邻门开度差**；
  5. **检修锁定**（锁定门开度固定）。
- 搜索：A\*，启发函数为按爬升率逼近目标泄量的步数下界；目标泄量落在可达区间之外时即时判定，无需搜索。
- **失败诊断**：按 检修锁定 → 禁振带 → 相邻开度差 → 爬升率 → 单门速率 的顺序逐一松弛约束重搜，首个松弛后可行的约束即"首个不可满足约束"；同时返回距目标泄量最近的可达状态。

## 协作与封定

- **版本冲突**：所有写操作携带 `base_version`，与 `plans.version` 不一致返回 `409 version_conflict`（后提交者冲突，前端提示刷新）。
- **锁定前缀**：锁定第 k 步后，重新推演从第 k 步的开度状态继续，0..k 步原样保留。
- **封定回放**：封定时对方案参数与全部步骤的规范化 JSON 取 SHA-256 存为摘要；已封定方案拒绝一切修改，可逐步回放，每次回放重算摘要与封定摘要比对（`digest_stable`）。

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/gates` | 闸门与禁振带 |
| POST | `/api/gates/{id}/maintenance` | 检修锁定切换 `{locked}` |
| GET/POST | `/api/plans` | 方案列表 / 新建 |
| GET | `/api/plans/{id}` | 详情（含步骤） |
| POST | `/api/plans/{id}/solve` | 推演 `{base_version}`，保留锁定前缀 |
| POST | `/api/plans/{id}/lock` | 锁定步骤 `{seq, base_version}` |
| POST | `/api/plans/{id}/seal` | 封定 `{base_version}` |
| GET | `/api/plans/{id}/replay/{seq}` | 逐步回放（含稳定摘要） |

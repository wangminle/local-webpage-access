# V1 验收样例夹具

本目录提供 `lwa` V1 验收所需的 6 个样例项目（WBS-27）。样例以 Python
`{相对路径: 内容}` 字典定义在 `samples.py`，由 `build_zip(name, dest)` 按需
打包成 zip，避免在仓库中提交二进制文件。

## 样例清单

| 名称 | 形态 | 预期识别 kind | 说明 |
| --- | --- | --- | --- |
| `static_html` | 纯静态 HTML + CSS + JS | `static` | 验证静态托管路径 |
| `vite_react` | Vite + React 纯前端 | `node` | 构建后静态托管（frontend-static） |
| `node_express` | Node/Express 后端，无 DB | `node` | 容器托管（backend-container） |
| `fastapi_sqlite` | FastAPI + SQLite | `python` | 容器托管 + data/ 挂载 |
| `build_failure` | 故意引用不存在模块 | `node` | 启动失败 → `failed` |
| `pending_unknown` | 只有文本文件 | `pending` | 无法识别，不自动部署 |

## 使用方法

```python
from tests.fixtures import build_zip, build_all, EXPECTED_KIND

# 打包单个样例
zp = build_zip("static_html", tmp_path / "demo.zip")

# 打包全部样例
zips = build_all(tmp_path / "out")

# 期望识别结果
print(EXPECTED_KIND["fastapi_sqlite"])  # → "python"
```

## 验收对应

| WBS-27 验收项 | 对应测试 |
| --- | --- |
| 四核心样例稳定复现识别路径 | `test_sample_detected_correctly` |
| 失败样例能触发 failed | `build_failure` + 生命周期测试 |
| pending 样例不会被错误部署 | `test_pending_sample_stays_pending` |
| pending 样例写风险提示 | `test_pending_sample_writes_risk_event` |

## 重新生成

样例源文件树内嵌在 `samples.py` 中。修改后直接运行测试即可重新验证；
无需手动打包或提交 zip。

---
name: lwa-import-folder
description: >-
  Import a local absolute directory into lwa by copying into the workspace (not
  in-place), or update an existing folder-source instance from the same linked
  path. Use when the user has a source tree on disk instead of a zip, wants
  folder sync / --from-dir, or must avoid running services inside the developer
  directory.
---

# lwa-import-folder

> 把**本机绝对路径**下的开发目录当作只读源，**复制**进 LWA 工作区后再托管（与 zip 同管线）。禁止在关联目录内就地 `npm` / `docker` / `caddy`。

## 何时触发

- 用户说「从文件夹导入 / 关联本机目录 / `--from-dir`」。
- 源码在磁盘目录里，尚未打 zip。
- 已有 `sourceKind=folder` 实例，需要「从源更新」。

## 红线（必须遵守）

1. **关联 ≠ 运行根**：Caddy root、compose bind、builtin 静态根、构建 cwd **不得**指向用户目录。
2. 只读复制源；LWA **不**往关联目录写运行产物。
3. 路径必须是**绝对路径**（拒绝 `./x`、`.` 等相对路径）。
4. 不要用本 skill 做 zip↔文件夹模式转换（归 IMP-048，未实现）。

## 导入（新建）

```bash
lwa import --from-dir /abs/path/to/my-site
lwa import --from-dir /abs/path/to/my-site --name "My App" --path-alias myapp
```

导入后：`sourceKind=folder`，`sourceDirPath` 为关联绝对路径；内容在 `apps/<id>/current/`。

## 更新

```bash
# 路径须与实例关联目录一致（不一致会 Exit 2，不会静默改用别的目录）
lwa import --from-dir /abs/path/to/my-site --update <instance-id>
```

- 内容指纹未变 → 跳过（「无需更新」/已跳过），不 rebuild、不重启。
- 有变更 → 再复制并走既有 update（data 策略对齐 zip / `--keep-data`）。
- 关联目录缺失/不可读 → 报错，**禁止**回退为挂载运行。
- 更换关联目录：先 `lwa remove`（按需）再对新路径重新 `--from-dir` 导入；不要指望 `--update` 换源。

## 与 zip skill 分工

| 场景 | Skill |
| --- | --- |
| 手里是 zip | [`lwa-import-zip`](../lwa-import-zip/SKILL.md) |
| 手里是本机目录 | **本 skill** |

zip 实例不能用 `--from-dir --update`；folder 实例日常更新用本路径，不要误走 zip `--update` 除非用户明确改用 zip（IMP-048 前无正式转换）。

## 管理页

管理页支持「本机文件夹」导入与「从源更新」；展示来源类型与关联路径。API：`POST /api/import-from-dir`、`POST /api/instances/{id}/update-from-dir`。

## 导入后启动

与 zip 相同：识别 → 需要时构建 → `lwa start <id>`。细节见其它 dockerize / build / gateway skills。

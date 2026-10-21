# LWA 真实应用 Eval 本地样本

本目录按 [LWA 真实应用跨系统 Eval 方案](../../design/3-plans/2026-10-12-LWA真实应用跨系统Eval方案.md) 准备 11 个项目、每个项目两个固定版本的源码，用于后续部署、更新及换源评测。本文保留源码准备时的版本冻结与完整性记录；后续评测状态见文末所关联的类型化结论报告。

## 本地目录约定

每个项目目录包含 `old/`、`new/` 两份完整仓库源码和 `old.tar.gz`、`new.tar.gz` 两份原始下载归档。`old` 是更新起点，`new` 是更新目标；它们不代表开发分支或可随时刷新的最新版。

所有源码来自官方 GitHub 仓库，先将版本标签解析为完整提交 SHA，再从 GitHub codeload 下载该提交归档。源码保留上游许可证、配置和锁文件，不包含 `.git` 历史。本次未安装依赖或生成构建产物、运行数据，上游已纳入版本管理的文件原样保留。Git 子模块和 Git LFS 内容若存在，需单独核实；归档不自动拉取它们。

根目录 `.gitignore` 忽略本目录下除本文外的全部内容；本目录仅将此 README 纳入版本管理。本地版本清单和配置说明统一保存在本文，不另提交源码清单或下载脚本。其他机器克隆 LWA 后需要按本文重新下载或复制样本。

`pyproject.toml` 的 pytest 默认参数排除 `tests/evals`，避免收集第三方仓库的测试与 `conftest.py`。Ruff 按 Git 忽略规则跳过这些源码。此目录不会自动加入 LWA 单元测试。

## 版本冻结清单

冻结日期：2026-10-13（Asia/Shanghai）。10 个项目使用非草稿、非预发布的相邻稳定 release；2048 没有 release，取 `js/game_manager.js` 最近两个历史提交，确保是有游戏代码变化的真实上游快照。2048 的提交较早，不表示仓库最近活动时间。所有版本均不以 `latest` 或浮动分支作为身份。

| 样本 | 层级 | 项目 / 官方仓库 | old | new | 评测导入位置（相对于 old 或 new） |
| --- | --- | --- | --- | --- | --- |
| A01 | 核心 | [2048](https://github.com/gabrielecirulli/2048) | [cf01ca7e8bf6](https://github.com/gabrielecirulli/2048/commit/cf01ca7e8bf670822c55b0a4b04cd5c2b0271b3e) | [3b86903e6538](https://github.com/gabrielecirulli/2048/commit/3b86903e65383e30ffc836b733dcaf094c33ff10) | `.` |
| A02 | 核心 | [it-tools](https://github.com/CorentinTh/it-tools) | [v2024.5.13-a0bc346](https://github.com/CorentinTh/it-tools/releases/tag/v2024.5.13-a0bc346) | [v2024.10.22-7ca5933](https://github.com/CorentinTh/it-tools/releases/tag/v2024.10.22-7ca5933) | `.` |
| A03 | 核心 | [astro](https://github.com/withastro/astro) | [astro@7.3.7](https://github.com/withastro/astro/releases/tag/astro%407.3.7) | [astro@7.3.8](https://github.com/withastro/astro/releases/tag/astro%407.3.8) | `examples/portfolio` |
| A04 | 核心 | [flask](https://github.com/pallets/flask) | [3.1.2](https://github.com/pallets/flask/releases/tag/3.1.2) | [3.1.3](https://github.com/pallets/flask/releases/tag/3.1.3) | `examples/tutorial` |
| A05 | 核心 | [uptime-kuma](https://github.com/louislam/uptime-kuma) | [2.5.5](https://github.com/louislam/uptime-kuma/releases/tag/2.5.5) | [2.5.6](https://github.com/louislam/uptime-kuma/releases/tag/2.5.6) | `.` |
| A06 | 核心 | [linkding](https://github.com/sissbruecker/linkding) | [v1.46.2](https://github.com/sissbruecker/linkding/releases/tag/v1.46.2) | [v1.47.0](https://github.com/sissbruecker/linkding/releases/tag/v1.47.0) | `.` |
| A07 | 探索 | [excalidraw](https://github.com/excalidraw/excalidraw) | [v0.18.0](https://github.com/excalidraw/excalidraw/releases/tag/v0.18.0) | [v0.18.1](https://github.com/excalidraw/excalidraw/releases/tag/v0.18.1) | `.` |
| A08 | 探索 | [homarr](https://github.com/homarr-labs/homarr) | [v2.4.0](https://github.com/homarr-labs/homarr/releases/tag/v2.4.0) | [v2.4.1](https://github.com/homarr-labs/homarr/releases/tag/v2.4.1) | `.` |
| A09 | 探索 | [hedgedoc](https://github.com/hedgedoc/hedgedoc) | [1.11.1](https://github.com/hedgedoc/hedgedoc/releases/tag/1.11.1) | [1.12.0](https://github.com/hedgedoc/hedgedoc/releases/tag/1.12.0) | `.` |
| A10 | 边界 | [full-stack-fastapi-template](https://github.com/fastapi/full-stack-fastapi-template) | [0.11.1](https://github.com/fastapi/full-stack-fastapi-template/releases/tag/0.11.1) | [0.12.0](https://github.com/fastapi/full-stack-fastapi-template/releases/tag/0.12.0) | `.` |
| A11 | 边界 | [memos](https://github.com/usememos/memos) | [v0.30.0](https://github.com/usememos/memos/releases/tag/v0.30.0) | [v0.31.0](https://github.com/usememos/memos/releases/tag/v0.31.0) | `.` |

Astro 与 Flask 下载的是完整框架仓库，执行评测时分别选上述样例子目录。Excalidraw、Homarr、FastAPI Template、Memos 保留完整 workspace / 前后端上下文，不把某个前端子目录误当成完整应用。

### 完整提交与归档校验

提交日期为上游 Git committer 时间（UTC）；SHA-256 是本地收到的原始 `.tar.gz` 字节校验值。归档地址统一为 `https://codeload.github.com/仓库所有者/仓库名/tar.gz/完整提交SHA`。

| 样本 | 本地归档 | 完整提交 SHA | 提交日期（UTC） | 归档 SHA-256 |
| --- | --- | --- | --- | --- |
| A01 | `2048/old.tar.gz` | [`cf01ca7e8bf670822c55b0a4b04cd5c2b0271b3e`](https://github.com/gabrielecirulli/2048/commit/cf01ca7e8bf670822c55b0a4b04cd5c2b0271b3e) | 2014-03-23T18:51:29Z | `b42aff3baa4c63c8ac541a5016f21cab39b049bcfb98fc16783ad9c15a347f01` |
| A01 | `2048/new.tar.gz` | [`3b86903e65383e30ffc836b733dcaf094c33ff10`](https://github.com/gabrielecirulli/2048/commit/3b86903e65383e30ffc836b733dcaf094c33ff10) | 2014-04-16T08:54:51Z | `ca4c787d5c291eb9d4ae86f753d6b172515ca97a236278611ad77962d0c9c0fd` |
| A02 | `it-tools/old.tar.gz` | [`e876d0360812eab1255e414102a44d65f7c1bfaf`](https://github.com/CorentinTh/it-tools/commit/e876d0360812eab1255e414102a44d65f7c1bfaf) | 2024-05-13T08:54:18Z | `9d1e74c7cf1da1f485bd3d573226ab7fae780830abbdc918a5dcfbb517ac2278` |
| A02 | `it-tools/new.tar.gz` | [`5732483fc24a6e6818839060bdf3cc7d9d324b9f`](https://github.com/CorentinTh/it-tools/commit/5732483fc24a6e6818839060bdf3cc7d9d324b9f) | 2024-10-22T08:25:36Z | `b79c6241dc58019bb8a053a713ab454578506dab7d44699fedc6d6544e443b77` |
| A03 | `astro/old.tar.gz` | [`25c19fa3ce8b5044efa246b7c2d1e50e8e8cffcf`](https://github.com/withastro/astro/commit/25c19fa3ce8b5044efa246b7c2d1e50e8e8cffcf) | 2026-10-07T21:33:22Z | `420fb4b97d4935aad46b46ac580a5f0f01acf193a1efdd6a1057e68816b3d9f7` |
| A03 | `astro/new.tar.gz` | [`3571e2d2e80a66764254fa9b7750704a150ca3f6`](https://github.com/withastro/astro/commit/3571e2d2e80a66764254fa9b7750704a150ca3f6) | 2026-10-08T15:35:14Z | `d07b36ddadef3b0822425efe84ad6ac47475aad53b6d21ef03d0b45d933c5c18` |
| A04 | `flask/old.tar.gz` | [`2c1b30d0503cfb064f1cb252e6614a06915a362a`](https://github.com/pallets/flask/commit/2c1b30d0503cfb064f1cb252e6614a06915a362a) | 2025-08-19T20:57:47Z | `41fb36e5e022870ff304336f7f6bc1e69b5927f448da2f62e2a15d9319b99d09` |
| A04 | `flask/new.tar.gz` | [`22d924701a6ae2e4cd01e9a15bbaf3946094af65`](https://github.com/pallets/flask/commit/22d924701a6ae2e4cd01e9a15bbaf3946094af65) | 2026-02-19T03:41:55Z | `f745d5ccfc1e5c4ccb7f543c501936c64b5ae922ef6153626b5e26ab799037cb` |
| A05 | `uptime-kuma/old.tar.gz` | [`c98982ac60eccb74cdab1d04c30e18057895ce87`](https://github.com/louislam/uptime-kuma/commit/c98982ac60eccb74cdab1d04c30e18057895ce87) | 2026-09-16T13:19:51Z | `5dac9e1fb9b74f80d8f90e20ff6048d320f7f492e4e2f6ec496027554e82d3a9` |
| A05 | `uptime-kuma/new.tar.gz` | [`7ca4a3debd76443d8452da95d4040200a1479af4`](https://github.com/louislam/uptime-kuma/commit/7ca4a3debd76443d8452da95d4040200a1479af4) | 2026-10-09T15:33:13Z | `e1fac7d7c0f593659cd1590ed09d2f9f5a04d3875332dcf3e04de0cbbfc220c1` |
| A06 | `linkding/old.tar.gz` | [`f279015767b734c8f9a17c402adaa68bcd9f8d90`](https://github.com/sissbruecker/linkding/commit/f279015767b734c8f9a17c402adaa68bcd9f8d90) | 2026-08-18T20:03:22Z | `fc9b13676c67d105df7d7bd44d2b36942e5e805b4ebd2f934856f931d0ee9da2` |
| A06 | `linkding/new.tar.gz` | [`24b5ad6cc9bde497b5d1b1e86aed5a2fb7d25c2f`](https://github.com/sissbruecker/linkding/commit/24b5ad6cc9bde497b5d1b1e86aed5a2fb7d25c2f) | 2026-09-13T16:01:08Z | `2aa9ba587e7be503f21541f959e9310462a0acc2a77a34ffb7f149d46f961626` |
| A07 | `excalidraw/old.tar.gz` | [`817d8c553c3389650f8b4503984a6d4a5d2f0c11`](https://github.com/excalidraw/excalidraw/commit/817d8c553c3389650f8b4503984a6d4a5d2f0c11) | 2025-03-11T12:44:10Z | `26ac0dcf147c706c7146bbc7b775cde0f289b312e2b9f4846dd80deb4c6af178` |
| A07 | `excalidraw/new.tar.gz` | [`a2ec2889babf7d2295469c6d90ebe77fae57df84`](https://github.com/excalidraw/excalidraw/commit/a2ec2889babf7d2295469c6d90ebe77fae57df84) | 2026-04-20T20:30:38Z | `621dfe535d6b65ee42fb4e4d9dc058cbf2e140319d942b03aaf7b115b2bdb642` |
| A08 | `homarr/old.tar.gz` | [`cd126e8df70a00df69070c4e5b37d17588f30308`](https://github.com/homarr-labs/homarr/commit/cd126e8df70a00df69070c4e5b37d17588f30308) | 2026-10-09T16:35:26Z | `b49c362332c61a63ac02edd15f726aaf5662186ec30334e662ecdd9750fce29e` |
| A08 | `homarr/new.tar.gz` | [`ebbd4876d25a51151444a009ab596e1154d62880`](https://github.com/homarr-labs/homarr/commit/ebbd4876d25a51151444a009ab596e1154d62880) | 2026-10-10T11:29:02Z | `2aaf8155b73bb5e3e34916d674d6b77cd9358a59448ff5b1d67da1bd00c4efb7` |
| A09 | `hedgedoc/old.tar.gz` | [`0dda149c7f528a7e98a34a8459b7afb23c90a05b`](https://github.com/hedgedoc/hedgedoc/commit/0dda149c7f528a7e98a34a8459b7afb23c90a05b) | 2026-07-24T19:39:28Z | `c912613937c92d4f16fbfa9b7fd1074eb252ba5dab53be8887009bbbac181bde` |
| A09 | `hedgedoc/new.tar.gz` | [`b9335040716278d029b5872e3825b92821d9cb70`](https://github.com/hedgedoc/hedgedoc/commit/b9335040716278d029b5872e3825b92821d9cb70) | 2026-08-21T21:40:50Z | `362d7501b34c23d9d1efd3393ea401f4e990dd6bd7c4a16638831b05e70c7ba0` |
| A10 | `full-stack-fastapi-template/old.tar.gz` | [`ddb3c34f1956c028bcb10985ce60c3c147c52d8c`](https://github.com/fastapi/full-stack-fastapi-template/commit/ddb3c34f1956c028bcb10985ce60c3c147c52d8c) | 2026-08-11T19:57:14Z | `cf321157a94a07865a3502fbf8edf9e82612297f1dc703f1c7e0cbf731bdbde4` |
| A10 | `full-stack-fastapi-template/new.tar.gz` | [`84c5a9e11a9e58262599e3d772f5386924e02ca7`](https://github.com/fastapi/full-stack-fastapi-template/commit/84c5a9e11a9e58262599e3d772f5386924e02ca7) | 2026-08-12T08:46:45Z | `f4b03eff0c3052a3802ee9d4bdc533e750bf229d7d6cb2115796e368da4a72cc` |
| A11 | `memos/old.tar.gz` | [`2036c1ffc1b0a1e1fa6a473738c2a5ef520df67f`](https://github.com/usememos/memos/commit/2036c1ffc1b0a1e1fa6a473738c2a5ef520df67f) | 2026-07-26T15:45:43Z | `088d7bc6ee2f421487ca47fc584dd5c8075b2aabdaa1833a19ea94972e44f26f` |
| A11 | `memos/new.tar.gz` | [`2b2192d4e153bd04f1d325b60fd880cf00d68b01`](https://github.com/usememos/memos/commit/2b2192d4e153bd04f1d325b60fd880cf00d68b01) | 2026-09-19T16:50:44Z | `75ffeb73f58aac5c21209d62e9d88e17b65cdd66e6986fa84443e7777fa29f88` |

### 本地占用与完整性

共 11 个项目目录、22 个源码目录、22 个原始归档。归档合计 246.2 MiB；源码普通文件合计 410.0 MiB、32,481 个文件（不含文件系统分配开销）。

| 样本 | old 文件数 | new 文件数 | 归档合计（MiB） |
| --- | --- | --- | --- |
| A01 / 2048 | 32 | 34 | 0.6 |
| A02 / it-tools | 480 | 498 | 1.2 |
| A03 / astro | 6961 | 6962 | 75.9 |
| A04 / flask | 234 | 235 | 1.4 |
| A05 / uptime-kuma | 794 | 795 | 4.3 |
| A06 / linkding | 439 | 442 | 6.1 |
| A07 / excalidraw | 1116 | 1116 | 67.2 |
| A08 / homarr | 4216 | 4199 | 61.7 |
| A09 / hedgedoc | 470 | 471 | 22.0 |
| A10 / full-stack-fastapi-template | 248 | 250 | 1.3 |
| A11 / memos | 1054 | 1435 | 4.5 |

## 本地配置与执行边界

样本位置统一写作 `<仓库根目录>/tests/evals`。各机器自行选择仓库路径；真实账号、主机名、地址和传输步骤仅记录在本地操作手册。

下面的运行时要求来自冻结源码中的配置文件；它们是后续配方的输入，尚未安装或实测。宿主机无需为了保存样本而安装全部运行时。

| 样本 | 冻结源码中的配置 | 后续评测注意事项 |
| --- | --- | --- |
| A01 / 2048 | 仓库根目录 `index.html`、`js/`、`style/`；无构建或依赖锁 | 两个提交的 `js/game_manager.js` 不同；得分持久化属于浏览器 localStorage。 |
| A02 / IT-Tools | 两版均有 `pnpm-lock.yaml`；new 的 `packageManager` 为 `pnpm@8.15.3`，old 未声明；根目录 `build` 脚本生成 `dist/` | old 的包管理器版本需结合锁文件冻结；依赖安装应遵守上游锁文件。 |
| A03 / Astro portfolio | `examples/portfolio/package.json`：Node `>=22.12.0`；old/new 分别依赖 `astro@^7.3.7` / `astro@^7.3.8`；构建入口 `astro build` | 样例无独立锁文件；仓库根 `pnpm-lock.yaml` 不等于独立样例锁。直接安装可能将两版解析到同一 Astro 版本，正式升级评测前必须在执行副本中精确冻结依赖并留存锁文件。 |
| A04 / Flaskr | `examples/tutorial/pyproject.toml` 两版应用版本均为 `1.0.0`，仅依赖未固定版本的 `flask`；框架根目录有 `uv.lock` | 必须在执行副本中分别绑定 Flask `3.1.2` / `3.1.3`，否则可能装成同版。`flaskr/__init__.py` 有真实变化，涉及 instance 目录创建；SQLite 初始化、生产 WSGI 启动与数据目录在执行配方中明确。 |
| A05 / Uptime Kuma | Node `>=20.4.0`、`package-lock.json`；`build` 使用 `config/vite.config.js`，后端入口 `server/server.js` | 源码归档不含预构建 `dist`；上游 `setup` 含 `git checkout`，不适用于无 `.git` 的快照，需使用源码构建路线。数据目录与实时连接单独验证。 |
| A06 / linkding | Python `>=3.13`；`pyproject.toml`、`uv.lock`、`package-lock.json`；后端 `manage.py`，前端构建根 `package.json` | 初始化、静态资源、任务进程按冻结版本说明配置；上游 Compose 数据路径 `/etc/linkding/data`。预构建镜像运行不等于源码部署通过。 |
| A07 / Excalidraw | Yarn `1.22.22`、`yarn.lock`；Node `18.0.0 - 22.x.x`；根 workspace 包含 `excalidraw-app` 与 `packages/*` | 以完整仓库为构建上下文，浏览器应用在 `excalidraw-app`；标签是上游发布身份，不能用 app 内恒定版本号代替实际构建身份。 |
| A08 / Homarr | Bun `1.4.2`（engines `>=1.4.2`）、Node `>=24.18.0`、`bun.lock`；根目录 Turbo workspace | 使用 `homarr-labs/homarr`；数据库、环境变量、加密配置和持久化须按两版说明冻结，当前只准备源码。 |
| A09 / HedgeDoc | Node `>=18`、Yarn `4.13.0`、`yarn.lock`、根 `package.json` | 先验证数据库与实时编辑链路；反代协议和持久化依赖不可省略。 |
| A10 / FastAPI Template | `backend/pyproject.toml`：Python `>=3.14,<4.0`；根 `uv.lock`、`bun.lock`，前端 `frontend/` | 作为完整 Compose 拓扑样本，需要 PostgreSQL 等依赖；不能只导入前端并宣布完整部署成功。 |
| A11 / Memos | `go.mod`：old 为 Go `1.26.2`、new 为 Go `1.27.0`；两版均有 `go.sum`；`web/package.json`：Node `>=24`、pnpm `11.0.1`，`web/pnpm-lock.yaml` | 完整应用包含 Go 后端；前端 release 脚本写入 `server/frontend/dist`。仅出现页面不等于服务可用。 |

以上版本对仅完成源码冻结，尚未验证上游支持的升级路径、数据库迁移和回退。两个快照存在不等于升级成功；正式评测仍须按方案记录部署、业务断言、重启、换源和升级结果。Flaskr 的应用元数据版本相同，Astro 样例依赖是范围，这两个情况尤其不能仅看版本字符串判定升级。

本清单记录 2026-10-13 的源码准备快照，不代表后续部署状态。评测使用独立工作区与测试数据；按系统分类的已执行结果及限制见 [类型化结论报告](../../design/2-achievement/2026-10-19-真实应用跨系统Eval类型化结论.md)。各机器的接入、运行配置与详细分析保留在本地检查点。

评测应从 `old/`、`new/` 复制出执行副本，保留冻结源和归档不变。构建产物、补充依赖锁、运行数据和结果放入各自已忽略的项目目录，例如 `项目/runs/H-MAC/`；源码修改与额外配置分别记录。未固定的依赖、镜像和外部服务在正式执行前继续冻结，不把本次下载当成方案里的完整环境冻结。

## 在其他机器恢复同一份样本

先从版本表取官方仓库名、完整提交 SHA、归档 SHA-256，设置目标项目与 old/new，再下载到对应目录。例如在 LWA 根目录恢复 A02 的 old：

```bash
mkdir -p tests/evals/it-tools
curl -fL --retry 3 --retry-all-errors \
  https://codeload.github.com/CorentinTh/it-tools/tar.gz/e876d0360812eab1255e414102a44d65f7c1bfaf \
  -o tests/evals/it-tools/old.tar.gz
```

用 Python 3.12+ 校验并解压（Linux 与 macOS 均可；不会覆盖已存在的源码目录）：

```python
import hashlib
import shutil
import tarfile
import tempfile
from pathlib import Path

archive = Path("tests/evals/it-tools/old.tar.gz")
destination = archive.parent / "old"
expected = "9d1e74c7cf1da1f485bd3d573226ab7fae780830abbdc918a5dcfbb517ac2278"
with archive.open("rb") as stream:
    actual = hashlib.file_digest(stream, "sha256").hexdigest()
if actual != expected:
    raise RuntimeError("归档 SHA-256 不匹配，先核实来源，不继续解压")
if destination.exists():
    raise FileExistsError(destination)
staging = Path(tempfile.mkdtemp(prefix=".extract-", dir=archive.parent))
try:
    with tarfile.open(archive, "r:gz") as bundle:
        roots = {member.name.split("/")[0] for member in bundle.getmembers()}
        if len(roots) != 1:
            raise RuntimeError("不是预期的单根目录归档")
        bundle.extractall(staging, filter="data")
    (staging / roots.pop()).rename(destination)
finally:
    shutil.rmtree(staging)
```

其他 21 份按同样方式替换仓库、提交、校验值和目的目录。GitHub 将来重新生成归档时，压缩字节可能变化；校验不匹配时保留原样本并核实文件差异，不直接改写本文的校验值。需要完全相同的归档字节时，直接复制本机已下载的归档。

## 源码准备核验（2026-10-13 快照）

- 22 份归档 SHA-256 与上表一致，解压后逐项核对文件内容与归档；样例入口、许可证、锁文件及 14 个符号链接均保留。macOS 文件名按 Unicode NFC 等价比较，文件字节未改动；未发现 Git 子模块配置或 LFS 指针文件。
- 11 个项目目录均命中根 `.gitignore`，没有源码被 Git 纳入跟踪；`evals-readme.md` 可以纳入版本管理。
- 在 LWA 根目录执行 pytest 默认收集，共 3207 项，未进入 `tests/evals`；`ruff check src tests` 与 Git 差异空白检查通过。
- 当时未执行应用安装、构建、部署、更新或换源；后续实测按独立报告记录，不修改本次源码准备快照。

## 文档与证据分层

- **可提交**：本文的上游仓库、冻结版本、校验值、样例位置、通用配方与恢复样本说明；跨系统方案和仅按系统类型汇总的结论。
- **仅本地**：真实 SSH 接入、hostname、账号/IP、私钥位置、实际机器操作步骤，以及逐机分析、完整日志、截图和运行配置。统一放在 `design/5-checkpoints/`，不随仓库发布。
- 公开内容使用 macOS、Ubuntu、Debian WSL2 等分类，保留必要的版本、架构与验证范围；个人目录改写为 `<仓库根目录>`，网络地址改写为角色名称。通用说明不直接链接仅本地文件。
- 第三方源码、归档和运行产物继续忽略；新增公开内容须单独审阅，不通过放开整个证据目录来发布。

# SWE-bench 轨迹重放

用冻结的 Agent 操作轨迹，在新沙箱中真实执行命令和评测，比较调度、资源限制与内存管理方案。模型调用费用为 0。

仓库包含一条完整的 `django__django-14672` 轨迹、回放入口和实验流程。轨迹来自 500 题 SWE-bench Verified 采集；完整 500 题数据的分发方式见 [数据发布](docs/publishing.md)。

## 这条轨迹包含什么

| 项目 | 数值 |
| --- | ---: |
| 模型调用记录 | 20 |
| Agent 工具操作 | 35 |
| 独立评测工具操作 | 2 |
| API 等待累计 | 46.458 秒 |
| 采集时 Agent 阶段耗时 | 67.174 秒 |
| 采集时评测结果 | resolved=true |

任务修复 Django `ManyToManyRel.identity` 中列表参与哈希的问题：

```diff
- self.through_fields,
+ make_hashable(self.through_fields),
```

查看 [原始对话的发布副本](examples/django-14672/run/j000/attempt-sample/trajectory.json)、[补丁](examples/django-14672/run/j000/attempt-sample/prediction.patch) 和 [轨迹字段说明](docs/trace-format.md)。发布副本替换了实验主机路径和请求标识；工具命令及输出保持原字节，转换和 SHA256 记录在 `bundle-manifest.json`。

## 如何重放

```text
读取冻结清单、校验文件
  → 校验镜像 ID，创建 2CPU / 4GiB 新沙箱
  → 按原 API 时长等待
  → 真实执行下一条工具命令，保存新输出和资源指标
  → 重复等待与执行
  → 将本次补丁送入新的评测沙箱
  → 汇总一致性与性能
```

执行核心沿用已有 `replay_one()`。`api_response.elapsed_seconds` 控制等待；`tool_request` 中的命令、目录、超时控制实际执行；原 `tool_result` 用作比较基准。每条工具执行后立即比较输出和返回码，结束时比较补丁及评测结果。

固定的是顶层操作序列和模型等待。命令内部的条件分支、工具耗时、资源消耗和测试结果仍由本次运行决定。普通执行差异会记入结果并继续固定序列；基础设施故障记录为运行错误。模型面对新输出时的重新规划，属于另一个实验问题。

## 1. 下载与离线校验

克隆本仓库后进入仓库目录。Python 3.11 或更新版本可执行离线工具：

```bash
python3 scripts/inspect_trace.py examples/django-14672 --scan-credentials
python3 scripts/replay.py --output outputs/validation-01 --validate-only
```

输出目录每次使用新名称。离线校验检查文件哈希、事件顺序、请求和输出配对、镜像标识以及资源配置。

## 2. 准备 Linux 回放环境

真实回放使用 Linux x86_64、启用 containerd image store 的 Docker Engine、cgroup v2，以及可访问本机 Docker 的当前用户。至少提供两个可用物理核和足够容纳 4GiB 沙箱及宿主服务的内存。镜像层与容器写入还会消耗磁盘空间。

原采集使用 Docker 29.6 的 containerd image store，其 image ID 口径与 classic graphdriver 有差异。入口检查存储类型，沿用原 image ID 口径；daemon 配置由实验环境自行准备。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-replay.txt
docker info
python scripts/replay.py --output outputs/image-check-01 --prepare-image
```

准备步骤使用记录的镜像引用，并校验不可变 image ID。镜像引用发生变化时应取得原镜像副本；环境 ID 匹配是重放对照的前提。镜像准备的网络与磁盘开销单独记录。使用私有 Docker daemon 时，可在命令前设置自己的 `DOCKER_HOST`。

若公共仓库无法按记录引用拉取，可指定同题官方镜像标签，入口仍要求拉取后的 image ID 完全匹配：

```bash
python scripts/replay.py --output outputs/image-check-02 --prepare-image \
  --image-reference docker.io/swebench/sweb.eval.x86_64.django_1776_django-14672:latest
```

也可先通过 `docker load` 导入保存的精确镜像，再运行准备检查。

CPU 和 NUMA 绑定由入口根据当前主机选择；明确的绑定参数见 `python scripts/replay.py --help`。每沙箱 CPU 配额为 2、内存限制为 4GiB，swap 为 0。

## 3. 运行一条真实轨迹

```bash
python scripts/replay.py --output outputs/replay-01 --concurrency 1 --wait-scale 1
```

`--wait-scale 1` 保留采集时的 API 等待节奏。`0` 可用于功能调试；正式性能对照需保持相同等待比例。

回放入口禁用模型构造和凭据读取。Agent 与 evaluation 使用分别创建的沙箱，评测运行本次产生的补丁。工具以禁用网络、丢弃 capabilities 的容器执行；镜像准备阶段在宿主访问镜像仓库。

## 4. 读取结果

查看输出目录中的 `summary.json`、`status.json`、`configuration.json` 和任务目录。逐步 `compare-*.json` 保存新旧返回码、输出摘要和差异；`prediction.patch` 与 `eval_report.json` 保存本次补丁及评测。`events.jsonl`、`metrics.jsonl` 保存事件与资源观测。

- `state=completed`：执行器完成该轨迹。
- `functional_equal`：初始源码、命令状态、补丁与评测满足当前比较规则。
- `resolved`：本次 SWE-bench 评测判定修复成功。

这三种口径分别汇报。输出中的时间戳、PID 等变化可产生文本差异，原始和规范化比较都会保留。详见 [实验流程与口径](docs/experiment-workflow.md)。

## 5. 扩展到多题和算法实验

通过 `scripts/export_trace.py --help` 导出其他完整采集 attempt，保持相同数据格式。完整 500 题使用冻结清单为每题选择一个 attempt；清单之外的重试记录单独归档。

入口支持 `--source` 和 `--selection`，用于选择其他轨迹根目录及任务清单。先完成单题和小样本一致性检查，再扩大并发。开发调度与压缩算法时固定轨迹、镜像状态、资源总预算和等待比例，比较吞吐、工具 P95/P99、内存峰值、OOM 以及结果一致性。

当前发布入口按可用 CPU 拓扑提供基础回放。原 64 槽实验还有滚动镜像导入、磁盘回压、父内存池与中断续跑控制；精确复现实验配置需要单独移植和核对这些机制。

## 验证与来源

```bash
python3 -m unittest discover -s tests -v
```

采集模型记录为 `deepseek-flash`，`thinking=enabled`，`reasoning_effort=max`，最多 50 轮。数据集 revision 为 `c104f840cc67f8b6eec6f759ebc8b2693d585d4a`。受步数限制结束的轨迹同样保存全部已执行操作，其解题结论单独统计。

本仓库是为跨主机分享制作的派生发布版本；文件转换与原始来源可通过清单核验。37项离线测试和样例校验已通过，发布入口的 Linux Docker 端到端验证留待目标主机执行。范围见 [验证记录](docs/validation.md)。第三方内容与许可见 [来源说明](THIRD_PARTY.md)。

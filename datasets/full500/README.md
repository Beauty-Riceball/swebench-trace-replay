# 完整 500 题重放清单

`selection.json` 选择原始采集的同 500 个最终 attempt，保持题目顺序；将服务器绝对路径改为解压目录下的相对路径。原始归档包含 510 个 attempt，其中另 10 个历史重试不纳入这份重放清单。原始文件未修改。

需要单独取得 `full500-traces.tar.gz`（5,132,076,046 字节），SHA256：

```text
05d1226212a12f325683110774d1ea86442a0d3809a9523ccde61a7118872f3f
```

完整分卷在本仓库的 [full500-20260922-v1 Release](https://github.com/Beauty-Riceball/swebench-trace-replay/releases/tag/full500-20260922-v1) 中；本目录保存选择清单和验证记录，不包含 Docker 镜像。原始分卷用于私有备份与重放；公开发布需先完成完整共享副本审查。

若下载的是分卷，把同一版本的所有 `full500-traces.tar.gz.part-000` 起的文件、`SHA256SUMS.parts`、`SHA256SUMS.archive` 与 `release-manifest.json` 放到同一目录，先逐卷校验再合并。以下命令在 Linux 上执行；macOS 可将 `sha256sum -c` 换成 `shasum -a 256 -c`：

```bash
sha256sum -c SHA256SUMS.parts
cat full500-traces.tar.gz.part-* > full500-traces.tar.gz
sha256sum -c SHA256SUMS.archive
```

只合并该版本清单所列的分卷，使用新的下载目录以免混入旧卷。校验失败时不要继续解压重放。

## 解压与离线校验

在仓库根目录执行。以下 `/absolute/path` 请替换为自己的路径，解压目录必须新建：

```bash
mkdir /absolute/path/full500
tar -xzf /absolute/path/full500-traces.tar.gz -C /absolute/path/full500
TRACE_ROOT=/absolute/path/full500/capture
python3 scripts/replay.py \
  --source "$TRACE_ROOT" \
  --selection datasets/full500/selection.json \
  --output /absolute/path/full500-validation-01 \
  --validate-only
```

原始归档的成员是 `capture/j000/attempt-...`，没有额外的 `run/` 目录。因此这里必须指定 `--selection`；入口的默认样例扫描路径不适用于原始归档。校验成功应显示 500 个任务；这一步不会创建容器或调用模型。

## 准备镜像并真实重放

先按仓库 [README](../../README.md) 准备 Linux x86_64、cgroup v2、Docker containerd image store 和 Python 依赖。镜像通过冻结引用另行取得，拉取或导入后的 image ID 必须与轨迹相同。公共引用失效时需要原精确镜像副本；本轨迹归档不含镜像层。

```bash
python scripts/replay.py \
  --source "$TRACE_ROOT" \
  --selection datasets/full500/selection.json \
  --output /absolute/path/full500-image-check-01 \
  --prepare-image

python scripts/replay.py \
  --source "$TRACE_ROOT" \
  --selection datasets/full500/selection.json \
  --output /absolute/path/full500-replay-01 \
  --concurrency 1 --wait-scale 1
```

以上重放命令覆盖全部 500 题；并发 1 是便携入口示例参数。每个沙箱仍为 2 CPU、4 GiB、swap 0，当前模型调用为 0。提高并发需匹配目标机器资源，并预留镜像和容器磁盘空间。便携入口没有原实验的滚动镜像供给和父内存池调度，不能直接当作原 64 槽性能实验的复现。

`provenance.json` 保存原 selection、原归档清单和派生 selection 的 SHA256；`validation.json` 保存逐题核对结果：500 个唯一题目与顺序一致、3,673 项源 artifact SHA 匹配、2,500 项必需文件存在，选定 attempt 共 171,212 个清单文件、131,778 个事件。

上述结果是清单间核对，未重新读取全部解压文件，也未执行这一便携版完整 500 题的 Linux Docker 重放。原归档的内容完整性应先由归档 SHA 与逐文件清单验证。这份相对路径清单只改变路径元数据；原始归档未脱敏，不能将它视为已经完成完整 500 题发布扫描的共享副本。

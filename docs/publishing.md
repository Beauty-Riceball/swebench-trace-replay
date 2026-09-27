# GitHub 发布与同组复用

## 仓库内容

Git 仓库保存回放代码、文档、一条完整发布样例和校验清单。镜像层、运行结果、大规模轨迹包使用独立存储或 Release 附件。

仓库发布流程：本地整理 → 校验派生轨迹与代码 → 选择仓库可见性 → GitHub 登录 → 创建仓库并推送 → 用远端提交验证发布结果。已有仓库可通过分支和 PR 审阅。

维护者在项目目录使用 GitHub CLI：

```bash
gh auth login --hostname github.com --git-protocol https --web
git init -b codex/trace-replay
git add README.md THIRD_PARTY.md LICENSES docs engine runtime scripts tests examples requirements-replay.txt .gitignore .gitattributes
git commit -m "Add frozen SWE-bench trace and replay workflow"
gh repo create swebench-trace-replay --private --source . --remote origin --push
```

需要公开时，按维护者选择修改可见性参数。将同组成员加入私有仓库后，他们可克隆代码、下载附件并运行同样的校验与重放入口。

## 完整 500 题

本次原始完整归档约 4.78GiB，原始 SHA256 为：

```text
05d1226212a12f325683110774d1ea86442a0d3809a9523ccde61a7118872f3f
```

这是原始归档的校验值。发布前应按与样例相同的转换规则导出完整共享副本，并生成独立清单、任务覆盖报告和新归档 SHA256。原始归档与共享归档各自保留身份。

GitHub 普通 Git 文件上限为 100MiB；Release 单附件需小于 2GiB。因此共享归档可以按 1GiB 分卷，附上各卷及合并文件的 SHA256。[GitHub 文件限制](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github)、[Release 附件限制](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)。

Linux 上的分卷与校验示例（文件名代表已经准备好的共享归档）：

```bash
split -b 1G -d -a 3 full500-shared.tar.gz full500-shared.tar.gz.part-
sha256sum full500-shared.tar.gz > SHA256SUMS.archive
sha256sum full500-shared.tar.gz.part-* > SHA256SUMS.parts
```

接收者下载同一版本的全部分卷和校验文件后：

```bash
sha256sum -c SHA256SUMS.parts
cat full500-shared.tar.gz.part-* > full500-shared.tar.gz
sha256sum -c SHA256SUMS.archive
mkdir full500-shared
tar -xzf full500-shared.tar.gz -C full500-shared
```

Release 标注采集数据版本、导出工具提交、500 个唯一任务的选择清单，以及附件校验值。Docker 镜像通过冻结引用另行取得，轨迹附件不包含全部镜像层。

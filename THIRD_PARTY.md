# 来源与许可

- 任务来源：[SWE-bench Verified](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified)，revision `c104f840cc67f8b6eec6f759ebc8b2693d585d4a`。样例实例为 `django__django-14672`。
- 样例工具输出、评测脚本和补丁包含 Django 代码片段。Django 使用 BSD 3-Clause 许可，文本保存在 [LICENSES/Django-BSD-3-Clause.txt](LICENSES/Django-BSD-3-Clause.txt)。
- 评分通过 SWE-bench Python 包调用；项目 MIT 许可保存在 [LICENSES/SWE-bench-MIT.txt](LICENSES/SWE-bench-MIT.txt)。
- 回放执行核心派生自本研究项目的冻结实验代码。为移植运行环境而做的修改由运行时代码说明记录；源文件哈希与发布文件哈希分别保存。
- 模型响应来自既有实验采集，调用记录中的模型 ID 为 `deepseek-flash`。工具命令和输出与样例原件逐字节对应；部分元数据经过转换。

第三方内容继续适用各自许可。仓库原创部分的对外许可由维护者另行指定。

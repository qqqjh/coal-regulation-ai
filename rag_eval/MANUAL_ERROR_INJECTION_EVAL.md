# S1302 人工错误注入自动评测

评测对象为：

- 盲测文档：`new_docs/test_doc/S1302人工错误注入测试集_v1/004 S1302工作面作业规程（综采）_人工错误注入版_v1.docx`
- 金标：`new_docs/test_doc/S1302人工错误注入测试集_v1/004 S1302工作面作业规程（综采）_人工错误金标_v1.json`
- 金标规模：合规性错误 12 项、错别字 10 项、重复内容 8 项，共 30 项。

## 一键运行（自动启动后端和 worker）

在项目根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File rag_eval\run_manual_error_injection_eval_v1.ps1
```

该命令不会启动前端。它会自动启动 MinerU API、FastAPI 后端和 `v9_worker.py`，通过与前端相同的 V9 API 上传盲测文档，等待审查完成，生成报告，然后停止本次启动的三个服务进程。

如果 MinerU 本身因模型或环境问题无法启动，可临时使用本地 DOCX 解析兜底：

```powershell
powershell -ExecutionPolicy Bypass -File rag_eval\run_manual_error_injection_eval_v1.ps1 --local-docx-parser
```

正式评测应优先使用默认 MinerU 路径，因为这是当前 Web 审查对新上传 Word 的标准解析链路。

如果后端和 worker 已经启动，可直接执行：

```powershell
D:\Anaconda\envs\langchain0.3\python.exe rag_eval\scripts\evaluate_manual_error_injection_v1.py
```

## 评估已有任务

```powershell
D:\Anaconda\envs\langchain0.3\python.exe rag_eval\scripts\evaluate_manual_error_injection_v1.py --job-id 任务ID
```

## 离线评估导出的问题列表

问题 JSON 可以是数组，也可以是包含 `issues` 数组的对象：

```powershell
D:\Anaconda\envs\langchain0.3\python.exe rag_eval\scripts\evaluate_manual_error_injection_v1.py --issues-json path\to\issues.json
```

## 输出

默认写入 `rag_eval/reports/s1302_manual_error_eval_v1/`：

- `s1302_manual_error_eval_v1.html`：可视化评测报告；
- `s1302_manual_error_eval_v1.json`：完整机器可读结果；
- `s1302_manual_error_eval_v1.csv`：逐条金标与误报明细；
- `s1302_manual_error_issues_raw_v1.json`：系统原始问题列表；
- `service_mineru.log`、`service_backend.log`、`service_worker.log`：使用自动服务模式时的进程日志。

使用自动服务模式时，JSON/HTML 报告还会记录本次启动的 MinerU、backend 与 `v9_worker` 峰值工作集内存。该数值用于估算应用进程内存需求，不包含独立部署的 MongoDB、Milvus 或其他外部服务。

报告同时提供两组口径：

- 发现指标：文本或位置与金标匹配，不强制要求类型和最终判定正确；
- 严格指标：错误匹配、问题类型和最终判定全部正确。

正式汇报建议使用严格精确率、严格召回率和严格 F1，同时展示合规性、错别字、重复内容三类分项结果。

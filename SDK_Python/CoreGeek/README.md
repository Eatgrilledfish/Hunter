# SDK 开发位置

参赛入口已实现为此目录的 `main3.py`，本项目启动脚本是 `../run.sh`。目前未收到官方 SDK 压缩包或原始 run.sh。
对话提供的入口源代码完整保存在 `docs/source/main3_original.py`（仓库根目录相对路径）。

完整实现方案见 [SDK 设计](../../docs/SDK_DESIGN.md)，开发与验收顺序见 [实施计划](../../docs/SDK_VALIDATION_PLAN.md)。当前实现与真实验证记录见 [实现状态](../../docs/IMPLEMENTATION_STATUS.md)。

已保留 `POST /` 和 `callback(json_data)`；响应只输出 `roleCommandMap`、`prompt`、`executeCmd`。原始入口在 docs/source 中留档，运行入口不再回显 Request。

## 比赛诊断日志

按原方式 `bash SDK_Python/run.sh <port>` 启动即可。SDK 默认使用
`print(..., flush=True)` 输出日志，由比赛平台收集，不写日志文件、不额外打包。
赛后把平台打包的完整标准输出日志交给我们即可。

每行以 `HUNTER ` 开头，后面是 JSON：`run` 标识进程，`call` 标识一次请求，
`round` 为请求回合，`seq` 为日志事件序号。长事件分为 `part/parts` 多段；
按同一 `run/seq` 的 `part` 顺序拼接 `payload` 字符串，再解析 JSON，得到完整内容。
缺少分段或请求没有对应响应时，可定位日志缺失或中途退出的位置。

- `startup`：Python 版本、各 SDK 源文件 SHA-256、实际规则和策略配置。
- `request`：完整原始请求，含上一回合执行反馈、错误、LLM 和沙盒返回。
- `decision`：选择和拒绝动作的依据、规则差异、风险与各模块诊断。
- `task_state`：任务阶段、待返回通道、答案、执行记录及未知副作用状态。
- `response`：完整返回指令、耗时、正常/缓存/隔离/锁超时/降级结果。
- `exception`：错误及完整堆栈；非法 JSON 和超大请求另外记录。

日志不改变 HTTP 响应的三个字段。打印失败不会使本来合法的动作失败。
日志保留任务正文、答案和生成命令，方便复盘。定位依赖平台保留完整输出；
平台截断/丢失部分日志或未返回的判题器内部状态无法从 SDK 日志补出。

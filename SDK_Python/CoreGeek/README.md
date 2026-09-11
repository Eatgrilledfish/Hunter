# SDK 开发位置

参赛入口已实现为此目录的 `main3.py`，本项目启动脚本是 `../run.sh`。目前未收到官方 SDK 压缩包或原始 run.sh。
对话提供的入口源代码完整保存在 `docs/source/main3_original.py`（仓库根目录相对路径）。

完整实现方案见 [SDK 设计](../../docs/SDK_DESIGN.md)，开发与验收顺序见 [实施计划](../../docs/SDK_VALIDATION_PLAN.md)。当前实现与真实验证记录见 [实现状态](../../docs/IMPLEMENTATION_STATUS.md)。

已保留 `POST /` 和 `callback(json_data)`；响应只输出 `roleCommandMap`、`prompt`、`executeCmd`。原始入口在 docs/source 中留档，运行入口不再回显 Request。

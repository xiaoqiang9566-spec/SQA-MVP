# Bug 审计独立 Worker 验证记录

范围：分支 `codex/bug-audit-worker`，从台账基线 `68a0479` 继续实施独立执行器，仅改 Bug 审计代码、页面局部、测试及专属部署资料。当前尚未进行生产切换。

## 已执行的验证

- 使用 Windows Python 3.13 创建全新 venv，依照专属 requirements 安装 `tzdata==2026.6` 成功；未继承系统 site-packages。
- 干净 venv 的 `python -X utf8 -m unittest discover -s tests -q`：最终 97/97 通过。原有台账 32 项保留通过；Node 语法和差异检查同时通过。
- 子进程在中文空格复制路径、不同 cwd、空 HOME/USERPROFILE、移除外部 CLI 的 PATH 下，完成 doctor、demo、reconcile；未生成用户或读取个人工具目录。
- 演示真实执行时拦截任何 socket.connect 和 Config.secret，验证无需网络或真实凭据。
- 模拟 HTTP 覆盖 TP 过滤、分页、完整 History、模型 Responses/Chat Completions、三字段写入、消息真实提及、附件上传和下载 SHA-256。测试只请求本机临时 HTTP 服务。
- 故障恢复覆盖跨进程互斥、进程退出锁释放、TP 成功丢响应、本地登记中断、飞书未知消息、附件拒绝后补发、同日重复运行、冻结窗口和模型预算保留；实际请求计数验证未重复已确认副作用。
- 验证旧库没有 Worker 表仍可读，查询只投影来源/模式/profile/阶段；备份恢复保留 jobs/outbox/尝试历史。runs_dir 仍需单独备份。
- 执行真实 Bug 页 JavaScript 渲染函数，验证合成标识、未执行真实回填提示和 profile 转义；Node 语法检查通过。
- Windows 默认 GBK stdout 导致 JSON 中文乱码的问题已有失败用例和修复，入口固定输出 UTF-8。
- Git 差异检查通过；新增中文使用文档与 README 均为 UTF-8 无 BOM。

独立整分支审查报告提出 3 个 Important，无 Critical/Minor：等价 TP URL 绕过去重、Feature 描述未参与写前比较、模型 not_applicable 跳过适用说明。三项均已用失败用例复现并修复；补充验证了旧 scope hash/带 ID 的旧展示标签不能绕过当日切换门禁。真实模型合成探针还复现了“格式有效但语义 pending 被误判接口失败”，已将协议能力与语义结论分开，新增失败回归后修复，并纳入最终 97 项验证。

## 在线输入及未验范围

用户已确定公司模型 `https://aiapi.suunto.cn/v1`、`gpt-6.1-sol`、Responses，已填本机 Git 忽略模板 `work/bug-audit-worker/worker.toml`。截至本次检查，Worker 启动进程中三个专属凭据变量均不存在；TP 和飞书的部署身份/范围仍需填入该配置或提供已准备好的配置文件路径。

用户随后授权从 `D:\3681\Documents\targetprocess-workflow-v2\.env` 读取。该文件只含 TP 配置，已通过 Worker 原生只读 HTTP 确认项目 `114081 Suunto work` 和团队 `126106 ESW UI Team`、`188759 ESW China NG3 Driver`、`188760 ESW China NG3 Framework` 均完整返回，凭据未输出或复制入仓库。

用户已补充本机模型/飞书配置。已在验收进程内映射本机填写的凭据，生成只含环境变量名的 `work/bug-audit-worker/worker.online-validate.toml`，原文件保留。公司 Responses 合成探针返回 completed，结构/覆盖校验通过；语义 pending 正常保留，不等同真实业务语义验收。

在线 Context 元数据已确认属于项目 114081，三个字段及 Testing/Development 选项均完整。机器人应用的实际身份为“肖强的飞书助手”，本机 bot_open_id 与接口返回值不符，验收配置已校正；群及成员权限检查仍需读取最终结果。

真实 dry_run 已在 `work/bug-worker-online-readonly` 隔离目录启动，尚待完成取证、语义检查和对账。完整只读链、人工语义质量对照、受控真实 apply、首次自动触发均未验收。没有写入真实 TP、发送真实飞书消息、修改旧 `bug-2` 调度或创建新生产调度。

Linux/macOS 以及 Python 3.11 已配置离线 CI 矩阵，但尚未由本机执行，不能标为通过。原有跨模块固定历史验收缺 `work/pilot-sources.json` 及六份原样本，本轮未修改其他模块或放宽断言。

## 实施裁定

1. 沿用当前 checkout、新建专属分支，保留未跟踪的既有文档；未建额外 worktree，以免遗漏已批准设计。代价：实现文件在当前共享目录可见。
2. 已获设计批准与 Native 选择，按“继续”连续实施；缺在线参数仍完成离线工作。代价：真实接入需后续补验，不能据离线测试宣称生产闭环。
3. 保持通用备份模块不动，数据库备份保留 Worker 日志，部署者另存 runs_dir。代价：只恢复数据库不足以恢复执行。
4. 检查点保存脱敏证据及原资料哈希，预检比较原资料哈希。代价：无法从检查点还原已脱敏文本。
5. 旧 Worker 没有可逆 origin 身份时，对同日同名同团队的不同 scope hash 保守阻塞，须先人工对账。代价：不同 TP 实例恰好同项目名/团队名时也可能被阻塞。
6. 重叠而不相同的团队集合不在首版自动协调范围，生产必须保持既定完整团队集合，变更范围先停止旧执行器并跨日切换。代价：不能依赖本机去重支持多个交叉范围执行器。

## 审查保留的边界

- 外部 runs_dir 联合备份：按第 3 项裁定执行，不修改通用备份。
- TP 预检至写入的竞态：沿用不承诺原子 CAS 的已批准设计；只读预检和回读不能消除全部并发。
- 不共享台账的多个生产节点：明确不支持，仅部署一个活跃执行器。
- 缺消息 ID 的未知发送：保留 unknown，人工找到消息后再在线核验，禁止盲目重发。
- 团队集合交集：按第 6 项部署边界处理，不自行扩大既定业务范围。
- 真实公司接口、人工语义质量及未运行的 CI：继续作为上线验收项，不用离线结果替代。

未延后任何 Minor 审查项。

完整使用和迁移步骤见 [Bug 审计 Worker 使用说明](../../../outputs/SQA_MVP/Bug审计Worker使用说明.md)。

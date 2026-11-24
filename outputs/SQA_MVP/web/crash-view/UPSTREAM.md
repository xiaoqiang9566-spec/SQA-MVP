# 崩溃看板展示代码来源

复用用户指定的内部工程 `http://10.134.23.50:8765/dashboard/`，于 2026-11-24 读取 index.html、styles.css、app.mjs、data.mjs、snapshot.mjs、signatures.mjs。

保留 data.mjs 与 signatures.mjs 的原有设备日集合去重、平台覆盖、趋势及类型汇总逻辑。工作台适配改动包括：

- snapshot.mjs 通过经过登录鉴权的固定源站只读数据适配器加载发布清单与分片，保留原 SHA-256 校验。
- app.mjs 默认选择 Race 3S 2.57.62 / 2.57.64；按账号和浏览器访问地址保存日期、维度筛选、粒度、图例及类型搜索。
- index.html 添加筛选恢复状态说明。

此目录代码固定在当前工作台版本，源站发布更新只更新数据，不自动替换执行代码。仅限本工作台使用。

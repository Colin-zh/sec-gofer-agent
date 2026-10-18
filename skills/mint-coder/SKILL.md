---
name: mint-coder
description: 为 Mint 的 mint.execute API 生成或改写 Python 代码，涵盖 pandas/Spark、数据契约、分组与时间窗口、多表关联、代理指标和批量生成。适用于用户要求使用 Mint 实现代码的场景。
---

# Mint 代码生成

以 `mint.execute` 为核心，把需求转为口径明确、数据契约可检查、验证程度可追溯的 Python 代码。使用领域无关的计算模式，不把单次任务的字段、阈值或业务假设固化为通用默认值。

## 工作流程

1. 阅读可用字段定义、相关代码和输入样例；确认输入 DataFrame 类型、运行环境和真实 API。本 skill 支持 pandas 与 PySpark 两种模式：输入为 `pandas.DataFrame` 时先读 [pandas 基础调用](references/execute-basic.md)；输入为 `pyspark.sql.DataFrame` 时先读 [PySpark 执行](references/execute-spark.md)，需要补充通用调用约定时再读基础调用。类型不明时先从用户上下文或创建输入的代码确认，不能默认选择模式。其他参考按下方路由按需读取，不一次加载全部文档。
2. 建立口径契约：输入/输出粒度、实体/账户/事件键、单位、筛选、分组、窗口与锚点、排序、去重、分子/分母、边界、缺失及无事件行为。简单任务可简述，不强制输出完整表格。
3. 选择精确实现、代理实现、扩展字段模板或子指标实现；字段缺失按 [数据契约](references/data-contracts.md) 处理，不静默猜测，也不一律留空。
4. 对会改变结果的未决问题追问；复用适用范围相同的已确认口径。用户更正时重新检查受影响的已生成条目、说明与测试。
5. 编写签名清晰的普通 callable，复用已存在的 helper；阈值、字段映射、窗口等配置通过 `params` 传入。调用前及组内分别验证必要前置条件。
6. 声明与返回形状、字段顺序和类型一致的 `schema`；新增列不能只用 `schema="*"`。按目标粒度使用 `partition`，跨实体关联不能先按单实体隔离。
7. 按 [分层验证](references/validation.md) 执行能覆盖改动的最小验证；报告实际完成层级。缺少 Mint 时不能以 callable 测试代替框架集成结论。
8. 交付代码、关键口径、代理偏差/扩展字段前置条件、数据覆盖限制和验证状态。批量任务按 [批量交付](references/batch-delivery.md) 分批保存、重读并总检。

## 澄清与生成模式

- 不仅凭字段名推断含义；字段类型相同不代表语义相同。先区分资料未知、字段缺失和规则未定义。
- 精确实现优先；代理改变指标含义，需用户授权并在说明/输出命名标明。扩展字段可以设计，但必须标为假设并给出接入条件，不能声称数据已存在。
- 复杂结论缺少定义或阈值时，可提供已确定的子指标；不能擅自用示例值作阈值，也不能把候选关联当成事实结论。
- 批量任务先归纳共用歧义，记录确认值及作用范围，按交互能力逐项确认；不要反复询问已确定参数，也不要为减少问题默认合并方向或改变分母。
- 安全的编码细节可自行选择；窗口、计数对象、替代字段、单位、缺失策略、匹配复用等结果口径存在合理分歧时必须确认。

## Mint 代码约定

- 使用项目文档中真实存在的 Mint API；不要虚构包装器、参数或执行模式。
- 优先使用签名清晰的普通命名 Python callable，避免将复杂逻辑塞入内联表达式。
- 保持处理逻辑与领域无关。实现时使用用户提供的实际字段名；示例中优先使用 `entity_id`、`related_id`、`event_time`、`value`、`category` 等通用名称。
- 确保 schema 的字段名、类型和顺序与返回的记录或 DataFrame 对齐。特别注意：单独的 `schema="*"` 不会声明新增字段。
- 按组计算时使用 `partition`；计算依赖行顺序时使用 `presort`。不要假设输入的物理顺序具有业务含义。
- Spark DataFrame 会选择 Spark 执行路径，但这并不意味着要将 callable 改写成 Spark SQL。Spark 引擎配置与结果查看方式请参考 Spark 文档。
- 领域解释或建议阈值仅适用于特定上下文，不应表述为普遍结论。
- 缺失、零值、无事件、无匹配和非法输入分别处理。不要依赖 `sum`、`nunique` 等默认忽略行为悄悄改变口径；采用容错策略时输出质量信息或按项目约定记录问题。
- 配对、关系遍历或候选搜索说明复用规则、并列处理和规模成本；不得静默截断以伪装完整结果。

## 按需路由

| 任务信号 | 补充参考 |
|---|---|
| 输入为 `pandas.DataFrame` | [pandas 基础调用](references/execute-basic.md) |
| 输入为 `pyspark.sql.DataFrame` | [PySpark 执行](references/execute-spark.md)，按需补充基础调用 |
| 字段缺失、粒度/标识不清、代理或假设字段 | [数据契约与生成模式](references/data-contracts.md) |
| 最近一段时间、日历单位、截止日、时区、后续事件 | [时间语义与数据覆盖](references/time-semantics.md) |
| 单位转换、精确相等、误差、数值倍数 | [数值精度与容差](references/numeric-semantics.md) |
| 核销、归因、事件配对、数值分配 | [时序配对与分配](references/event-matching.md) |
| 多表关联、有效期、候选路径/连通分量 | [关联契约与关系推理](references/relational-contracts.md) |
| 测试、空输入、序列化、引擎差异 | [分层验证](references/validation.md) |
| 多条需求、文件写入、续做、覆盖已有内容 | [批量生成与交付](references/batch-delivery.md) |

维护本 skill 时使用 [领域无关回归案例](references/regression-cases.md)；它是验收规格，不代表测试已经运行。

## 参考文档索引

### 基础执行

- [基础 execute 调用](references/execute-basic.md)：callable、`params` 和标准 `mint.execute` 调用形式。
- [数据连接](references/execute-join.md)：合并 DataFrame 输入。
- [单字段映射](references/execute-map-single.md)与[多字段映射](references/execute-map-multi.md)：可复用的列转换。
- [分组与预排序](references/execute-partition.md)：分组计算与组内排序。
- [输出 schema](references/execute-schema.md)：标量与嵌套 schema 表达式。
- [Spark DataFrame](references/execute-spark.md)：Spark 执行与结果查看。
- [可迭代结果与 yield](references/execute-yield.md)：逐条输出筛选后的行或记录。

### 加工逻辑

- [序列操作](references/execute-logics-series.md)：差分、筛选和值之间的关系。
- [时间窗口](references/execute-logics-window.md)：范围筛选、时间分量、分桶和相邻时间间隔。
- [会话切分](references/execute-logics-session.md)：切分有序记录并汇总各区段。
- [实体内图关系](references/execute-logics-graph.md)：相关实体之间的有向或无向关系。
- [跨实体图关系](references/execute-logics-graph-relation.md)：共同邻居、图连通分量及图规模注意事项。

### 指标计算

- [分布与集中度](references/execute-metrics-distribution.md)
- [时间跨度、速率与间隔](references/execute-metrics-time.md)
- [趋势与相邻变化](references/execute-metrics-trend.md)
- [极值](references/execute-metrics-extremum.md)
- [有序序列中的相对位置](references/execute-metrics-location.md)
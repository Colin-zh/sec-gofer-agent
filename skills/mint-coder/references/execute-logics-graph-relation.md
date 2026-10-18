---
id: execute-logics-graph-relation
title: Graph Relation (Traversal) mint.execute Pattern
resource_type: pattern_source
summary: Reusable graph-relation logic for traversing relationships across entities: hubs, shared related entities, and connected components. Use this when you need to reason about how entities connect through shared related IDs or graph neighborhoods across the full dataset. This complements the single-entity relation pattern in execute-logics-graph.
terms: [execute, logics, graph, relation, traversal, networkx, community, entities, shared-relationship, connected-components]
related_source: [execute-logics-graph, execute-metrics-distribution]
priority: 60
---

## 适用场景
适用于跨实体看到“关系如何连起来”，而不是看单个实体和它有哪些相关实体。核心问题是：某个相关实体连接了多少实体、两个实体是否共享相同的相关实体，以及整个图是否由多个连通分量组成。

典型场景：
- 哪个相关实体连接了最多实体？
- 哪两个实体共享多少个相同的相关实体？
- 哪些实体和相关实体构成同一连通图块？

> 与 `execute-logics-graph` 的分工：前者是“一个实体的关系集合”（按 `date_key + entity_id` 的 partition 视角，输出每个实体一行）；本模式是“关系视角”（跨实体，通常整表视角），输出每个相关实体 / 每个实体对 / 每个节点的归属。
> 边界：只问某个值分布/集中度去 `execute-metrics-distribution`；只问逐行序列去 `execute-logics-series`；只问“一个实体和哪些相关实体有关系”去 `execute-logics-graph`。本模式看的是图结构和跨实体关系。

> ⚠️ 数据量预警：共享相关实体的 self-join 会产生 O(N²) 的实体对；连通分量会在整表上建立图，节点和边规模大时可能很快走到内存/时间瓶颈。规模大时应优先只跑 hub 统计，缩小时间范围，并对高度集中节点做过滤。

## 逻辑速查表（话术 → 关系 → pandas / networkx 写法）

> 让 `edges = df[[entity_id_col, related_id_col]].dropna()`；过滤空字符串后 `drop_duplicates()`，得到实体-相关实体去重边表。`related_id_col` 可能为 `None` 或 `""`，都需要过滤。

### A. 相关实体枢纽度（连接了多少实体）
| 话术 | 关系 | pandas 写法 |
|---|---|---|
| 一个相关实体给多少实体提供连接 | 相关实体枢纽度 RelationHubDegree | `partition={"by": [related_id_col]}` → `df[entity_id_col].nunique()` |

### B. 实体间共享相关实体（团伙线索）
| 话术 | 关系 | pandas 写法 |
|---|---|---|
| 两个实体共享几个相关实体 | 共享相关实体数 SharedRelatedCount | `edges.merge(edges, on=related_id_col)` → 过滤 `entity_a < entity_b` → `groupby([entity_a, entity_b])[related_id_col].nunique()` |
| 与某个实体共享相关实体的其他实体 | 共享相关实体名单 | 同上，过滤特定 `entity_a == target` |

### C. 连通分量（图归属）
| 话术 | 关系 | networkx 写法 |
|---|---|---|
| 哪些实体/相关实体属于同一个图块 | 节点归属 ComponentId | `G = nx.from_pandas_edgelist(edges, source=entity_id_col, target=related_id_col)`；`nx.connected_components(G)` |
| 图块大小 | ComponentSize | 组件节点数 `len(members)` |

> 两个实体之间的共享相关实体 self-join 会同时产生 `(a,b)` 和 `(b,a)`，需 `entity_a < entity_b` 去重；输出规模通常是 O(N²)。
> 连通分量是把图中通过边相连的节点都归到同一个社区/图块中；它不区分“实体”和“相关实体”——所有出现在边上的节点都会纳入同一图块。
> 若存在高度集中且合法的中间节点（例如平台账户），会把整张图连成一个巨大组件；此时需先筛掉高 hub 度节点，再算 graph component。

## 关系解读与借鉴值
> 这些关系输出的是“度数、共享数、节点归属”，必须转回具体数据语义。下表给经验解释，不是硬性判断。

| 关系 | 数值含义 | 借鉴值 |
|---|---|---|
| 相关实体枢纽度 RelationHubDegree | 一个相关实体连接了多少实体 | degree 高通常表示中转、桥接或集中连接 |
| 共享相关实体数 SharedRelatedCount | 两个实体共享多少个相同相关实体 | 共用多个相关实体常提示更强联系或更接近的网络结构 |
| 节点归属 ComponentId | 节点属于哪个图块 | 同一分量表明存在相连的关系网络 |
| ComponentSize | 图块中有多少节点 | size=1 为孤立；很大的图块说明网络较集中或连接广泛 |

## 参数（口径需先确认）
- `entity_id_col`: 实体字段，例如 `entity_id`。
- `related_id_col`: 相关实体字段，例如 `related_id`；`None` 或空字符串都需要过滤。
- `value_col`: 数值字段（可选，用于加权分析）。
- `partition_cols`: A 段用 `[related_id_col]`，B/C 段通常不 partition（整表）。
- `hub_degree_threshold`: 对高枢纽度节点做过滤的阈值（可选，防止整图串成超大组件）。
- `top_k` / `min_shared`: 共享相关实体输出的截断、最小共享数（可选）。

## 示例

### 示例一：相关实体枢纽度
```python
from typing import Any, Dict, List

import pandas as pd
from mint import execute

df = pd.DataFrame({
    "entity_id": ["e1", "e2", "e1", "e1", "e2", "e3"],
    "related_id": ["r1", "r1", "r2", "r3", "r3", "r3"],
})

def hub_degree(
    df: pd.DataFrame, partition_cols: List[str], entity_id_col: str
) -> List[Dict[str, Any]]:
    return [{
        **{col: df.iloc[0][col] for col in partition_cols},
        "hub_degree": int(df[entity_id_col].nunique()),
    }]

result = execute(
    df,
    hub_degree,
    schema="related_id:str,hub_degree:long",
    params=dict(partition_cols=["related_id"], entity_id_col="entity_id"),
    partition={"by": ["related_id"]},
)
print(result.to_markdown())
```

说明：
- `partition by related_id` 让函数看到某个相关实体下的全部实体集合。
- `df[entity_id_col].nunique()` 即“这个相关实体连接了多少实体”。
- r1 连了 {e1, e2} → 2；r2 连了 {e1} → 1；r3 连了 {e1, e2, e3} → 3。

### 示例二：实体间共享相关实体
```python
import pandas as pd
from mint import execute

df = pd.DataFrame({
    "entity_id": ["e1", "e1", "e1", "e2", "e2", "e3"],
    "related_id": ["r1", "r2", "r3", "r1", "r2", "r3"],
})

def shared_related(
    df: pd.DataFrame, entity_id_col: str, related_id_col: str
) -> pd.DataFrame:
    edges = df[[entity_id_col, related_id_col]].dropna()
    edges = edges[edges[related_id_col].astype(str).str.strip() != ""].drop_duplicates()
    ea = entity_id_col + "_a"
    eb = entity_id_col + "_b"
    pairs = edges.merge(edges, on=related_id_col, suffixes=("_a", "_b"))
    pairs = pairs[pairs[ea] != pairs[eb]]
    pairs = pairs[pairs[ea] < pairs[eb]]
    shared = pairs.groupby([ea, eb])[related_id_col].nunique().reset_index()
    shared.columns = ["entity_a", "entity_b", "shared_count"]
    return shared

result = execute(
    df,
    shared_related,
    schema="entity_a:str,entity_b:str,shared_count:long",
    params=dict(entity_id_col="entity_id", related_id_col="related_id"),
)
print(result.to_markdown())
```

说明：
- 不 partition，函数拿整表，`edges.merge(edges, on=related_id_col)` 自连接。建边前先过滤 `None`/空字符串。
- `entity_a != entity_b` 排除自连接；`entity_a < entity_b` 去掉 `(a,b)` 和 `(b,a)` 的重复。
- e1, e2 共享 r1, r2 → `shared_count=2`；e1, e3 共享 r3 → 1；e2, e3 共享 r3 → 1。

### 示例三：连通分量 / 图归属（需要 networkx）
```python
from typing import Any, Dict, List

import networkx as nx
import pandas as pd
from mint import execute

df = pd.DataFrame({
    "entity_id": ["e1", "e2", "e1", "e2", "e3", "e3", "e4"],
    "related_id": ["e2", "e1", "r1", "r1", "r2", None, ""],
})

def component_membership(
    df: pd.DataFrame, entity_id_col: str, related_id_col: str
) -> List[Dict[str, Any]]:
    edges = df[[entity_id_col, related_id_col]].dropna()
    edges = edges[edges[related_id_col].astype(str).str.strip() != ""].drop_duplicates()
    G = nx.from_pandas_edgelist(edges, source=entity_id_col, target=related_id_col)

    comps = [sorted(comp) for comp in nx.connected_components(G)]
    comps.sort(key=lambda c: (-len(c), c[0]))

    rows = []
    for comp_id, members in enumerate(comps):
        for m in members:
            rows.append({"node_id": m, "comp_id": comp_id, "comp_size": len(members)})
    return rows

result = execute(
    df,
    component_membership,
    schema="node_id:str,comp_id:long,comp_size:long",
    params=dict(entity_id_col="entity_id", related_id_col="related_id"),
)
print(result.to_markdown())
```

说明：
- 不 partition，整表建图；节点 = 实体 ∪ 相关实体，边 = 实体-相关实体关系。
- 过滤 `None` / 空字符串后再建边，否则空数据会被当成真实节点。
- `nx.connected_components` 会把同一网络中的所有节点归到同一图块；本例中 e1 和 e2 互相连接，并共享 r1，因此它们位于同一连通分量。

## 生成注意事项
- **视角别搞混**：本模式是关系视角（跨实体），通常是整表路径，且 `partition` 要么固定在 `related_id` 要么不 partition；不是单实体 `execute-logics-graph` 的近邻逻辑。
- **名单/序列类结果必须落库**：`set` / `list` 需写成字符串列（如 `"|".join(sorted(...))`）或保留为数量字段。
- **self-join 必须去重**：`entity_a != entity_b`（排除自连接）+ `entity_a < entity_b`（去掉双向重复）否则 (a,b) 和 (b,a) 会重复出现。
- **连通分量前要先控制 hub 节点**：如果某个相关实体连接了大量实体，可能导致整图被拉成一个大组件；需要先按 hub 度过滤，然后再计算 component。
- **相关实体与实体可能是同一套 ID**：`related_id` 可能是真正的实体 ID（比如某个实体也出现在另一实体的 related_id），也可能是外部实体；在连通分量中这两类都作为节点处理，不需要额外区分。
- **建边前过滤缺失值**：`None` 和空字符串都不能参与 graph；需要显式过滤 `dropna()` 和 `str.strip() != ""`。
- **connected_components 的输出顺序不稳定**：用 `sorted` 排序后再编号只保证相同输入下可复现；成员变化或新增分量会改变编号，不保证跨期身份稳定。
- **关联是候选证据**：主体/资源键使用类型命名空间，避免同字符串意外合并节点；公共资源可能串联大量实体。有效期关系、时间有序路径及共享边重复计量见 [关联契约](relational-contracts.md)。
- networkx 是额外依赖（`import networkx as nx`）；A/B 层纯 pandas，C 层需 networkx。
- 函数中不要写死字段名：用 `df[entity_id_col]` / `df[related_id_col]` 等由 `params` 注入；输出字段使用固定语义名（如 `node_id` / `comp_id` / `shared_count` / `hub_degree`）。
- 不要杜撰不存在的 API；本模式依赖 pandas 与 networkx 的标准方法（`merge` / `drop_duplicates` / `groupby` / `nunique` / `from_pandas_edgelist` / `connected_components`）。

## 扩展（网络算法进阶，暂未展开示例）
- **社区发现**：对较大的图块做更细粒度结构分析，`nx.algorithms.community.greedy_modularity_communities(G)`（networkx ≥ 2.x）可输出每个节点的社区标签。
- **资金环 / 关系环**：用 `nx.DiGraph`（按 `direction_col` 建有向图）和 `nx.simple_cycles` 检测循环关系；注意其复杂度可能呈指数级增长，需设置长度上限和过滤条件。

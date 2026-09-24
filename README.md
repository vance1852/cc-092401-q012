# 人形机器人试验统计准入服务

本仓库是一套已经实现并可直接运行的服务端项目，用于把人形机器人在递送、讲解和灵巧操作等场景中的结构化试验记录转成可复核的统计准入结论。系统以 SQLite 保存机器人、软件构建、不可变协议版本、试验批次、原始观测、排除申请、分析快照、准入决定和审计事件，不依赖机器人设备、图片、音频、视频或外部基础设施。

现有代码按职责分为：

- `api.py`：标准库实现的 HTTP JSON 接口与无网络路由测试边界；
- `service.py`：角色权限、批次状态机、幂等导入、排除复核、任务租约、审批、构建对照和报告；
- `analysis.py`：分层覆盖、Wilson 区间、描述性统计、确定性 bootstrap 和准入规则；
- `comparison.py`：跨批次对照，成功率差与 Newcombe 区间、均值差、Hedges g 效应量、分层确定性 bootstrap 区间和非劣/优效判定；
- `contracts.py`：协议、指标、分层、权重、随机种子、对照规则和单次观测的数据契约；
- `jsonio.py`：严格 JSON/JSONL 读取、规范化序列化与内容摘要；
- `numeric.py`：不依赖第三方库的描述性统计、分位数和 Wilson 区间；
- `storage.py`：完整 SQLite 业务模式、约束、索引和事务辅助；
- `clock.py`：生产时钟与可确定性推进的测试时钟；
- `acceptance.py`：贯通建档、导入、封存、分析、审批、对照和报告的离线验收。

系统已经实现以下主流程：协议发布后不可原地覆盖；批次按版本从草稿进入运行、封存、分析和决定状态；观测分片同时受请求幂等键和来源行唯一身份保护；排除请求必须由不同角色复核；分析任务使用 SQLite 租约避免重复执行并支持过期接管；同一输入快照使用固定算法版本和随机种子得到一致结果；分析者与审批人职责分离，报告保留输入摘要、统计规则和批次审计链。

## 构建对照

统计负责人可以把一个候选构建批次同一个已准入的基线批次做对照，判断候选相对基线是改善还是退化，而不必人工对齐两份孤立报告：

- 两个批次都必须已封存，且协议版本、机器人型号和预注册分层一致，否则拒绝对照；
- 对照只引用双方各自的当前分析版本，并重新校验分析输入快照；引用过期分析版本、分析算法版本不一致，或封存后排除/观测发生变化，都会被拒绝；
- 二元指标给出分层成功率差与 Newcombe 区间，以及分层加权的成功率差和确定性 bootstrap 置信区间；连续与计数指标给出均值差、Hedges g 效应量和确定性 bootstrap 区间；
- 每个指标按预先声明的非劣（`non_inferior`）或优效（`superior`）规则逐项形成结论，差值按指标方向统一换算为“正数代表候选改善”后与界值比较；
- 任一预注册分层在基线或候选中缺样时，对照进入明确的 `insufficient` 状态而不是给出误导性结论；
- 对照结果带独立算法版本写入 `comparisons` 表，相同输入幂等重算返回同一记录，并出现在两个批次的报告中；对照不会改变两个原批次的准入决定。

对照接口：

```bash
POST /comparisons
{
  "baseline_batch_id": "batch-a",
  "candidate_batch_id": "batch-b",
  "baseline_analysis_id": 1,
  "candidate_analysis_id": 2,
  "rules": [
    {"metric": "completed", "rule": "non_inferior", "margin": "0.05"},
    {"metric": "completion_seconds", "rule": "superior", "margin": "0"}
  ]
}
```

`margin` 使用指标原生单位（二元指标为成功率差，连续指标为测量单位，计数指标为次数），缺省为 `0`。新建返回 `201`，相同输入重算返回 `200` 与同一对照编号；`GET /comparisons/{id}` 可按编号读取已封存的对照结果。

## 环境

- Linux
- Python 3.11 或更高版本
- 无需安装第三方 Python 包

如需安装到隔离环境，可在依赖已经准备好的容器中执行：

```bash
python3 -m pip install --no-index --no-deps .
```

## 测试

在 `project/` 目录执行：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

测试使用临时目录和内存数据库，不访问网络，也不依赖常驻服务。

## 构建检查

本项目是纯 Python 源码包，构建检查采用字节码编译：

```bash
python3 -m compileall -q src tests
```

## 无浏览器验收

下面的命令会读取 `fixtures/` 中的协议与观测记录，在临时 SQLite 数据库中完成用户与设备建档、协议发布、批次启动、观测导入、批次封存、任务领取、统计分析、准入审批和审计报告导出，随后输出一行 JSON 结果：

```bash
PYTHONPATH=src python3 -m robot_trials.acceptance --workspace .
```

成功时退出码为 `0`，输出中的 `status` 为 `ok`。验收过程不会写入仓库，也不需要浏览器或外部服务。

## 启动 HTTP 服务

```bash
PYTHONPATH=src python3 -m robot_trials.api --database robot_trials.sqlite3 --host 127.0.0.1 --port 8080
```

接口使用 `X-Actor-Id` 表示当前操作人，写入观测时还需提供 `Idempotency-Key`。正式使用前应先创建操作员、统计负责人、审批人和审计人员，再登记机器人、软件构建与协议版本。服务进程可以停止后重新启动，SQLite 中的业务状态、分析任务和租约信息会保留。

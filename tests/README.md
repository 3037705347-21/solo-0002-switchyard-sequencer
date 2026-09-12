# 关键链路测试（无第三方依赖）

本目录为 switchyard-sequencer 的关键链路单元/服务测试，只使用 Python 3.11
标准库（`unittest`、`tempfile`、`unittest.mock`），不需要 pytest 或其他依赖。

## 运行方式

```bash
# 推荐：仓库根目录执行
python3 tests/run_tests.py            # 运行全部
python3 tests/run_tests.py -v         # 详细输出
python3 tests/run_tests.py validators sequencer   # 只跑指定模块

# 或使用标准 unittest（需保证 src 与 tests 都在路径上）
PYTHONPATH=src:tests python3 -m unittest discover -s tests -p 'test_*.py'
PYTHONPATH=src:tests python3 -m unittest test_validators -v
```

每个用例都在独立的 `tempfile.TemporaryDirectory` 中创建真实的
`YardRepository` / `YardApplication`，**不会读写仓库的 `data/` 目录**。

## 文件与覆盖范围

| 文件 | 覆盖内容 |
| --- | --- |
| `test_validators.py` | 车号/实体代码格式、车型/目的地/危险等级枚举、长度与布尔边界、空/超长编组、重复代码、时间戳、字段错误归属 |
| `test_transitions.py` | 车辆、接入列车、出发列车、拉取任务、班次五张状态转换表的允许与禁止迁移 |
| `test_allocator.py` | 目的地亲和、车型限制、危险等级股道、车数/总长度双重容量、候选排序、部分分类与回滚 |
| `test_sequencer.py` | LIFO 深位拉取的 buffer→pull→return 顺序、同栈多车、跨股道计划、各类排程失败与缓冲容量边界 |
| `test_executor.py` | buffer/pull/return 前置条件、栈顶/缓冲顶校验、容量拒绝、中途失败的内存语义 |
| `test_run_service.py` | 多步推进、提交边界、**中途出错后磁盘保持上次提交、可重试、无事件泄漏**、出发校验 |
| `test_persistence.py` | 工作区编解码往返、原子替换写、失败清理、空目录初始化与种子落盘、JSONL 日志 |
| `test_service_workflows.py` | 四个工作流的服务级端到端、唯一性/冲突/未找到、关闭班次的全部阻塞项与快照 |

## 关于 expected failure（实现与接口约定不一致的证据）

套件中以 `@unittest.expectedFailure` 固定了若干**当前实现不满足规范**的行为。
这些用例运行时显示 `expected failure`，不是回归失败；修复对应业务代码后，它们会
变成 `unexpected success`，提示移除标记。已记录的缺口：

1. **校验错误未进入 `fields`**（`test_validators.py`）
   `ValidationError(message, **{field: [...]})` 把字段问题放进了
   `details`/`payload`，而错误信封的 `fields` 恒为空；嵌套车辆错误甚至两处都没有，
   只剩人类可读消息，客户端无法定位出错字段。
2. **非法时间戳逃逸为裸 `ValueError`**（`test_validators.py`）
   `normalize_iso` 对坏字符串抛 `ValueError`，经 HTTP 边界映射为 500，而非约定的
   `VALIDATION_ERROR` / 422。
3. **部分分类无法重试到 CLASSIFIED**（`test_allocator.py`）
   规范给出 OPEN→PARTIAL→CLASSIFIED 路径，但重分类时已 STANDING 的在编车辆被再次
   记入 `unplaced`，PARTIAL 列车永远无法到达 CLASSIFIED。
4. **维修股道可作为拉取来源**（`test_sequencer.py`、`test_executor.py`）
   规范明确维修股道不能用作 pull source，但排程器与执行器均不检查股道状态。
5. **事件日志读取无法处理多行 JSONL**（`test_persistence.py`）
   `EventJournal.read_all` 对整个文件 `json.load`，日志超过一行即抛
   `JSONDecodeError`（当前生产代码只追加不读取，属潜伏缺陷）。

此外，部分用例以普通断言固定了**实际行为**（例如 `PLANNED→DRAFT` 在转换表中
被允许、失败的推进不会把 run 置为 FAILED 而是保留 RUNNING 可重试），这些行为
与规范文字有出入但当前设计自洽，仅作为证据记录，不修改业务规则。

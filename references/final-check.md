# 最终复查与台账

用于阶段合并、完整收口和已授权的历史清理，在实际操作后运行。清理前仍按 cleanup-check.md 的证据与应用入口规则执行。这个工具不删除任何对象，也不认证验收、授权、备份或聊天占用；这些结论来自真实记录。完整收口必须同时按 [全目录空间与历史材料收口](storage-cleanup.md) 通过独立的全目录检查；本工具的 complete 仅覆盖 Git 与声明的运行资料，不能单独代表全部空间已收口。

项目级复查与容量总表由 [统筹聊天](default-behavior.md#单窗口统筹与必要协作) 统一完成，允许的跨目录收尾由当前窗口直接执行，不默认发消息。工具权限只阻塞其准确对象；仅在另有明确协作授权时补入原聊天动作结果，不要求每个原聊天重新运行整套收口。复用验收不省略本页的实时最终核验。

## 台账

将 JSON 台账保存到清理目录之外，作为检查输入；核验结束后的去留按 [控制本轮新增材料](storage-cleanup.md#控制本轮新增材料) 处理，用户结果按 [聊天输出格式](../SKILL.md#最终回复直接在聊天中展示) 展示，不另生成报告副本。用操作前的完整盘点记录原始头，用操作后的真实查询填写结果。每个候选都包含 `local` 与 `remote`；不存在的引用也须记录，避免遗漏。

```json
{
  "schema_version": 2,
  "mode": "full",
  "scope": "selected",
  "base": {"ref": "origin/main", "branch": "main", "head": "完整主线提交"},
  "main_path": "/absolute/main-checkout",
  "candidates": [
    {
      "branch": "dev/task",
      "local": {"head": "清理前本地头", "outcome": "removed"},
      "remote": {"head": "清理前远端头", "outcome": "removed"}
    }
  ],
  "directories": [
    {"path": "/absolute/task-worktree", "head": "清理前目录头", "outcome": "removed"}
  ]
}
```

- `mode`：完整收口/历史清理用 `full`，本批交付后继续开发用 `stage`。结果中的 mode 必须随完成结论报告，避免把阶段完成说成整体结束。
- `scope`：单独 `$shoukou` 默认使用 `selected`，只核对当前任务引用；用户明确要求项目级或历史收口时才用 `all-task-branches` 核对全部本地、远端引用的去向。项目级审计包括受保护和待确认引用，不因此授权修改它们。项目级模式会检查全仓引用遗漏及操作期间新增引用；新增对象单独核对，不自动删除。
- 引用结果：`removed` 表示本次实际删除，保留原头；`absent` 表示操作前已经不存在，`head` 为 `null`；`retained` 表示保留，必须包含原头、`reason` 和 `retention`。
- 目录结果：`removed` 或 `retained`；保留时必须记录当前头、`reason` 和 `retention`。主目录不列入清理目录。无独立工作树时用空列表。
- `retention`：`agreed` 只用于用户约定或本次已确认范围本就要求的保留；`blocked` 用于必要清理因占用、资料、工具或证据缺口未完成。完整收口不能通过把 `blocked` 改成 `agreed` 得到通过。

`mode: full` 中每个 `retained` 引用还须按 [遗留分支逐项取证](branch-cleanup.md#遗留分支逐项取证) 填写 `review`；原有/新增、本地/远端分别绑定准确头。检查器核对任务、成果及分支依赖记录、阻碍事实和后续操作/位置/解除条件，并在每个引用结果的 `review` 中报告主线祖先关系。缺少这些记录的旧台账不能通过，补查真实来源后重验；阶段合并保持原有语义。目录保留理由不能代替引用记录，Git 祖先关系不代表未提交成果或功能已交付。

项目级保护有明确继续开发约定的任务或政策要求的长期引用时，记为 `retained/agreed` 并提供真实来源；未知任务仍为 `blocked`。项目级结果通过仅表示所选已结束任务已收口、其他对象按依据保护，不代表全项目任务结束。完整处理边界见 [默认范围与保护边界](default-behavior.md)。

项目级完整收口还须加入 `branch_inventory`，用首次 Git/清理操作前的原始引用清单与结束时的引用比对：

```bash
python3 scripts/verify_closeout.py --repo /absolute/repository --capture-branches
```

将该输出保存为目录外的 `branches-before.json` 并计算 SHA-256，作为下列台账输入；示例没有新增分支，`introduced` 两张表为空：

```json
{
  "branch_inventory": {
    "before_inventory": "/absolute/outside-target/branches-before.json",
    "before_sha256": "原始分支清单的SHA256",
    "introduced": {"local": {}, "remote": {}}
  }
}
```

新增分支在相应 local/remote 表中以准确分支名为键，值包含 `head`（本次核验的准确头）、`state` 和 `source`（实际任务状态来源），同时正常列入 `candidates`。`continuing`、`long-lived` 只可对应 `retained/agreed`；`paused` 可按明确保留约定或未解除的阻碍对应 `retained/agreed` 或 `retained/blocked`；`unknown` 只可对应 `retained/blocked`；`completed` 可对应已实际删除，或因具体阻碍 `retained/blocked`。新增不能作为自动删除许可。初始清单中已不存在的旧分支仍必须进入台账；不能删除后从台账省略。

缺少初始清单的旧台账仍可复查引用现状，但项目级完整结果为 `partial`；摘要损坏、归属不同、漏列原始/新增分支、状态与去向矛盾均拒绝。`branch_changes` 单独列出新增分支；核验期间引用变化同样拒绝，重新审阅真实变化，不改写初始清单。[旧目录重复内容的例外](branch-cleanup.md)另须通过分支检查的 `passed` 和 `preserved` 两阶段，两者都不代替目录及运行资料检查。

上例只展示 Git 部分；还须按 [运行资料分类与复查](runtime-cleanup.md) 加入 `runtime`，覆盖本任务主目录及全部声明目录的忽略数据、临时服务和前后占用。工作树保留时同样核对其内部数据，不以目录未删跳过此项。旧版台账缺少该项时结果为 `partial`。

## 任务级空间与台账接入

先按 [台账接入](artifact-ledger.md) 读取当前任务，台账的 ready-for-closeout 和读取器的 ready-for-review 都不等于交付或清理完成。日常 Git 盘点带 `--worktree`，空间 capture 也带同一准确 `--worktree`；storage review 必须写 `scope: selected` 和该目录的 `worktree`。验证结果只证明所选范围，不报告整个项目已清理。项目级完整收口仍使用原全项目 capture 和原始分支清单，不能用任务级输出替代。

## 未完成工作与真实阻碍

`closeout`/`status` 描述已声明处置的核验结果；新增的 `execution` 描述还有没有工作应继续做。两者一起读取，单独的 `partial` 或 `blocked` 不再作为结束本轮的理由：

| execution.state | 含义与动作 |
| --- | --- |
| `continue` | `unfinished` 列出尚未完成的对象、绑定版本及下一项操作；原授权内继续处理。缺少运行资料、未知模板、缺少分类或停点证据不成立均在此列。 |
| `blocked` | 当前未解决对象都有完整停点记录，列在 `blocked`；执行者读取原件核对其真实影响和授权后才可报告受阻，不因此删除对象。 |
| `complete` | 本检查没有待办或停点；仍须满足另一项检查、验收和应用关联条件。 |

`verify_closeout.py` 的运行资料、原始分支清单等缺项继续保留原有未评估标记，同时进入执行待办。`storage_audit.py verify` 缺少目录或条目分类时返回 `status: incomplete`、明确的分类待办及退出码 3；按顶层材料组汇总缺项数量和少量样本，避免输出整份文件清单。覆盖重叠、目录兜底规则、坏摘要等无效输入仍拒绝，不能借待办状态放宽检查。

只有真实受阻时，在对应 ledger 或 storage review 顶层的可选 `stops` 中填准确对象键；键与 `execution.unfinished[].object` 完全一致。示例：

```json
{
  "stops": {
    "local:dev/task": {
      "version": "该引用的完整头提交",
      "kind": "user-pause",
      "attempt": "读取原任务中明确的暂停约定",
      "result": "该任务暂停，未提交成果和失败现场须保留",
      "release_condition": "用户明确恢复该任务且相关门禁允许",
      "evidence": {"path": "/absolute/original-pause-record.json", "sha256": "原始记录SHA256"}
    }
  }
}
```

`version` 必须原样使用待办中的版本：分支为准确头，目录为其 HEAD，其余 Git/运行待办为本次目标基线头；全目录待办为原始容量盘点文件摘要。`kind` 仅接受 `user-pause`、`app-protection`、`query-error`、`scope-boundary`、`external-dependency`、`acceptance-failure`；记录实际尝试、结果与可检查的解除条件。验收失败在原任务范围内可以修复时仍继续；只有真实范围/暂停/外部约束阻止继续时才使用停点。

证据指向已有的、非空普通文件原件及准确摘要，可复用同一真实约定覆盖多个明确列出的对象，不默认复制全部聊天或制作新报告。无记录、符号链接、摘要或版本不符的停点回到待办；已无对应待办的旧停点须从当前台账移出，原始记录继续按需要保留。一个对象的有效暂停不能覆盖其他对象的未完成工作。

脚本只验证原件存在、字节摘要和对象/版本绑定，无法认证记录内容、用户意图或实际尝试；执行者必须读取原始来源，不能写一份自述当成外部失败。真实查询错误应保留原始错误结果；输入错误不自动证明外部阻碍。任何执行状态都不授予删除、恢复暂停任务、跨聊天消息或绕过安全审查的权限。

## 运行与解释

```bash
python3 scripts/verify_closeout.py --repo /absolute/repository --ledger /absolute/outside-target/ledger.json
```

工具实时查询 `origin`，核对远端目标与台账、本地目标引用、本地主目录分支/提交/改动、本地与远端引用、工作树目录和注册；另核对运行资料完整分类、数据去留、外置副本摘要和新鲜服务观察，并报告目标数据占用及磁盘可用空间变化。目录消失但 Git 注册仍在、远端已删但本地仍在、远端新增头、本地主线落后、有未说明改动、漏报数据或仍有声称已停止的进程都不能通过。

| 结果 | 退出码 | 结论 |
| --- | --- | --- |
| `complete` | 0 | 台账与真实状态一致，且没有声明的清理阻碍；仍需结合该 mode 的验收和应用关联证据。 |
| `partial` / `blocked` 且 execution 为 `continue` | 3 | 继续完成列出的核查、处理或输入修正，不能仅凭“部分完成”结束。 |
| `partial` / `blocked` 且 execution 为 `blocked` | 1 | 有逐对象停点证据；核对真实来源后报告部分完成及准确阻碍。 |
| 输入/查询错误 | 2 | 查询失败、目标不明或台账无效；保留现场，不能宣称完成。 |

网络查询失败不能用缓存引用替代通过。头变化时重新核验交付和范围；不要为了符合旧台账重置 Git。输出记录的是当前状态，不能由一次通过推定后续状态不会改变。

## 技能验证

运行 `tests/test_inspect_repo.py`、`tests/test_verify_closeout.py`、`tests/test_storage_audit.py`、`tests/test_branch_cleanup.py` 、`tests/test_closeout_progress.py`、`tests/test_artifact_handoff.py` 和 `tests/test_task_lifecycle.py`。测试在隔离临时仓库中验证清理前拒绝与清理后复查，包含能真实失败的反例：未提交成果、过期/错误 PR、聊天仍占用、忽略文件遗漏、备份损坏、残留引用、主线不同步、远端新头、目录与注册残留、缺少运行资料记录、数据假删除、未知数据误分类、存活进程假退出、查询失败和部分完成的非零退出码。另覆盖准确路径的历史版本、独有及暂存内容保护、未提交文件原样保留、原始/新增分支漏列、并发新增引用和新任务保护。

在线保留测试还覆盖日志正常追加、缓存原子更新，以及文件消失、日志截断或替换、无关变化、目录级声明、过期观察、进程退出/签名不符和来源变化的拒绝。`dynamic_observations` 只表示区间内有依据的在线保留观察；不能把通过结论写成静态快照或逐字节保全证明。

另覆盖原有分支只填保留原因、目录异常误当引用依赖、结束/未知任务伪装约定保留、版本或来源缺失、未提交状态矛盾、已解除阻碍，以及继续开发与真实验收失败的保护。测试通过只说明脚本行为符合这些用例，不证明真实任务已清理。修改技能后还运行 skill-creator 的 quick_validate.py，并核对新增引用可访问、原有策略与未提交修改得到保留。

单窗口限定例外的执行验证从统筹目录启动 CLI，对多个注册工作树运行前置检查、原 HEAD 分离、已合并引用删除及 `preserved` 复查，核对文件、索引、未提交状态、忽略资料和目录仍在；另验证其他仓库和未合并头被拒绝。该隔离验证不证明真实项目的任务归属、写入者调查或聊天保护已经通过，也不证明未来代理必定执行所有步骤。

## 纯本地仓库

只有 `git remote` 为空时可在 `base` 明确声明 `location: "local"`、`ref: "main"`（与 branch 相同）及准确本地主线 head。候选 remote 必须为 `{"head": null, "outcome": "absent"}`。脚本验证主线、工作目录及本地分支，输出 remote main 为 null；不宣称远端交付。默认仍为 remote 模式，已配置任何远端时拒绝本地模式，远端查询失败不能绕过。

引用已删除但目录因应用占用查询不足保留时，台账记录 local removed、directory retained/blocked，结果应为 partial；它证明引用已清理，不证明目录可删除。

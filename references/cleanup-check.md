# 清理证据与检查

此检查仅用于本次已授权移除的工作树，不用于删除本地项目主目录或推定其他分支也获授权。脚本不执行任何删除、提交、获取、归档或推送。

默认保留聊天历史不要求恢复历史任务环境，也不放宽本检查的关联保护。无法满足应用入口、附件、占用或资料条件时，只保留受影响目录并报告阻碍；不为完成收口反复恢复原聊天、重建目录或迁移日常环境。具体边界见 [默认范围与保护边界](default-behavior.md)。

## 采集与执行

1. 获取并验证准确目标基线后，运行 `scripts/inspect-repo.sh --base origin/main --json`。从目标工作树记录 `path`、`branch`、`head`、`fingerprint`，从顶层记录 `base_ref`、`base_head`。基线引用必须与交付验收使用的版本相同。若 squash/rebase 合并不满足祖先关系，脚本只接受同一分支、准确 `headRefOid`、正确目标分支、且合并提交已进入基线的 PR；查不到准确证明时保留，不能仅凭同名历史 PR 删除。
2. 用受支持的应用查询核对目标路径的已知关联聊天、内置移交结果及工作树附件。先定位原任务，再核对其他已知关联聊天和空闲聊天。完整查询路径只有实际查询完整时才填 `complete: true`；只查最近 50 个聊天、遗漏归档或关联信息、仅搜进程均不足。对有明确共享保护的原生归档，可按下文单独路径填写，保留 `complete: false`；其余查询不足仍保留目录。
3. 核对进程/服务占用及本任务验收和清理授权。分类每一个被忽略文件：需要保留的备份到目标目录之外并逐文件核验；可重建缓存填写实际依据。不能把整个 `.runtime` 默认当缓存。需要保留的符号链接、子模块或嵌套仓库需单独保全，当前检查不支持这些保留方式；可重建依赖内的忽略符号链接可以随已分类的缓存丢弃。
4. 将以下证据保存到目标目录之外；时间填本次观察的 Unix 秒数。检查仅接受最近 120 秒的观察，并核对当前 Git、基线和文件清单。运行检查并读取完整结果，失败退出码为 1，输入/查询错误为 2。

```json
{
  "schema_version": 1,
  "observed_at_unix": 0,
  "target": {
    "path": "/absolute/worktree",
    "head": "full-commit-sha",
    "branch": "dev/task-branch",
    "fingerprint": "inventory-fingerprint",
    "base_ref": "origin/main",
    "base_head": "full-base-commit-sha"
  },
  "cleanup_authorized": false,
  "task_delivery": {"accepted": false, "head": "full-commit-sha"},
  "app": {
    "source": "实际使用的应用查询及结果记录位置",
    "complete": false,
    "chats": [
      {"id": "真实聊天标识", "cwd": "/absolute/current-directory", "archived": false}
    ]
  },
  "processes": {"source": "实际进程/服务查询及结果记录位置", "complete": false, "active": []},
  "ignored": {
    ".runtime/evidence.log": {
      "action": "preserve",
      "reason": "本任务验收原始日志",
      "backup": "/absolute/outside-target/evidence.log",
      "sha256": "source-and-backup-sha256"
    },
    "node_modules/": {"action": "discard", "reason": "可从锁文件重建的依赖缓存"}
  }
}
```

`ignored` 必须完整覆盖目标的忽略文件清单；可用以 `/` 结尾的相对目录前缀分类整组可重建缓存（例如 `node_modules/`），其余使用准确相对文件名。每个文件只能匹配一个规则；遗漏、重叠、无对应文件的规则均拒绝。盘点中的 `ignored_groups` 帮助定位目录组，但检查仍核对完整文件清单。没有忽略文件时填写 `{}`。目录规则只支持 `discard`；需要保留的内容逐文件备份。备份当前仅支持目录外的普通文件副本，压缩包中的条目不能直接作为 `backup`；先在目标目录外恢复所需副本并验证。源码仍有已暂存、未暂存或未跟踪改动时，即使备份完整也拒绝正常清理。

`task_delivery` 由实际验收记录支持，不能因为分支头在主线就填通过。`app` 与 `processes` 的完整性来自原始查询，不是脚本自动识别；证据声明可以被伪造，所以通过结果不是应用侧占用证明或安全边界。填写所有已知目标关联聊天；仍需使用的聊天不得指向目标目录，其新目录必须实际存在。归档历史可以保留旧路径；重新打开时需依靠内置恢复。

检查通过后立即使用受支持的清理入口。出现写入、基线变化或人员/会话状态变化时重新采集，不使用旧结果。操作后复查目录去留、Git 工作树注册、备份和继续使用聊天的位置；通过检查不能被写成“目录已删除”。

## 受保护原生归档路径

此路径仅支持当前启用的 `mcp__codex_app__archive_worktree`。其工具契约声明：拒绝主目录、固定或共享工作树，移除前保留可恢复 Git 快照；初始化子模块或嵌套仓库也会拒绝。一般产品说明见 [官方工作树文档](https://developers.openai.com/codex/app/worktrees/)；共享拒绝的具体依据是本次启用的工具契约，不能将文档中的自动回收描述扩展为所有删除接口都有相同保护。

先在统筹聊天按 [单窗口执行与协作边界](default-behavior.md#单窗口统筹与必要协作) 查明目标、已知阻碍和可行的解除路径。当前聊天无权使用目标附件、接口只允许原聊天调用且没有跨聊天消息授权时，保留该目录并记录准确能力缺口，继续独立分支和临时批次处理；不擅自委派。仅在用户另行明确授权协作时，才考虑由持有附件的原聊天执行这一具体回收动作；原聊天仍关联目标等条件未解除时，不为重做相同检查而唤醒它。执行时重新查询 `list_artifacts` 和 `read_thread`，保存原始响应，复用仍适用的验收证据和材料分类，只补充本动作必要的新鲜观察。准确 identityKey 必须对应同一目标目录，原聊天必须已经位于另一个存在的目录；查询到其他仍使用目标的聊天时保留。原生接口承担无法完整枚举的共享保护；最近聊天列表仍如实填不完整，不是发现了“无人使用”。如果本次工具契约没有明确的共享拒绝，使用完整查询路径或保留目录。

将完整证据中的 `app` 替换为下例，其余验收、基线、指纹、授权、进程和忽略资料检查不变：

```json
{
  "source": "本次read_thread和list_artifacts原始查询记录位置",
  "mode": "native-archive",
  "complete": false,
  "chats": [
    {"id": "原聊天真实标识", "cwd": "/absolute/current-main-directory", "archived": false}
  ],
  "archive": {
    "tool": "mcp__codex_app__archive_worktree",
    "identity_key": "原聊天list_artifacts返回的准确identityKey",
    "target_path": "/absolute/target-worktree",
    "owner_thread_id": "原聊天真实标识",
    "current_thread_id": "正在调用的同一个原聊天标识",
    "contract_source": "本次实际启用工具的完整契约记录位置",
    "attachment_source": "本次list_artifacts原始响应记录位置"
  }
}
```

结果必须是退出码 0、`cleanup_preflight: native-archive-ready`，且输出的 `required_native_action` 与本次附件一致。它仅表示可以请求该原生接口执行受保护归档，不能用于 `git worktree remove` 或手工删除。脚本不认证调用者填写的附件/契约；代理核对实际原始结果，原生工具另行执行自身保护。接口拒绝、失败或结果不明时保留并记录，不改字段、重建附件或切换删除手段来绕过。接口成功后仍复查实际目录、Git 注册、原聊天当前位置、恢复快照及备份；随后按分支独立清理规则核验本地引用。

## 校验性能

忽略文件分类按准确文件名和目录祖先查询规则；每个文件仍必须恰好匹配一条规则，目录规则只可丢弃可重建缓存，保留资料仍逐文件校验原件与外置副本。遗漏、重叠、无对应文件、路径越界和坏备份继续拒绝。大量依赖文件不再逐一遍历全部分类规则；性能测量需区分分类耗时与完整指纹/备份检查耗时。

## 验证

在隔离的临时仓库运行 `python3 tests/test_inspect_repo.py`。测试覆盖：未提交成果、错误/过期 PR、准确 squash 证明、完整查询缺失、原生附件/工具/目标/调用者绑定错误、已知聊天或进程仍占用、证据/基线变化、分类遗漏/重叠、坏备份、大文件清单，以及原生就绪状态绑定特定工具且不执行删除。隔离测试不能证明应用后端实际拒绝共享目录；该保护由当前接口契约及真实调用结果支持。所有测试仅清理由测试创建的临时仓库，不对真实任务执行删除。

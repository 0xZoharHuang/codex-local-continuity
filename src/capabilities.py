"""User-facing account boundaries. This module does not call any service."""
ROWS = (
    ("folders", "Local folder registrations and project order", "本地文件夹登记与项目顺序", "setting", ("folders","folders-history","projects","both"),
     "manage.py settings"),
    ("projects", "Explicit chat memberships and exclusions", "明确的聊天分组与无项目选择", "setting", ("projects","both"),
     "manage.py settings"),
    ("recall", "Per-chat sent-prompt recall (↑)", "每条聊天已发送输入（↑）", "setting", ("history","folders-history","both"),
     "manage.py settings"),
    ("plugins", "Local plugins", "本地插件", "local-audit", None, "manage.py doctor show"),
    ("schedules", "Local scheduled task definitions", "本机定时任务定义", "local-audit", None,
     "manage.py doctor compare"),
    ("pins", "Pinned chats, custom sections and read state", "置顶、侧栏自定义分区与已读状态", "native", None,
     "Codex → sidebar / chat menu"),
    ("chats", "Transcript, title and archive", "聊天正文、标题与归档", "native", None, "Codex → chat menu"),
    ("drafts", "Drafts, queued input and active turns", "草稿、排队输入与进行中回合", "native", None,
     "Codex → current chat"),
    ("models", "Model availability and usage", "模型可用性与用量", "account", None, "Codex → active account / model picker"),
    ("connections", "External connections and workspace access", "外部连接与工作区授权", "account", None,
     "Codex → Plugins / workspace administration"),
    ("permissions", "Tool approvals and folder permissions", "工具审批与文件夹权限", "native", None,
     "Codex → permissions for the current chat"),
)


def report(mode=None, lang='en'):
    result = []
    for key, en, zh, operation, modes, action in ROWS:
        status = ("unconfigured" if mode is None else
                  "enabled" if mode in modes else "disabled") if modes else None
        result.append({"id":key, "feature":zh if lang=="zh" else en, "control":operation,
                       "setting":status, "action":action})
    return result

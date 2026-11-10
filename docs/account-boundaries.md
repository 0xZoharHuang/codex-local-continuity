# What follows the account?

Three questions have different answers: can the sidebar list a project, can the client read the local transcript, and can the active account start a new turn with the same tools? The first is the problem this helper addresses. The table gives developers a way to investigate the other two without assuming a shared file implies shared authorization.

| Feature | What to check after switching | Helper writes |
| --- | --- | --- |
| Local project sidebar, manual assignment, explicit ungrouping | On the tested desktop build, the account/workspace pair selects a saved renderer view. The same machine can have valid local project records while the active Projects section is empty. Compare both views and keep the original IDs. | `folders` shares plain registrations, local ID mappings and project order. `projects` also shares explicit memberships/exclusions and manual chat order. No mode grants folder access. |
| Empty-composer ↑ | The documented shortcut restores a previous prompt. The tested build keeps a bounded input cache per profile and chat, distinct from the transcript. An editable queued message can take priority. [Desktop shortcuts](https://learn.chatgpt.com/docs/reference/commands) | Eligible per-thread text, with separate opt-in |
| Pinned chats, custom sections, read/unread | A pin or custom section can have separate sidebar state. The tested local database also has read receipts. Compare these in the native sidebar; a matching project list does not imply that these choices match. | None |
| Transcript and continuation | `thread/read` reads metadata. `thread/turns/list` and `thread/items/list` page history. `thread/resume` can reopen an existing thread, and `turn/start` initiates new work. Reading old turns does not prove that new work or an old attachment is authorized. `turn/steer` targets a particular in-flight turn. [App Server](https://learn.chatgpt.com/docs/app-server) | None |
| Identity, limits and model choices | `account/read` describes the effective app-server account. `account/rateLimits/read` and `account/usage/read` have account-specific results. `model/list` is the relevant model catalog for the connection; plan, workspace and provider requirements can affect availability. Browser sign-in can return credentials to the desktop client while the web app keeps its own browser session. [App Server](https://learn.chatgpt.com/docs/app-server) · [Authentication](https://learn.chatgpt.com/docs/auth) | None |
| Plugins, skills and MCP | A package on disk, a plugin visible in the workspace catalog, and an authorized external connection are three different states. Compare in the same project/host and in a new chat. `skills/list` is sensitive to working directories. `mcpServer/oauth/login` is a distinct authorization flow. The current docs mark `plugin/list` as under development. [Plugin permissions](https://learn.chatgpt.com/docs/plugins#how-permissions-and-data-sharing-work) · [App Server](https://learn.chatgpt.com/docs/app-server) | None |
| Scheduled tasks | Some tasks return to an existing chat. Definitions, visibility, eligibility and observed execution are separate checks. Compare the task ID, target, enabled state and a naturally scheduled run. Copying a definition to every profile could run an action more than once. [Scheduled tasks](https://learn.chatgpt.com/docs/automations) | None |
| Approvals, remote hosts and managed workspaces | Access to a local directory, approval to execute a tool and authorization to use a remote service are independent of sidebar membership. Check the new identity and its permissions when continuing work. [Permissions](https://learn.chatgpt.com/docs/permissions) | None |

## Use the comparison on your Mac

`manage.py capabilities --json` labels each area as a configurable sharing setting, a local audit, an account feature or a native desktop action. `manage.py status --json` shows the saved scope, feature mode and listener state. Run these commands with `/usr/bin/python3 -I -B src/manage.py` from the checkout.

| Before switching | After switching | How to interpret the result |
| --- | --- | --- |
| In profile A run `doctor baseline`. | In B run `doctor compare`. | Check the reported identity relation first. A missing folder or unsupported local format is an unknown, not an empty inventory. |
| Look at a local plugin in the **Installed** view; record whether it is connected. | Use the same desktop mode, host and working directory; open a new chat and check the same plugin. | An enabled local entry and cached manifest do not establish workspace entitlement or OAuth access. Reconnect using B's own native authorization if needed. |
| Pick an existing scheduled task and note its target chat and enabled/paused state. | Compare its local definition, its visibility in **Scheduled** and, later, an ordinary due run. | Definition, scheduling eligibility and execution are distinct observations. The helper never clones tasks, updates ownership, or triggers a run. |
| Record the model available to the active connection. | Check the native model picker and the active account/workspace. | Model availability and quota depend on the service and active identity. The helper provides no switches for these. |

## Evidence of demand

Developers have reported local sidebar organization disappearing on a Mac after switching accounts, and a similar symptom on Windows. Another reporter wanted *stronger* isolation between a personal account and a business account. These reports support an explicit choice on a trusted device; they do not measure how common the problem is or establish one cause for every report.

- [Request for configurable local project visibility](https://github.com/openai/codex/issues/60337)
- [Local projects and thread assignments disappear after switching](https://github.com/openai/codex/issues/60364)
- [Windows report](https://github.com/openai/codex/issues/59895)
- [Request for stricter personal/business separation](https://github.com/openai/codex/issues/57785)

A [folder-only Windows report](https://github.com/openai/codex/issues/60223) and a [request involving two personal profiles](https://github.com/openai/codex/issues/60509) illustrate why scope and content are separate choices. The upstream [contributing guide](https://github.com/openai/codex/blob/main/docs/contributing.md) asks contributors to add new evidence to an existing matching issue.

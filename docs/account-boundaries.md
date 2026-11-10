# What follows the account?

Three questions have different answers: can the sidebar list a project, can the client read the local transcript, and can the active account start a new turn with the same tools? The first is the problem this helper addresses. The table gives developers a way to investigate the other two without assuming a shared file implies shared authorization.

| Feature | What to check after switching | Helper writes |
| --- | --- | --- |
| Local project sidebar, manual assignment, explicit ungrouping | On the tested desktop build, the account/workspace pair selects a saved renderer view. The same machine can have valid local project records while the active Projects section is empty. Compare both views and keep the original IDs. | Selected local sidebar fields |
| Empty-composer ↑ | The documented shortcut restores a previous prompt. The tested build keeps a bounded input cache per profile and chat, distinct from the transcript. An editable queued message can take priority. [Desktop shortcuts](https://learn.chatgpt.com/docs/reference/commands) | Eligible per-thread text, with separate opt-in |
| Transcript and continuation | `thread/read` reads metadata. `thread/turns/list` and `thread/items/list` page history. `thread/resume` can reopen an existing thread, and `turn/start` initiates new work. Reading old turns does not prove that new work or an old attachment is authorized. `turn/steer` targets a particular in-flight turn. [App Server](https://learn.chatgpt.com/docs/app-server) | None |
| Identity, limits and model choices | `account/read` describes the effective app-server account. `account/rateLimits/read` and `account/usage/read` have account-specific results. `model/list` is the relevant model catalog for the connection; plan, workspace and provider requirements can affect availability. Browser sign-in can return credentials to the desktop client while the web app keeps its own browser session. [App Server](https://learn.chatgpt.com/docs/app-server) · [Authentication](https://learn.chatgpt.com/docs/auth) | None |
| Plugins, skills and MCP | A package on disk, a plugin visible in the workspace catalog, and an authorized external connection are three different states. Compare in the same project/host and in a new chat. `skills/list` is sensitive to working directories. `mcpServer/oauth/login` is a distinct authorization flow. The current docs mark `plugin/list` as under development. [Plugin permissions](https://learn.chatgpt.com/docs/plugins#how-permissions-and-data-sharing-work) · [App Server](https://learn.chatgpt.com/docs/app-server) | None |
| Scheduled tasks | Some tasks return to an existing chat. Definitions, visibility, eligibility and observed execution are separate checks. Compare the task ID, target, enabled state and a naturally scheduled run. Copying a definition to every profile could run an action more than once. [Scheduled tasks](https://learn.chatgpt.com/docs/automations) | None |
| Approvals, remote hosts and managed workspaces | Access to a local directory, approval to execute a tool and authorization to use a remote service are independent of sidebar membership. Check the new identity and its permissions when continuing work. [Permissions](https://learn.chatgpt.com/docs/permissions) | None |

## Evidence of demand

Developers have reported local sidebar organization disappearing on a Mac after switching accounts, and a similar symptom on Windows. Another reporter wanted *stronger* isolation between a personal account and a business account. These reports support an explicit choice on a trusted device; they do not measure how common the problem is or establish one cause for every report.

- [Request for configurable local project visibility](https://github.com/openai/codex/issues/60337)
- [Local projects and thread assignments disappear after switching](https://github.com/openai/codex/issues/60364)
- [Windows report](https://github.com/openai/codex/issues/59895)
- [Request for stricter personal/business separation](https://github.com/openai/codex/issues/57785)

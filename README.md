# Codex Local Continuity

A macOS helper for people who use more than one Codex desktop profile on a trusted Mac. It carries local project organization between the profiles you select. You can separately opt in to sharing eligible sent prompts, so pressing **↑** in an empty composer can recall text from the **same local chat** after switching accounts.

For example, you move a chat into a project under profile A, sign into B, then quit the desktop app as usual. After the next launch, B can see the move in its native sidebar. The helper has no window. It listens for a desktop exit, checks that the app is offline and merges local metadata. A rapid relaunch can defer the merge until a later exit.

This is an independent, experimental tool. It uses an undocumented desktop storage format. A future release can change the format or apply different visibility rules. [中文说明](docs/zh-CN.md) · [Account and API behavior](docs/account-boundaries.md) · [Verification](docs/verification.md)

## Try it without installing

Requires macOS, the standard user-owned `~/.codex` layout, and a desktop profile initialized by a normal sign-in. The installer recognizes one `Codex.app` or `ChatGPT.app` with bundle ID `com.openai.codex` in `/Applications` or `~/Applications`. Custom `CODEX_HOME` locations are unsupported. The tested desktop build is **26.1103.60933 (25894)** on macOS 15.5, Apple Silicon. Building the watcher requires Swift and system Python via the Xcode Command Line Tools.

```sh
git clone https://github.com/0xZoharHuang/codex-local-continuity.git
cd codex-local-continuity
/usr/bin/python3 -I -B src/manage.py profiles
/usr/bin/python3 -I -B src/manage.py preview --profiles existing
```

`profiles` shows opaque local labels and identifies the active profile. To limit sharing to two identities, pass their comma-separated labels in place of `existing`. **`existing` freezes the set of initialized profiles. `all` also enrolls future profiles**, including a work account you may sign into later. Choose the group whose members should see the same project names, local paths and, if enabled, eligible prompt text.

The preview reports counts and does not register a listener or update the desktop configuration. To install for the profiles currently present:

```sh
/usr/bin/python3 -I -B src/manage.py install --profiles existing
```

For sidebar **and** per-chat input recall, add `--features both` to the preview and install commands. For recall alone, choose `--features history`. The previous `--include-history` spelling still means `both`. The service normally needs one full desktop exit before its first merge. Later edits stay in the active profile until another offline sync. The helper never logs in, selects accounts, reads quotas or sends a model request.

## Change your choices after installing

Double-click [settings.command](settings.command) in Finder, or run `/usr/bin/python3 -I -B src/manage.py settings` in Terminal. The English/Chinese editor lists the active profile, lets you pick the sharing group, previews the effect and asks before saving. You can change these helper settings while Codex is open. Changes to the native sidebar or composer wait for the next offline sync.

| You want to… | Setting or action |
| --- | --- |
| Register the same folders but keep manual chat groupings personal | `folders` |
| Do that and recall eligible per-thread inputs | `folders-history` |
| Share folders and explicit chat groupings but keep typed prompts separate | `projects` |
| Recall inputs for the same chat but keep different project layouts | `history` |
| Use both across the chosen profiles | `both` |
| Pause future sharing while keeping the helper installed | `off` |
| Find out whether a plugin or scheduled definition changed locally | Run the local comparison below |

For example, this previews a change to input recall alone, then saves it for the **already enrolled** group:

```sh
/usr/bin/python3 -I -B src/manage.py preview --features history
/usr/bin/python3 -I -B src/manage.py configure --features history
```

In folder-registration mode, Codex may still group already accessible local chats by their working directory. That mode never transfers explicit chat memberships, exclusions or folder permissions. Add `--profiles` to change the group as well. Saving a new policy does not erase earlier copies. If B already received a prompt from A, B still has that text; enrolling B and C together later can make eligible entries in B available to C. Review the groups before doing that. Undo any earlier sync you intend to reverse **before** changing the policy. The helper retains old private recovery data separately and does not apply it under the new consent.

## Check a suspected difference after a switch

```sh
/usr/bin/python3 -I -B src/manage.py doctor baseline   # once, with A active
# Sign in to B through the desktop app as usual.
/usr/bin/python3 -I -B src/manage.py doctor compare
```

The comparison reports whether the active local identity changed and how local plugin or scheduled-task definitions differ. A check with no usable data is reported as unknown. A matching disk inventory alone cannot confirm that the new account can use a connector or that a job ran. Use `doctor show` for a one-off report; use `doctor baseline --replace-baseline` only when you intend to replace the reference. The exit listener does not run these diagnostics.

Run `src/manage.py capabilities` with system Python to see the controls and native destinations for each feature, or see the [account behavior table](docs/account-boundaries.md).

## What gets shared

| Data | Behavior |
| --- | --- |
| Local projects and memberships | Reuses existing project and thread IDs. Carries supported renames, order, manual moves and explicit ungrouping. Unilateral edits propagate. Conflicting edits are retained for resolution. An incomplete profile is not evidence that the user deleted all projects. |
| Composer ↑ history, when enabled | Merges eligible sent text for the same local thread. Respects the native 20-entry window. Initial caches do not provide cross-profile timestamps, so a destination keeps its existing relative order. Rich content is copied only when its text can be reproduced without opaque references. |
| Transcripts, drafts, attachments and queued submissions | The helper does not rewrite transcripts or transfer unsent work. A queued submission could otherwise execute twice. |
| Account, workspace and tools | Model availability, billing, credentials, plugin connections, grants and schedules stay with their native systems. The helper does not grant access to a cloud project or make an external connector available under a different identity. |

**Sharing input can disclose private text across the selected profiles.** The filter skips recognizable credentials and unsupported references. Ordinary prose can still contain sensitive information. Keep history disabled when the profiles should only share project organization.

## Status, undo and uninstall

```sh
/usr/bin/python3 -I -B src/manage.py status
/usr/bin/python3 -I -B src/manage.py preview
/usr/bin/python3 -I -B src/manage.py uninstall
```

To undo the most recent sync, fully quit the desktop app, then run `/usr/bin/python3 -I -B src/manage.py restore`. Restore stops the listener. It refuses to overwrite affected values that have been edited since the sync. Uninstall removes the listener and its installed code; it leaves the native layout and private recovery data in place.

The user LaunchAgent is `local.codex.continuity`. Its files are in `~/Library/Application Support/CodexLocalContinuity`. Up to five recovery points are retained per sharing policy period. Changing the settings starts a fresh recovery area; the helper keeps up to five previous generated areas plus any original legacy recovery data. Recovery points can contain prior prompt text or rich entries from changed buckets. Their directory is private to the macOS user. Keep it out of bug reports, along with authentication files, native global state and session transcripts.

To update an installed copy from a new checkout, run `/usr/bin/python3 -I -B src/manage.py upgrade`. This keeps your saved group and features. Use `configure` or the settings editor to change consent explicitly.

Apache License 2.0. This project is not affiliated with OpenAI.

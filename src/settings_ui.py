"""Local terminal enrollment editor. It only reads state and returns a confirmed proposal."""
import os
import re

import sync


class Cancelled(Exception):
    pass


def edit(store, *, lang='en', input_fn=input, output=print, will_install=False):
    """Return the previewed policy after explicit confirmation, or None on cancellation."""
    zh = lang.lower().startswith('zh')
    def tr(en, cn):
        return cn if zh else en

    features = {
        'folders': tr('Only local folders and project list order', '仅本机项目目录和项目列表顺序'),
        'folders-history': tr('Folders, project list order and per-chat ↑ recall', '项目目录、项目列表顺序和聊天内 ↑ 输入历史'),
        'projects': tr('Project layout with explicit chat grouping', '项目分组，含手动指定的聊天归属'),
        'both': tr('Project layout, explicit chat grouping and per-chat ↑ recall', '项目分组、手动聊天归属和聊天内 ↑ 输入历史'),
        'history': tr('Per-chat ↑ recall only', '仅聊天内 ↑ 输入历史'),
        'off': tr('Pause sharing', '暂停共享'),
    }

    def ask(prompt):
        answer = input_fn(prompt).strip()
        if answer.casefold() in {'q', 'quit', 'cancel', '退出', '取消'}:
            raise Cancelled()
        return answer

    def menu(heading, items, default):
        output(heading)
        for i, (_, description) in enumerate(items, 1):
            output(f'  {i}. {description}')
        while True:
            answer = ask(tr('Choose a number', '输入选项编号') + f' [{default}] ')
            if not answer:
                return items[default - 1][0]
            if answer in {str(i) for i in range(1, len(items) + 1)}:
                return items[int(answer) - 1][0]
            output(tr('Enter a listed number or q.', '请输入上方编号，或输入 q 取消。'))

    def group(policy):
        if policy['scope'] == 'all':
            return tr('All verified profiles, including future profiles', '所有已验证身份，含今后新增的身份')
        return tr('Selected profiles ', '已选身份 ') + ', '.join(o[:12] for o in policy['owners'])

    try:
        try:
            profiles = store.list_profiles()
        except FileNotFoundError:
            profiles = []
        if not profiles:
            output(tr('Open Codex and sign in to initialize a local profile, then try again.',
                      '请先打开 Codex 并登录，待本机身份初始化后重试。'))
            return None
        current = store.policy() if os.path.lexists(store.enrollment) else None
        mode = sync.policy_features(current) if current is not None else 'projects'
        output(tr('Codex Local Continuity · Settings', 'Codex Local Continuity · 设置'))
        output(tr('Current enrollment', '当前设置'))
        if current is None:
            output(tr('  No enrollment saved. You can preview before installing.',
                      '  尚未保存授权范围。可在安装前预览。'))
        else:
            output('  ' + group(current))
            output('  ' + features[mode])
        output(tr('Verified local profiles', '已验证的本机身份'))
        labels = set()
        for profile in profiles:
            label = profile['label']
            if not isinstance(label, str) or re.fullmatch('[0-9a-f]{12}', label) is None or label in labels:
                raise ValueError('invalid-profile-label')
            labels.add(label)
            suffix = tr(' (active)', '（当前）') if profile['active'] else ''
            output('  ' + label + suffix)
        output(tr('Projects shares local project layout and chat grouping. ↑ recall can share sent text, '
                  'including private text, within the same local chat.',
                  '项目分组会共享本机项目和聊天归属。开启 ↑ 输入历史后，同一本机聊天中已发送的文字可能包含私密内容，并会在所选身份间共享。'))
        output(tr('Folder modes keep explicit chat assignments in each profile. Codex may show chats that '
                  'the profile can already access when their working directory matches a shared folder. '
                  'Codex controls access separately.',
                  '选择目录模式时，手动分配的聊天归属留在各自身份中。对于当前身份本来就能访问的聊天，'
                  'Codex 仍可能按工作目录归入对应项目。访问权限由 Codex 单独管理。'))
        output(tr('Cloud features use Codex’s own account settings. See the documentation for details. '
                  'Run doctor manually for a local check. Enter q to cancel.',
                  '云端功能使用 Codex 自身的账号设置，详见文档。可手动运行 doctor 检查本机状态。输入 q 可取消。'))
        choices = []
        if current is not None:
            choices.append((None, tr('Keep the saved enrollment', '保留已保存的身份范围')))
        choices += [
            ('existing', tr('All profiles shown above, fixed to today’s list', '上方所有身份，固定为当前名单')),
            ('choose', tr('Choose profiles by label', '按标签选择身份')),
            ('all', tr('All verified local profiles, including future sign-ins', '所有已验证本机身份，包括今后新增登录')),
        ]
        selection = menu(tr('Which profiles may share?', '哪些身份之间可以共享？'), choices, 1)
        if selection == 'choose':
            while True:
                answer = ask(tr('Paste labels, separated by commas: ', '输入标签，多个标签用逗号分隔：'))
                values = [v.strip() for v in answer.replace('，', ',').split(',')]
                if len(values) == len(set(values)) and all(v in labels for v in values):
                    selection = ','.join(values)
                    break
                output(tr('Use one or more distinct labels from the list.', '请输入上方名单中一个或多个不重复的标签。'))
        items = list(features.items())
        mode = menu(tr('What may be shared?', '允许共享哪些内容？'), items, list(features).index(mode) + 1)
        proposal = store.propose(selection, mode)
        plan = store.update('plan', proposal)
        counts = plan.get('counts', {})
        output(tr('Read-only preview', '只读预览'))
        output('  ' + group(proposal))
        output('  ' + features[sync.policy_features(proposal)])
        rows = [
            (tr('Enrolled profiles available', '当前可用的已选身份'), counts.get('profiles')),
            (tr('Projects', '项目数'), counts.get('projects', {}).get('projects')),
            (tr('Project conflicts', '项目冲突数'), counts.get('projects', {}).get('conflicts')),
            (tr('History entries to copy', '拟补入的输入记录数'), counts.get('history', {}).get('entries_copied')),
            (tr('Native fields to change', '拟修改的原生字段数'), counts.get('changed_fields')),
        ]
        for name, count in rows:
            if type(count) is int:
                output(f'  {name} · {count}')
        if current is None:
            output(tr('This preview does not install the helper.', '此次预览不会安装工具。'))
        if will_install:
            answer = ask(tr('Install and start the exit listener with these choices? [y/N] ',
                            '按以上选择安装并启动退出监听器？[y/N] '))
        else:
            answer = ask(tr('Confirm this exact proposal? [y/N] ', '确认以上范围和功能？[y/N] '))
        if answer.casefold() in {'y', 'yes', '确认', '是'}:
            return proposal
    except (EOFError, KeyboardInterrupt, Cancelled):
        pass
    output(tr('Cancelled. No settings were written.', '已取消，未写入设置。'))
    return None

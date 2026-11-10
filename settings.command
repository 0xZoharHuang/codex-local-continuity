#!/bin/sh
set -eu
cd -P -- "$(dirname -- "$0")"
manager="$PWD/src/manage.py"
case "${LC_ALL:-${LC_MESSAGES:-${LANG:-en}}}" in
  zh*) lang=zh ;;
  *) lang=en ;;
esac
while :; do
  if [ "$lang" = zh ]; then
    printf '\n'
    printf '%s\n' 'Codex 本机多账号' '1  选择身份与共享功能' '2  查看当前设置和同步结果' '3  查看哪些行为可以配置' '4  检查本机插件与定时任务' '5  保存切号前的对照记录' '6  与切号前的记录比较' '回车退出 · L / English'
  else
    printf '\n'
    printf '%s\n' 'Codex Local Continuity' '1  Choose profiles and sharing features' '2  Current settings and sync result' '3  What can I configure?' '4  Inspect local plugins and scheduled tasks' '5  Save a before-switch comparison point' '6  Compare after switching' 'Enter to exit · L / 中文'
  fi
  IFS= read -r choice || exit 0
  case "$choice" in
    1) set -- settings --lang "$lang" ;;
    2) set -- status --lang "$lang" ;;
    3) set -- capabilities --lang "$lang" ;;
    4) set -- doctor show --lang "$lang" ;;
    5) set -- doctor baseline --lang "$lang" ;;
    6) set -- doctor compare --lang "$lang" ;;
    [Ll]) if [ "$lang" = zh ]; then lang=en; else lang=zh; fi; continue ;;
    '') exit 0 ;;
    *) continue ;;
  esac
  /usr/bin/python3 -I -B "$manager" "$@" || :
done

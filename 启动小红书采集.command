#!/bin/zsh
# Finder double-click entry; resolve symlinks so the Desktop shortcut also works.
PROJECT_DIR="${0:A:h}"
cd -- "$PROJECT_DIR" || exit 1
if [[ ! -x "$PROJECT_DIR/.venv/bin/python" ]]; then
  print '未找到项目 Python 环境。请先按照 README 完成首次安装。'
  read '?按回车关闭…'
  exit 1
fi
export PYTHONUTF8=1
export PYTHONUNBUFFERED=1
"$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/scripts/launcher.py" "$@"
result=$?
if (( result != 0 )); then
  read '?按回车关闭…'
fi
exit "$result"

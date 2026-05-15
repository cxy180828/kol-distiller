#!/bin/bash
# KOL Distiller - Cron定时任务安装脚本
#
# 使用方式：
#   chmod +x setup_cron.sh
#   ./setup_cron.sh
#
# 会自动添加每6小时执行一次的定时任务

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_BIN="$(which python3)"
LOG_FILE="${SCRIPT_DIR}/logs/cron.log"

# 确保logs目录存在
mkdir -p "${SCRIPT_DIR}/logs"

# cron表达式：每6小时执行一次（0点、6点、12点、18点）
CRON_EXPR="0 */6 * * *"
CRON_CMD="${PYTHON_BIN} ${SCRIPT_DIR}/main.py cron >> ${LOG_FILE} 2>&1"

# 检查是否已经存在
if crontab -l 2>/dev/null | grep -q "kol-distiller"; then
    echo "⚠️  cron任务已存在，先移除旧的..."
    crontab -l 2>/dev/null | grep -v "kol-distiller" | crontab -
fi

# 添加新的cron任务
(crontab -l 2>/dev/null; echo "${CRON_EXPR} ${CRON_CMD} # kol-distiller") | crontab -

echo "✅ Cron定时任务已安装"
echo ""
echo "  频率: 每6小时（0:00, 6:00, 12:00, 18:00 UTC）"
echo "  命令: ${CRON_CMD}"
echo "  日志: ${LOG_FILE}"
echo ""
echo "查看当前cron: crontab -l"
echo "移除任务: crontab -l | grep -v kol-distiller | crontab -"

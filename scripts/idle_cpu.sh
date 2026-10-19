#!/usr/bin/env bash
# idle_cpu.sh — оценить нагрузку на CPU в простое (нет движения в кадре).
#
# Запускает приложение на N секунд (по умолчанию 30) и усредняет суммарный %CPU
# всех python-процессов этого проекта. Хороший headless-профиль: единицы процентов
# (Motion Gate не даёт тяжёлым моделям крутиться впустую).
#
# Использование:
#   ./scripts/idle_cpu.sh          # 30 секунд
#   ./scripts/idle_cpu.sh 60       # 60 секунд
set -euo pipefail
cd "$(dirname "$0")/.."

DUR="${1:-30}"
LOG=/tmp/cam_idle.log

echo "▸ Замер CPU в простое: ${DUR}s (наведите камеру на неподвижную сцену)"
./run_local.sh >"$LOG" 2>&1 &
APP_PID=$!

cleanup() { kill "$APP_PID" 2>/dev/null || true; pkill -P "$APP_PID" 2>/dev/null || true; }
trap cleanup EXIT

sleep 8   # прогрев MOG2 (warmup_frames)

total=0; samples=0
end=$(( $(date +%s) + DUR ))
while [ "$(date +%s)" -lt "$end" ]; do
    cpu=$(ps -Ao %cpu,command | grep -E "python[0-9.]* .*main\.py" | grep -v grep \
          | awk '{s+=$1} END {printf "%.1f", s+0}')
    total=$(awk -v t="$total" -v c="$cpu" 'BEGIN{printf "%.2f", t+c}')
    samples=$((samples+1))
    sleep 1
done

cleanup
trap - EXIT

avg=$(awk -v t="$total" -v n="$samples" 'BEGIN{printf "%.1f", (n? t/n : 0)}')
echo "==========================================="
echo "Средняя нагрузка CPU в простое: ${avg}%  (${samples} замеров за ${DUR}s)"
echo "Смотрите также строки «Статистика:» в логе: $LOG"
grep -E "Статистика|лицо|ТРИГГЕР" "$LOG" | tail -5 || true
